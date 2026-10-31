"""Build the Telegram messages from plain fields.

Everything that came from outside (job title, description, the draft) is
escaped before it goes into the message, and every part is cut to a fixed
length measured AFTER escaping, so a post full of "&" or "<" can never push a
message over Telegram's 4096 character limit or leave a half-cut entity.

Two renderings of the same layout: Telegram HTML for the bot, plain text for
the terminal demo.

Standard library only.
"""
from __future__ import annotations

import html
import re
from typing import Dict, List, Optional, Tuple

from .rules import Job, Verdict

TELEGRAM_LIMIT = 4096
CUT = " (cut)"

STATUS_WORDS = {"pass": "pass", "fail": "fail", "unknown": "unknown"}
CHECK_LABELS = {
    "job_type": "Job type",
    "budget": "Budget",
    "competition": "Competition",
    "client_rating": "Client rating",
}


def esc(text: str) -> str:
    """Escape for Telegram HTML text."""
    return html.escape(str(text), quote=False)


def esc_attr(text: str) -> str:
    """Escape for a Telegram HTML attribute value."""
    return html.escape(str(text), quote=True)


def fit(raw: str, max_escaped: int, escape=esc) -> str:
    """Cut raw text so that its escaped form is at most max_escaped characters.

    Cuts at a space when it can and adds a short note. Returns the RAW text
    (escape it afterwards); cutting raw text can never split an entity.
    """
    raw = str(raw)
    raw = " ".join(raw.split()) if "\n" not in raw else raw.strip()
    if len(escape(raw)) <= max_escaped:
        return raw
    room = max_escaped - len(escape(CUT))
    out, used = [], 0
    for ch in raw:
        n = len(escape(ch))
        if used + n > room:
            break
        out.append(ch)
        used += n
    cut = "".join(out)
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip() + CUT


class _Html:
    esc = staticmethod(esc)

    @staticmethod
    def bold(text: str) -> str:
        return f"<b>{esc(text)}</b>"

    @staticmethod
    def block(text: str) -> str:
        return f"<pre>{esc(text)}</pre>"

    @staticmethod
    def link(url: str, label: str) -> str:
        return f'<a href="{esc_attr(url)}">{esc(label)}</a>'


class _Plain:
    @staticmethod
    def esc(text: str) -> str:
        return str(text)

    @staticmethod
    def bold(text: str) -> str:
        return str(text)

    @staticmethod
    def block(text: str) -> str:
        return "\n".join(("    " + line) if line else "" for line in str(text).splitlines())

    @staticmethod
    def link(url: str, label: str) -> str:
        return f"{label}: {url}"


def _fmt(as_html: bool):
    return _Html if as_html else _Plain


def fit_draft(draft: str, room: int, escape, tail: str = "") -> str:
    """Cut a draft to `room` escaped characters, but never cut off its closing `tail` line (the AI disclosure line)."""
    if tail and draft.endswith(tail):
        closing = "\n\n" + tail
        left = room - len(escape(closing))
        if left > 0:
            return fit(draft[: -len(tail)].rstrip(), left, escape) + closing
    return fit(draft, room, escape)


def _ai_lines(ai, f) -> List[str]:
    """The AI check section. `ai` is a bot.llm.AiResult. Every model-written part is escaped and cut."""
    e = f.esc
    lines = [""]
    if ai.status == "pass":
        lines.append(f"AI check: confidence {ai.confidence} out of 100, can deliver: yes")
        lines.append("(the confidence is the model's own estimate, not a measured accuracy)")
        if ai.reason:
            lines.append("AI reason: " + e(fit(ai.reason, 200, e)))
        if ai.risks:
            lines.append("AI risks: " + e(fit("; ".join(ai.risks), 200, e)))
    else:
        lines.append("AI check: unknown, " + e(fit(ai.note, 120, e)) + ". The ping was not blocked.")
    lines.append("Model: " + e(fit(ai.model_label, 80, e)))
    return lines


def render_ping(job: Job, verdict: Verdict, draft: str, as_html: bool = True, ai=None) -> str:
    """The message with the job, the checks, the optional AI check and the saved draft."""
    f = _fmt(as_html)
    e = f.esc
    lines: List[str] = [f.bold("New job: " + fit(job.title, 300, e))]
    if verdict.score is None:
        lines.append("Score: not enough known checks")
    else:
        lines.append(f"Score: {round(verdict.score * 100)} out of 100")
    if verdict.matched_skills:
        lines.append("Matches your profile: " + e(fit(", ".join(verdict.matched_skills), 250, e)))
    lines.append("")
    for c in verdict.checks:
        lines.append(f"{CHECK_LABELS[c.name]}: {STATUS_WORDS[c.status]}, " + e(fit(c.detail, 250, e)))
    if ai is not None:
        lines += _ai_lines(ai, f)
    link_line = f.link(job.url, "Open the job post")
    tail = ["", link_line]
    if ai is not None and ai.draft is not None:
        heading = "Saved draft (written by the AI step, read it before you use it)"
    elif ai is not None:
        heading = "Saved draft (from your template" + (": " + fit(ai.draft_note, 100, e) if ai.draft_note else "") + ")"
    else:
        heading = "Saved draft"
    head_text = "\n".join(lines + ["", f.bold(heading), ""])
    tail_text = "\n".join(tail)
    # The draft gets whatever room is left; the block wrapper costs a few characters.
    room = TELEGRAM_LIMIT - len(head_text) - len(tail_text) - 40
    tail = ai.disclosure if ai is not None and ai.draft is not None else ""
    block = f.block(fit_draft(draft, max(room - 11, 0), e, tail)) if draft.strip() else "(empty draft)"
    return head_text + block + "\n" + tail_text


def render_go(job_title: str, url: str, draft: str, as_html: bool = True, tail: str = "") -> str:
    """What Go delivers: the job link and the saved draft. tail: a closing line (the AI disclosure) that is never cut off."""
    f = _fmt(as_html)
    e = f.esc
    head = "\n".join([f.bold("Go: " + fit(job_title, 300, e)), f.link(url, "Open the job post"), "", f.bold("Saved draft"), ""])
    room = TELEGRAM_LIMIT - len(head) - 40
    return head + f.block(fit_draft(draft, max(room - 11, 0), e, tail))


def skip_note(title: str, reason: str) -> str:
    """The short plain-text note posted silently when the AI check skips a job."""
    return fit(f"Skipped: {title}. {reason}", 600, lambda s: s)


# ---------- buttons ----------

def keyboard(job_id: str) -> Dict:
    """The two buttons under a ping."""
    return {"inline_keyboard": [[
        {"text": "Go", "callback_data": f"go:{job_id}"},
        {"text": "Skip", "callback_data": f"skip:{job_id}"},
    ]]}


def closed_keyboard(status: str) -> Dict:
    """What the buttons turn into after a tap: one label that does nothing."""
    label = {"go": "Go: link and draft below", "skip": "Skipped"}.get(status, status)
    return {"inline_keyboard": [[{"text": label, "callback_data": "noop:-"}]]}


_CALLBACK_RE = re.compile(r"^(go|skip|noop):([~0-9A-Za-z_-]{1,40})$")


def parse_callback(data: object) -> Optional[Tuple[str, str]]:
    """'go:<id>' to ('go', '<id>'); anything unexpected gives None."""
    m = _CALLBACK_RE.match(data) if isinstance(data, str) else None
    return (m.group(1), m.group(2)) if m else None
