#!/usr/bin/env python3
"""Print sample pings for made-up jobs. No accounts, keys or network needed.

  python3 demo.py

Run 1 has the optional AI steps off, which is the default: a keyword checklist
and a fixed draft template. Two jobs: one gets a ping, one is filtered out.

Run 2 switches the AI steps on with the FAKE model. Its answers are scripted in
this file, nothing is assessed and no network call is made. It shows what the
ping looks like with an AI check and an AI draft, and a job the AI check skips.
"""
import dataclasses
import os
import sys

from bot import message
from bot.llm import Assistant, DailyCounter, FakeProvider, parse_llm_config
from bot.rules import build_draft, load_config, parse_job, score_job

HERE = os.path.dirname(os.path.abspath(__file__))

SAMPLE_JOBS = [
    {
        "id": "~01demojobalpha01",
        "url": "https://example.com/jobs/~01demojobalpha01",
        "title": "Clean up and merge three sales spreadsheets",
        "description": "We have three spreadsheet files from different regions with mixed date formats. "
                       "Need data cleanup and one tidy summary dashboard.",
        "payment": "fixed",
        "budget": 250,
        "proposals": 4,
        "client_rating": None,
    },
    {
        "id": "~01demojobbravo02",
        "url": "https://example.com/jobs/~01demojobbravo02",
        "title": "Fill in a daily spreadsheet by hand",
        "description": "Small spreadsheet task, very cheap.",
        "payment": "fixed",
        "budget": 15,
        "proposals": 40,
        "client_rating": 3.9,
    },
]

# Passes the keyword rules, so in run 2 it reaches the (fake) model, which says it is not sure enough.
AI_ONLY_JOB = {
    "id": "~01demojobdelta04",
    "url": "https://example.com/jobs/~01demojobdelta04",
    "title": "Python dashboard plus a native mobile app with payments",
    "description": "We need a reporting dashboard in Python and also an iPhone app that takes card payments.",
    "payment": "fixed",
    "budget": 900,
    "proposals": 3,
    "client_rating": 4.9,
}

# The FAKE model's scripted answers, in the order the calls happen in run 2.
FAKE_ANSWERS = [
    {"can_deliver": True, "confidence": 97,
     "reason": "Spreadsheet merging and a summary dashboard match two portfolio entries.",
     "risks": ["Date formats may need a decision from the client."]},
    {"draft": "Hello, I read your post about merging three regional sales spreadsheets with mixed date formats. "
              "My portfolio has a customer list cleanup that merged three sheets into one deduplicated sheet, and a "
              "weekly sales dashboard built from CSV exports. Tell me what the summary should show and I will "
              "confirm the plan. (Scripted text from the fake model.)"},
    {"can_deliver": False, "confidence": 40,
     "reason": "The dashboard fits the portfolio but a native mobile app with card payments does not.",
     "risks": ["Mobile app work is not in the profile."]},
]


def show(job, cfg, assistant=None):
    verdict = score_job(job, cfg)
    print("=" * 60)
    if not verdict.ping:
        print(f"Job: {job.title}")
        print(f"No ping. {verdict.reason}.")
        return
    ai = None
    if assistant is not None:
        ai = assistant.assess(job, cfg["profile"]["skills"])
        if ai.status == "skip":
            print("No ping. A silent note is posted to Telegram instead, with no buttons:")
            print(message.skip_note(job.title, ai.skip_reason))
            return
    draft = ai.draft if ai is not None and ai.draft else build_draft(job, verdict, cfg)
    print(message.render_ping(job, verdict, draft, as_html=False, ai=ai))
    buttons = message.keyboard(job.id)["inline_keyboard"][0]
    print("\nButtons: " + "   ".join(f"[ {b['text']} ]" for b in buttons))
    print("Go posts the job link and the draft again. Skip closes the job.")


def main() -> int:
    cfg = load_config(os.path.join(HERE, "config.example.json"))

    print("RUN 1: AI steps off (the default)")
    for raw in SAMPLE_JOBS:
        show(parse_job(raw), cfg)

    print("\n" + "#" * 60)
    print("RUN 2: AI steps on, with the FAKE model")
    print("The model's answers are scripted in demo.py. No network call, nothing is assessed.")
    print("#" * 60)
    llm = dataclasses.replace(parse_llm_config(cfg), enabled=True, provider="fake")
    assistant = Assistant(llm, FakeProvider(FAKE_ANSWERS), DailyCounter(10))
    show(parse_job(SAMPLE_JOBS[0]), cfg, assistant)
    show(parse_job(AI_ONLY_JOB), cfg, assistant)
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
