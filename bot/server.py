"""The webhook: receives jobs from the Gmail watcher, pings Telegram, handles the buttons.

Two doors, each with its own secret, both checked in constant time:

  POST /jobs       header X-Api-Key                       from the Gmail watcher
  POST /telegram   header X-Telegram-Bot-Api-Secret-Token  from Telegram (button taps)
  GET  /health     no secret, answers {"ok": true} and nothing else

A job goes: parse and check the fields, skip it if already seen, score it with
the rules, and only then ping with the saved draft and two buttons. Go posts
the job link and the draft back to you. Skip closes the job. Nothing here ever
contacts the job site; you read the post and submit yourself.

The draft (and the AI result, when the AI steps are on) is saved before
Telegram is tried, so a retry after a Telegram failure reuses it instead of
asking the model again. Model calls run outside the lock that guards the
saved state, so button taps never wait for them; a job that is already being
worked on is answered "try again" (503) instead of being worked on twice.

An alert email with no job the watcher could read arrives as "unreadable":
the webhook posts a short silent note about it, once per email.

Standard library only.
"""
from __future__ import annotations

import hmac
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from . import message
from .llm import BATCH_SECONDS, AiResult, Assistant
from .rules import JobError, build_draft, link_hosts, parse_job, score_job
from .settings import Settings
from .store import Store
from .telegram import Telegram, TelegramError

MAX_BODY = 64 * 1024
MAX_JOBS_PER_POST = 20
READ_TIMEOUT = 10   # seconds a client may take to send its request; a stalled connection is closed after that
_MAIL_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

Reply = Tuple[int, Dict[str, Any]]


class App:
    """The logic, with no HTTP in it, so it can be tested directly."""

    def __init__(self, cfg: Dict[str, Any], store: Store, tg: Telegram, assistant: Optional[Assistant] = None,
                 clock=time.monotonic):
        self.cfg, self.store, self.tg = cfg, store, tg
        self.link_hosts = link_hosts(cfg)   # a second lock behind the watcher's: job links must point to these hosts
        self.assistant = assistant   # None means the AI steps are off, which is the default
        self._clock = clock
        self._lock = threading.Lock()   # guards reading and changing the saved state, never held during a model call
        self._busy = set()              # ids of the jobs being worked on right now

    # ----- jobs from the watcher -----

    def handle_jobs(self, body: Any) -> Reply:
        jobs = body.get("jobs") if isinstance(body, dict) else None
        notes = body.get("unreadable", []) if isinstance(body, dict) else None
        if (not isinstance(jobs, list) or len(jobs) > MAX_JOBS_PER_POST
                or not isinstance(notes, list) or len(notes) > MAX_JOBS_PER_POST):
            return 400, {"error": f"expected {{\"jobs\": [...]}} with at most {MAX_JOBS_PER_POST} jobs"
                                  f" (and at most {MAX_JOBS_PER_POST} unreadable alerts)"}
        result: Dict[str, Any] = {"pinged": [], "filtered": [], "duplicates": 0, "invalid": [], "busy": [], "noted": []}
        ai_deadline = self._clock() + BATCH_SECONDS
        for index, raw in enumerate(jobs):
            try:
                job = parse_job(raw, self.link_hosts)
            except JobError as err:
                result["invalid"].append({"index": index, "reason": str(err)})
                continue
            state, saved = self._claim(job.id)
            if state == "duplicate":
                result["duplicates"] += 1
                continue
            if state == "busy":
                result["busy"].append(job.id)
                continue
            try:
                error = self._handle_job(job, saved, ai_deadline, result)
            finally:
                with self._lock:
                    self._busy.discard(job.id)
            if error:
                # The draft and any AI result are saved as "unsent", so the watcher's retry pings without a new model call.
                result["error"] = error
                return 502, result
        for index, raw in enumerate(notes):
            error = self._handle_unreadable(index, raw, result)
            if error:
                result["error"] = error
                return 502, result
        # A job someone else is working on right now: "try again", so the watcher posts it once more next minute.
        return (503 if result["busy"] else 200), result

    def _claim(self, job_id: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        """("new", None), ("retry", saved record), ("duplicate", None) or ("busy", None). A new or retried job is marked busy."""
        with self._lock:
            if job_id in self._busy:
                return "busy", None
            rec = self.store.get(job_id)
            if rec is not None and rec.get("status") != "unsent":
                return "duplicate", None
            self._busy.add(job_id)
            return ("retry", rec) if rec is not None else ("new", None)

    def _handle_job(self, job, saved: Optional[Dict[str, Any]], ai_deadline: float, result: Dict[str, Any]) -> Optional[str]:
        """Score, check, save and ping one job that this request holds. Returns a Telegram error, or None."""
        verdict = score_job(job, self.cfg)
        if saved is None:
            if not verdict.ping:
                self.store.put(job.id, {"status": "filtered", "title": job.title})
                result["filtered"].append({"id": job.id, "reason": verdict.reason})
                return None
            ai = None
            if self.assistant is not None:
                # Only jobs the keyword rules already accepted reach the model. No lock is held here.
                ai = self.assistant.assess(job, self.cfg["profile"]["skills"], ai_deadline)
                if ai.status == "skip":
                    self.store.put(job.id, {"status": "filtered", "title": job.title})
                    result["filtered"].append({"id": job.id, "reason": ai.skip_reason, "by": "ai"})
                    self._note_skip(job.title, ai.skip_reason)
                    return None
            draft = ai.draft if ai is not None and ai.draft else build_draft(job, verdict, self.cfg)
            tail = ai.disclosure if ai is not None and ai.draft else ""   # the template draft never gets the disclosure line
            # Saved before Telegram is tried, so a failure costs no second model call.
            self.store.put(job.id, {"status": "unsent", "title": job.title, "url": job.url, "draft": draft,
                                    "draft_tail": tail, "ai": ai.to_dict() if ai is not None else None})
        else:
            draft, tail = saved.get("draft", ""), saved.get("draft_tail", "")
            ai = AiResult.from_dict(saved["ai"]) if saved.get("ai") else None
        try:
            msg_id = self.tg.post_message(message.render_ping(job, verdict, draft, ai=ai), message.keyboard(job.id))
        except TelegramError as err:
            return str(err)
        self.store.update(job.id, status="pending", message_id=msg_id)
        result["pinged"].append(job.id)
        return None

    def _handle_unreadable(self, index: int, raw: Any, result: Dict[str, Any]) -> Optional[str]:
        """One short silent note per alert email with no job in it. Returns a Telegram error, or None."""
        mail_id = raw.get("id") if isinstance(raw, dict) else None
        subject = raw.get("subject") if isinstance(raw, dict) else None
        if not isinstance(mail_id, str) or not _MAIL_ID_RE.match(mail_id) or not isinstance(subject, str) or not subject.strip():
            result["invalid"].append({"unreadable_index": index, "reason": "an unreadable alert needs an id and a subject"})
            return None
        key = "alert:" + mail_id   # ":" never appears in a job id, so this can never clash with a job or a button
        subject = " ".join(_CONTROL_RE.sub(" ", subject).split())[:200]
        with self._lock:
            if self.store.has(key) or key in self._busy:
                result["duplicates"] += 1
                return None
            self._busy.add(key)
        try:
            self.tg.post_message(message.unreadable_note(subject), html=False, silent=True)
            self.store.put(key, {"status": "noted", "title": subject})
            result["noted"].append(mail_id)
            return None
        except TelegramError as err:
            return str(err)
        finally:
            with self._lock:
                self._busy.discard(key)

    def _note_skip(self, title: str, reason: str) -> None:
        """A short silent note, so a job skipped by the AI check is not lost without a trace. Plain text, no buttons."""
        try:
            self.tg.post_message(message.skip_note(title, reason), html=False, silent=True)
        except TelegramError as err:
            print(f"could not post a skip note: {err}", file=sys.stderr)

    # ----- button taps from Telegram -----

    def _answer(self, callback_id: str, text: str) -> None:
        try:
            self.tg.answer_tap(callback_id, text)
        except TelegramError as err:
            print(f"could not answer a tap: {err}", file=sys.stderr)

    def handle_update(self, update: Any) -> Reply:
        cq = update.get("callback_query") if isinstance(update, dict) else None
        if not isinstance(cq, dict):
            return 200, {"ok": True, "ignored": True}
        callback_id = str(cq.get("id", ""))
        tapper = str((cq.get("from") or {}).get("id", ""))
        chat = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
        if tapper != self.tg.chat_id or chat != self.tg.chat_id:
            self._answer(callback_id, "Not allowed")
            return 200, {"ok": True, "ignored": True}
        parsed = message.parse_callback(cq.get("data"))
        if parsed is None:
            self._answer(callback_id, "Unknown button")
            return 200, {"ok": True, "ignored": True}
        action, job_id = parsed
        if action == "noop":
            self._answer(callback_id, "")
            return 200, {"ok": True}
        with self._lock:
            rec = self.store.get(job_id)
            if rec is None:
                self._answer(callback_id, "This job is gone")
                return 200, {"ok": True}
            if rec.get("status") != "pending":
                self._answer(callback_id, f"Already {rec.get('status')}")
                return 200, {"ok": True}
            if action == "go":
                try:
                    self.tg.post_message(message.render_go(rec["title"], rec["url"], rec["draft"], tail=rec.get("draft_tail", "")))
                except TelegramError as err:
                    print(f"could not post the draft: {err}", file=sys.stderr)
                    self._answer(callback_id, "Telegram error, tap Go again")
                    return 200, {"ok": False}
                self.store.update(job_id, status="go")
                self._close_buttons(rec, "go")
                self._answer(callback_id, "Link and draft posted below")
            else:
                self.store.update(job_id, status="skip")
                self._close_buttons(rec, "skip")
                self._answer(callback_id, "Skipped")
        return 200, {"ok": True}

    def _close_buttons(self, rec: Dict[str, Any], status: str) -> None:
        try:
            self.tg.set_keyboard(rec["message_id"], message.closed_keyboard(status))
        except TelegramError as err:
            print(f"could not update the buttons: {err}", file=sys.stderr)


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def make_handler(app: App, settings: Settings):
    class Handler(BaseHTTPRequestHandler):
        server_version = "job-alert-assistant"
        sys_version = ""
        timeout = READ_TIMEOUT   # a client that stalls while sending its request is cut off

        def log_message(self, fmt, *args):  # one short line, never headers or bodies
            command = getattr(self, "command", None) or "-"
            path = (getattr(self, "path", None) or "-").split("?")[0]
            print(f"{command} {path} {args[1] if len(args) > 1 else fmt.split(':')[0]}", file=sys.stderr)

        def _reply(self, status: int, body: Dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> Any:
            """The parsed JSON body, or None after replying with the error."""
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._reply(411, {"error": "Content-Length required"})
                return None
            if length < 0 or length > MAX_BODY:
                self._reply(413, {"error": "body too large"})
                return None
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, RecursionError):   # RecursionError: JSON nested too deeply to read
                self._reply(400, {"error": "body is not valid JSON"})
                return None

        def do_GET(self):
            if self.path.split("?")[0] == "/health":
                self._reply(200, {"ok": True})
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self):
            doors = {"/jobs": ("X-Api-Key", settings.api_key, app.handle_jobs),
                     "/telegram": ("X-Telegram-Bot-Api-Secret-Token", settings.webhook_secret, app.handle_update)}
            door = doors.get(self.path.split("?")[0])
            if door is None:
                self._reply(404, {"error": "not found"})
                return
            header, secret, handler = door
            if not _same(self.headers.get(header, ""), secret):   # checked before the body is read
                self._reply(401, {"error": "unauthorized"})
                return
            body = self._body()
            if body is None:
                return
            status, out = handler(body)
            self._reply(status, out)

    return Handler


def make_server(settings: Settings, app: App) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((settings.host, settings.port), make_handler(app, settings))
