"""The webhook: receives jobs from the Gmail watcher, pings Telegram, handles the buttons.

Two doors, each with its own secret, both checked in constant time:

  POST /jobs       header X-Api-Key                       from the Gmail watcher
  POST /telegram   header X-Telegram-Bot-Api-Secret-Token  from Telegram (button taps)
  GET  /health     no secret, answers {"ok": true} and nothing else

A job goes: parse and check the fields, skip it if already seen, score it with
the rules, and only then ping with the saved draft and two buttons. Go posts
the job link and the draft back to you. Skip closes the job. Nothing here ever
contacts the job site; you read the post and submit yourself.

Standard library only.
"""
from __future__ import annotations

import hmac
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from . import message
from .llm import BATCH_SECONDS, Assistant
from .rules import JobError, build_draft, link_hosts, parse_job, score_job
from .settings import Settings
from .store import Store
from .telegram import Telegram, TelegramError

MAX_BODY = 64 * 1024
MAX_JOBS_PER_POST = 20

Reply = Tuple[int, Dict[str, Any]]


class App:
    """The logic, with no HTTP in it, so it can be tested directly."""

    def __init__(self, cfg: Dict[str, Any], store: Store, tg: Telegram, assistant: Optional[Assistant] = None,
                 clock=time.monotonic):
        self.cfg, self.store, self.tg = cfg, store, tg
        self.link_hosts = link_hosts(cfg)   # a second lock behind the watcher's: job links must point to these hosts
        self.assistant = assistant   # None means the AI steps are off, which is the default
        self._clock = clock
        self._lock = threading.Lock()

    # ----- jobs from the watcher -----

    def handle_jobs(self, body: Any) -> Reply:
        jobs = body.get("jobs") if isinstance(body, dict) else None
        if not isinstance(jobs, list) or len(jobs) > MAX_JOBS_PER_POST:
            return 400, {"error": f"expected {{\"jobs\": [...]}} with at most {MAX_JOBS_PER_POST} jobs"}
        result: Dict[str, Any] = {"pinged": [], "filtered": [], "duplicates": 0, "invalid": []}
        with self._lock:
            ai_deadline = self._clock() + BATCH_SECONDS
            for index, raw in enumerate(jobs):
                try:
                    job = parse_job(raw, self.link_hosts)
                except JobError as err:
                    result["invalid"].append({"index": index, "reason": str(err)})
                    continue
                if self.store.has(job.id):
                    result["duplicates"] += 1
                    continue
                verdict = score_job(job, self.cfg)
                if not verdict.ping:
                    self.store.put(job.id, {"status": "filtered", "title": job.title})
                    result["filtered"].append({"id": job.id, "reason": verdict.reason})
                    continue
                ai = None
                if self.assistant is not None:
                    # Only jobs the keyword rules already accepted reach the model.
                    ai = self.assistant.assess(job, self.cfg["profile"]["skills"], ai_deadline)
                    if ai.status == "skip":
                        self.store.put(job.id, {"status": "filtered", "title": job.title})
                        result["filtered"].append({"id": job.id, "reason": ai.skip_reason, "by": "ai"})
                        self._note_skip(job.title, ai.skip_reason)
                        continue
                draft = ai.draft if ai is not None and ai.draft else build_draft(job, verdict, self.cfg)
                tail = ai.disclosure if ai is not None and ai.draft else ""   # the template draft never gets the disclosure line
                text = message.render_ping(job, verdict, draft, ai=ai)
                try:
                    msg_id = self.tg.post_message(text, message.keyboard(job.id))
                except TelegramError as err:
                    # Nothing is stored for this job, so a retry from the watcher pings it again.
                    result["error"] = str(err)
                    return 502, result
                self.store.put(job.id, {"status": "pending", "title": job.title, "url": job.url,
                                        "draft": draft, "draft_tail": tail, "message_id": msg_id})
                result["pinged"].append(job.id)
        return 200, result

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

        def log_message(self, fmt, *args):  # one short line, never headers or bodies
            print(f"{self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}", file=sys.stderr)

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
            except ValueError:
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
