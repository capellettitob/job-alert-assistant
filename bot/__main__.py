"""Command line.

  python3 -m bot serve                 run the webhook
  python3 -m bot set-webhook <url>     tell Telegram where to deliver button taps
  python3 -m bot check                 check the environment variables and the config, ask Telegram who the bot
                                       is and send one test message to your chat
  python3 -m bot score job.json        score one job file offline (no keys needed)
  python3 -m bot llm-check --yes       optional: make real, paid calls to the AI model to test your key and setup
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys

from . import message
from .llm import Assistant, DailyCounter, LlmError, build_assistant, parse_llm_config, AnthropicProvider, FakeProvider
from .rules import ConfigError, JobError, build_draft, load_config, parse_job, score_job
from .server import App, make_server
from .settings import Settings, SettingsError
from .store import Store
from .telegram import Telegram, TelegramError, http_transport


def _settings() -> Settings:
    try:
        return Settings.from_env()
    except SettingsError as err:
        sys.exit(f"ERROR: {err}")


def _config(path: str):
    try:
        return load_config(path)
    except ConfigError as err:
        sys.exit(f"ERROR: {err}")


SAMPLE_PORTFOLIO = (("Sales report dashboard", "Built a weekly sales dashboard in a spreadsheet from raw CSV exports."),)
CHECK_JOB = {"id": "~01demojobcheck001", "url": "https://example.com/jobs/check", "title": "Merge two sales spreadsheets",
             "description": "Two regional spreadsheets with different date formats need to become one clean sheet with a summary tab."}


def _llm_check(args) -> int:
    """One optional command for the owner: test the AI setup with an invented job. Paid calls, so it asks for --yes."""
    cfg = _config(args.config)
    try:
        llm = parse_llm_config(cfg)
    except ConfigError as err:
        sys.exit(f"ERROR: {err}")
    if not args.yes:
        print(f"llm-check makes up to 2 real, paid calls to the model {llm.model} ({llm.provider} provider) using one invented job.\n"
              "Nothing from your mailbox or your jobs is sent. It does not change any file.\n"
              "Run it again with --yes to go ahead.")
        return 0
    try:
        provider = FakeProvider() if llm.provider == "fake" else AnthropicProvider(os.environ.get("ANTHROPIC_API_KEY", ""), llm.model)
    except LlmError as err:
        sys.exit(f"ERROR: {err}")
    llm = llm if llm.portfolio else dataclasses.replace(llm, portfolio=SAMPLE_PORTFOLIO)
    assistant = Assistant(llm, provider, DailyCounter(10))   # its own in-memory counter, your daily count is not touched
    result = assistant.assess(parse_job(CHECK_JOB), cfg["profile"]["skills"])
    print(f"model: {result.model_label}")
    print(f"feasibility: {result.status}" + (f", confidence {result.confidence}, can deliver {result.can_deliver}" if result.confidence is not None else ""))
    if result.reason:
        print(f"reason: {result.reason}")
    if result.risks:
        print("risks: " + "; ".join(result.risks))
    if result.status == "unknown":
        print(f"problem: {result.note}")
        return 1
    print("draft: " + ("accepted by the draft rules" if result.draft else f"not used ({result.draft_note})" if result.status == "pass" else "not asked for (the job was skipped)"))
    if result.draft:
        print("---\n" + result.draft + "\n---")
    return 0 if result.status == "skip" or result.draft else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m bot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("check")
    hook = sub.add_parser("set-webhook")
    hook.add_argument("url")
    score = sub.add_parser("score")
    score.add_argument("file")
    score.add_argument("--config", default=os.environ.get("JAA_CONFIG", "config.json"))
    check = sub.add_parser("llm-check")
    check.add_argument("--config", default=os.environ.get("JAA_CONFIG", "config.json"))
    check.add_argument("--yes", action="store_true", help="really make the paid calls")
    args = ap.parse_args(argv)

    if args.cmd == "llm-check":
        return _llm_check(args)

    if args.cmd == "score":
        cfg = _config(args.config)
        try:
            with open(args.file, encoding="utf-8") as f:
                job = parse_job(json.load(f))
        except (OSError, ValueError) as err:
            sys.exit(f"ERROR: cannot read the job: {err}")
        verdict = score_job(job, cfg)
        print(message.render_ping(job, verdict, build_draft(job, verdict, cfg), as_html=False) if verdict.ping
              else f"Not pinged: {verdict.reason}")
        return 0

    settings = _settings()
    cfg = _config(settings.config_path)
    tg = Telegram(settings.chat_id, http_transport(settings.telegram_token))

    try:
        assistant = build_assistant(cfg, os.environ, settings.state_path)
    except (ConfigError, LlmError) as err:
        sys.exit(f"ERROR: {err}")
    ai_state = f"on, provider {assistant.cfg.provider}, model {assistant.cfg.model}" if assistant else "off"

    if args.cmd == "check":
        print("environment and config look fine")
        print(f"AI steps: {ai_state}")
        try:
            me = tg.get_me()
            tg.post_message("job-alert-assistant: test message from python3 -m bot check. The bot can write to this chat.", html=False)
        except TelegramError as err:
            sys.exit(f"ERROR: Telegram: {err}\nCheck TELEGRAM_BOT_TOKEN, and that TELEGRAM_CHAT_ID is the chat where you wrote to the bot.")
        print(f"Telegram: the bot @{me.get('username', '?')} answered and one test message was sent to your chat")
        return 0
    if args.cmd == "set-webhook":
        if not args.url.startswith("https://"):
            sys.exit("ERROR: Telegram needs an https address")
        try:
            tg.set_webhook(args.url, settings.webhook_secret)
        except TelegramError as err:
            sys.exit(f"ERROR: {err}")
        print("webhook set")
        return 0

    app = App(cfg, Store(settings.state_path), tg, assistant)
    server = make_server(settings, app)
    print(f"AI steps: {ai_state}", file=sys.stderr)
    print(f"listening on {settings.host}:{settings.port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
