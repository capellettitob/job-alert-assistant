"""Tests for the webhook, the buttons and the secrets. Telegram is faked: no network.

All jobs, names and ids are invented.
"""
import http.client
import io
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from bot import message
from bot.rules import load_config
from bot.server import App, make_server
from bot.settings import Settings, SettingsError
from bot.store import Store
from bot.telegram import Telegram, TelegramError, http_transport

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = load_config(os.path.join(ROOT, "config.example.json"))
# The invented jobs below use example.com links, so the tests allow that host. The default list is tested separately (LinkHosts).
CFG["allowed_link_hosts"] = ["example.com", "www.upwork.com", "upwork.com"]

CHAT = "424242"
API_KEY = "example-api-key-for-tests"
HOOK_SECRET = "example-hook-secret-for-tests"
TOKEN = "example-bot-token-for-tests"

GOOD = {"id": "~01demojobalpha01", "url": "https://example.com/jobs/1", "title": "Spreadsheet cleanup & report",
        "description": "Data cleanup for three sheets.", "payment": "fixed", "budget": 250, "proposals": 3}
WEAK = {"id": "~01demojobbravo02", "url": "https://example.com/jobs/2", "title": "Paint my fence",
        "description": "Two coats.", "payment": "fixed", "budget": 50}


class FakeTelegram:
    """Stands in for the Bot API and records every call."""

    def __init__(self):
        self.calls = []
        self.fail = 0
        self._next_id = 100

    def __call__(self, method, payload):
        self.calls.append((method, payload))
        if self.fail:
            self.fail -= 1
            raise TelegramError(f"{method}: HTTP 500")
        if method == "sendMessage":
            self._next_id += 1
            return {"ok": True, "result": {"message_id": self._next_id}}
        return {"ok": True, "result": True}

    def of(self, method):
        return [p for m, p in self.calls if m == method]


def tap(job_id, action, tapper=CHAT, chat=CHAT, callback_id="cb1"):
    return {"callback_query": {"id": callback_id, "from": {"id": int(tapper)},
                               "message": {"message_id": 101, "chat": {"id": int(chat)}},
                               "data": f"{action}:{job_id}"}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeTelegram()
        self.store = Store(os.path.join(self.tmp.name, "state.json"))
        self.app = App(CFG, self.store, Telegram(CHAT, self.fake))


class JobsFlow(Base):
    def test_a_good_job_gets_one_ping_with_buttons_and_is_remembered(self):
        status, out = self.app.handle_jobs({"jobs": [GOOD]})
        self.assertEqual((status, out["pinged"]), (200, [GOOD["id"]]))
        (ping,) = self.fake.of("sendMessage")
        self.assertEqual(ping["parse_mode"], "HTML")
        self.assertEqual(ping["chat_id"], CHAT)
        self.assertIn("Spreadsheet cleanup &amp; report", ping["text"])
        self.assertEqual([b["text"] for b in ping["reply_markup"]["inline_keyboard"][0]], ["Go", "Skip"])
        self.assertEqual(self.store.get(GOOD["id"])["status"], "pending")

    def test_the_same_job_twice_pings_once(self):
        self.app.handle_jobs({"jobs": [GOOD]})
        _, out = self.app.handle_jobs({"jobs": [GOOD]})
        self.assertEqual((out["pinged"], out["duplicates"]), ([], 1))
        self.assertEqual(len(self.fake.of("sendMessage")), 1)

    def test_a_job_the_rules_reject_gets_no_ping_and_a_reason(self):
        _, out = self.app.handle_jobs({"jobs": [WEAK]})
        self.assertEqual(out["pinged"], [])
        self.assertEqual(out["filtered"][0]["id"], WEAK["id"])
        self.assertIn("job type", out["filtered"][0]["reason"])
        self.assertEqual(self.fake.calls, [])
        # and it is not re-checked on the next post
        _, again = self.app.handle_jobs({"jobs": [WEAK]})
        self.assertEqual(again["duplicates"], 1)

    def test_one_bad_job_does_not_stop_the_others(self):
        bad = {"id": "x", "url": "http://nope", "title": ""}
        _, out = self.app.handle_jobs({"jobs": [bad, GOOD]})
        self.assertEqual(out["pinged"], [GOOD["id"]])
        self.assertEqual(out["invalid"][0]["index"], 0)

    def test_the_wrong_shape_or_too_many_jobs_is_refused(self):
        self.assertEqual(self.app.handle_jobs({"nope": 1})[0], 400)
        self.assertEqual(self.app.handle_jobs([GOOD])[0], 400)
        many = [dict(GOOD, id=f"~01demo{i:010d}") for i in range(21)]
        self.assertEqual(self.app.handle_jobs({"jobs": many})[0], 400)
        self.assertEqual(self.fake.calls, [])

    def test_if_telegram_fails_the_draft_is_kept_unsent_and_a_retry_pings_once(self):
        self.fake.fail = 1
        status, out = self.app.handle_jobs({"jobs": [GOOD]})
        self.assertEqual(status, 502)
        self.assertEqual(self.store.get(GOOD["id"])["status"], "unsent")
        status, out = self.app.handle_jobs({"jobs": [GOOD]})
        self.assertEqual((status, out["pinged"]), (200, [GOOD["id"]]))
        self.assertEqual(self.store.get(GOOD["id"])["status"], "pending")
        _, out = self.app.handle_jobs({"jobs": [GOOD]})
        self.assertEqual(out["duplicates"], 1)
        self.assertEqual(len([p for p in self.fake.of("sendMessage")]), 2, "one failed try, one ping")

    def test_link_previews_are_switched_off_the_current_way(self):
        self.app.handle_jobs({"jobs": [GOOD]})
        (ping,) = self.fake.of("sendMessage")
        self.assertEqual(ping["link_preview_options"], {"is_disabled": True})
        self.assertNotIn("disable_web_page_preview", ping)


class Unreadable(Base):
    """An alert email with no job the watcher could read: one silent note per email, never lost, never twice."""
    NOTE = {"id": "18f2a3b4c5d6e7f8", "subject": "New job alert: Inventory spreadsheet audit"}

    def test_a_silent_plain_note_is_posted_once_per_email(self):
        status, out = self.app.handle_jobs({"jobs": [], "unreadable": [self.NOTE]})
        self.assertEqual((status, out["noted"]), (200, [self.NOTE["id"]]))
        (note,) = self.fake.of("sendMessage")
        self.assertTrue(note["disable_notification"])
        self.assertNotIn("reply_markup", note)
        self.assertNotIn("parse_mode", note)
        self.assertIn("no job could be read from it: New job alert: Inventory spreadsheet audit", note["text"])
        _, again = self.app.handle_jobs({"jobs": [], "unreadable": [self.NOTE]})
        self.assertEqual((again["noted"], again["duplicates"]), ([], 1))
        self.assertEqual(len(self.fake.of("sendMessage")), 1)

    def test_if_telegram_fails_the_watcher_is_told_to_retry(self):
        self.fake.fail = 1
        status, _ = self.app.handle_jobs({"jobs": [], "unreadable": [self.NOTE]})
        self.assertEqual(status, 502)
        status, out = self.app.handle_jobs({"jobs": [], "unreadable": [self.NOTE]})
        self.assertEqual((status, out["noted"]), (200, [self.NOTE["id"]]))

    def test_bad_entries_are_refused_and_odd_subjects_are_tidied(self):
        bad = [{"id": "x y", "subject": "s"}, {"id": "abc"}, {"id": "abc", "subject": "  "}, "nope", {"id": "a" * 65, "subject": "s"}]
        _, out = self.app.handle_jobs({"jobs": [], "unreadable": bad + [{"id": "abc", "subject": "New job alert:\n\tTwo\x07lines " + "x" * 400}]})
        self.assertEqual(len(out["invalid"]), len(bad))
        (note,) = self.fake.of("sendMessage")
        self.assertIn("New job alert: Two lines xxx", note["text"])
        self.assertLessEqual(len(note["text"]), 600)
        self.assertEqual(self.app.handle_jobs({"jobs": [], "unreadable": [self.NOTE] * 21})[0], 400)

    def test_a_note_can_never_be_tapped(self):
        self.app.handle_jobs({"jobs": [], "unreadable": [self.NOTE]})
        self.fake.calls.clear()
        self.app.handle_update(tap("alert:" + self.NOTE["id"], "go"))
        self.assertEqual(self.fake.of("sendMessage"), [])
        self.assertEqual(self.fake.of("answerCallbackQuery")[0]["text"], "Unknown button")

    def test_state_survives_a_restart(self):
        self.app.handle_jobs({"jobs": [GOOD]})
        again = Store(self.store.path)
        self.assertEqual(again.get(GOOD["id"])["title"], GOOD["title"])


class LinkLock(Base):
    """With the default allowed_link_hosts the server refuses links the Gmail watcher would never have produced."""

    def setUp(self):
        super().setUp()
        self.app = App(load_config(os.path.join(ROOT, "config.example.json")), self.store, Telegram(CHAT, self.fake))

    def job(self, url, n):
        return dict(GOOD, id=f"~01demojoblock00{n}", url=url)

    def test_an_allowed_host_is_pinged(self):
        _, out = self.app.handle_jobs({"jobs": [self.job("https://www.upwork.com/jobs/~01demojoblock001", 1)]})
        self.assertEqual(out["pinged"], ["~01demojoblock001"])

    def test_foreign_lookalike_and_redirect_links_never_reach_telegram(self):
        bad = ["https://example.com/jobs/1", "https://upwork.com.evil.example/jobs/x", "https://www.upwork.com@evil.example/jobs/x",
               "https://track.example.com/click?u=https://evil.example/x", "http://www.upwork.com/jobs/x"]
        status, out = self.app.handle_jobs({"jobs": [self.job(u, i) for i, u in enumerate(bad)]})
        self.assertEqual(status, 200)
        self.assertEqual(out["pinged"], [])
        self.assertEqual(len(out["invalid"]), len(bad))
        reasons = [x["reason"] for x in out["invalid"]]
        self.assertEqual(sum("allowed_link_hosts" in r for r in reasons), 4)
        self.assertIn("https", reasons[4], "plain http is refused by the older https rule")
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(any(self.store.has(f"~01demojoblock00{i}") for i in range(len(bad))))

    def test_the_config_list_changes_what_is_allowed(self):
        cfg = dict(load_config(os.path.join(ROOT, "config.example.json")), allowed_link_hosts=["jobs.example.org"])
        app = App(cfg, self.store, Telegram(CHAT, self.fake))
        _, out = app.handle_jobs({"jobs": [self.job("https://jobs.example.org/j/1", 1), self.job("https://www.upwork.com/jobs/x", 2)]})
        self.assertEqual(out["pinged"], ["~01demojoblock001"])
        self.assertEqual(len(out["invalid"]), 1)


class Buttons(Base):
    def setUp(self):
        super().setUp()
        self.app.handle_jobs({"jobs": [GOOD]})
        self.fake.calls.clear()

    def test_go_posts_the_link_and_the_saved_draft_and_closes_the_buttons(self):
        status, _ = self.app.handle_update(tap(GOOD["id"], "go"))
        self.assertEqual(status, 200)
        (posted,) = self.fake.of("sendMessage")
        self.assertIn('href="https://example.com/jobs/1"', posted["text"])
        self.assertIn("Spreadsheet cleanup &amp; report", posted["text"])
        self.assertIn("<pre>", posted["text"])
        self.assertEqual(self.store.get(GOOD["id"])["status"], "go")
        (edit,) = self.fake.of("editMessageReplyMarkup")
        self.assertEqual(edit["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "noop:-")
        self.assertEqual(self.fake.of("answerCallbackQuery")[0]["text"], "Link and draft posted below")

    def test_tapping_go_twice_posts_only_once(self):
        self.app.handle_update(tap(GOOD["id"], "go"))
        self.app.handle_update(tap(GOOD["id"], "go", callback_id="cb2"))
        self.assertEqual(len(self.fake.of("sendMessage")), 1)
        self.assertEqual(self.fake.of("answerCallbackQuery")[-1]["text"], "Already go")

    def test_skip_closes_the_job_and_posts_no_draft(self):
        self.app.handle_update(tap(GOOD["id"], "skip"))
        self.assertEqual(self.store.get(GOOD["id"])["status"], "skip")
        self.assertEqual(self.fake.of("sendMessage"), [])
        self.app.handle_update(tap(GOOD["id"], "go", callback_id="cb2"))
        self.assertEqual(self.fake.of("sendMessage"), [], "Go after Skip does nothing")

    def test_a_tap_from_another_person_or_chat_is_refused(self):
        for who, where in (("999", CHAT), (CHAT, "999"), ("999", "999")):
            self.app.handle_update(tap(GOOD["id"], "go", tapper=who, chat=where))
        self.assertEqual(self.store.get(GOOD["id"])["status"], "pending")
        self.assertEqual(self.fake.of("sendMessage"), [])
        self.assertTrue(all(p["text"] == "Not allowed" for p in self.fake.of("answerCallbackQuery")))

    def test_odd_button_data_and_unknown_jobs_are_harmless(self):
        junk = tap("../../etc/passwd", "go")
        self.assertEqual(self.app.handle_update(junk)[0], 200)
        self.assertEqual(self.app.handle_update(tap("~01demoneverexisted", "go"))[0], 200)
        self.assertEqual(self.app.handle_update({"message": {"text": "hello"}})[0], 200)
        self.assertEqual(self.app.handle_update("not even an object")[0], 200)
        self.assertEqual(self.fake.of("sendMessage"), [])

    def test_if_telegram_fails_on_go_the_job_stays_pending_so_the_tap_can_be_repeated(self):
        self.fake.fail = 1
        self.app.handle_update(tap(GOOD["id"], "go"))
        self.assertEqual(self.store.get(GOOD["id"])["status"], "pending")
        self.app.handle_update(tap(GOOD["id"], "go", callback_id="cb2"))
        self.assertEqual(self.store.get(GOOD["id"])["status"], "go")


class Http(Base):
    """The real HTTP layer on a local port."""

    def setUp(self):
        super().setUp()
        self.settings = Settings(TOKEN, CHAT, API_KEY, HOOK_SECRET, "config.json", "state.json", "127.0.0.1", 0)
        self.server = make_server(self.settings, self.app)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = raw if raw is not None else (None if body is None else json.dumps(body))
        conn.request(method, path, body=data, headers=headers or {})
        r = conn.getresponse()
        text = r.read().decode()
        conn.close()
        return r.status, text

    def post_jobs(self, body, key=API_KEY):
        return self.request("POST", "/jobs", body, {"X-Api-Key": key, "Content-Type": "application/json"})

    def test_a_correct_key_works(self):
        status, text = self.post_jobs({"jobs": [GOOD]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text)["pinged"], [GOOD["id"]])

    def test_a_wrong_or_missing_key_is_refused_before_anything_happens(self):
        for headers in ({}, {"X-Api-Key": ""}, {"X-Api-Key": "wrong"}, {"X-Api-Key": API_KEY + "x"}, {"X-Api-Key": API_KEY[:-1]}):
            with self.subTest(headers=headers):
                status, _ = self.request("POST", "/jobs", {"jobs": [GOOD]}, headers)
                self.assertEqual(status, 401)
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(self.store.has(GOOD["id"]))

    def test_the_key_in_the_address_does_not_count(self):
        status, _ = self.request("POST", f"/jobs?k={API_KEY}", {"jobs": [GOOD]})
        self.assertEqual(status, 401)

    def test_telegram_door_needs_its_own_secret(self):
        update = tap(GOOD["id"], "go")
        self.assertEqual(self.request("POST", "/telegram", update, {"X-Telegram-Bot-Api-Secret-Token": API_KEY})[0], 401)
        self.assertEqual(self.request("POST", "/telegram", update)[0], 401)
        self.assertEqual(self.request("POST", "/telegram", update, {"X-Telegram-Bot-Api-Secret-Token": HOOK_SECRET})[0], 200)

    def test_the_jobs_key_does_not_open_the_telegram_door_and_the_reverse(self):
        self.assertEqual(self.request("POST", "/jobs", {"jobs": []}, {"X-Api-Key": HOOK_SECRET})[0], 401)

    def test_bad_bodies(self):
        h = {"X-Api-Key": API_KEY}
        self.assertEqual(self.request("POST", "/jobs", raw="{not json", headers=h)[0], 400)
        self.assertEqual(self.request("POST", "/jobs", raw="x" * (64 * 1024 + 1), headers=h)[0], 413)
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(self.request("POST", "/jobs", raw="[" * 50000, headers=h)[0], 400, "nested too deeply to read")

    def test_health_and_unknown_paths(self):
        self.assertEqual(self.request("GET", "/health"), (200, '{"ok": true}'))
        self.assertEqual(self.request("GET", "/jobs")[0], 404)
        self.assertEqual(self.request("POST", "/elsewhere", {})[0], 404)

    def test_no_secret_appears_in_any_reply_or_telegram_payload(self):
        replies = [self.post_jobs({"jobs": [GOOD]})[1], self.post_jobs({"jobs": [GOOD]}, key="wrong")[1],
                   self.request("POST", "/telegram", tap(GOOD["id"], "go"), {"X-Telegram-Bot-Api-Secret-Token": HOOK_SECRET})[1]]
        everything = "\n".join(replies) + json.dumps(self.fake.calls)
        for secret in (API_KEY, HOOK_SECRET, TOKEN):
            self.assertNotIn(secret, everything)


class StalledClient(unittest.TestCase):
    def test_a_client_that_stops_sending_is_cut_off(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = App(CFG, Store(os.path.join(tmp.name, "s.json")), Telegram(CHAT, FakeTelegram()))
        settings = Settings(TOKEN, CHAT, API_KEY, HOOK_SECRET, "config.json", "state.json", "127.0.0.1", 0)
        with mock.patch("bot.server.READ_TIMEOUT", 0.3), mock.patch("sys.stderr", io.StringIO()):
            server = make_server(settings, app)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            sock = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=3)
            sock.sendall(b"POST /jobs HTTP/1.1\r\nHost: x\r\n")   # and then nothing
            self.assertEqual(sock.recv(1), b"", "the server closed the connection")
            sock.close()
        self.assertEqual(server.RequestHandlerClass.timeout, 0.3)


class Secrets(unittest.TestCase):
    ENV = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT, "JAA_API_KEY": API_KEY,
           "TELEGRAM_WEBHOOK_SECRET": HOOK_SECRET}

    def test_settings_come_from_the_environment(self):
        s = Settings.from_env(self.ENV)
        self.assertEqual((s.chat_id, s.host, s.port), (CHAT, "127.0.0.1", 8080))

    def test_missing_variables_are_named_without_values(self):
        with self.assertRaises(SettingsError) as ctx:
            Settings.from_env({"TELEGRAM_BOT_TOKEN": TOKEN})
        text = str(ctx.exception)
        self.assertIn("JAA_API_KEY", text)
        self.assertNotIn(TOKEN, text)

    def test_weak_or_malformed_values_are_refused(self):
        for name, value in (("JAA_API_KEY", "short"), ("TELEGRAM_WEBHOOK_SECRET", "short"),
                            ("TELEGRAM_WEBHOOK_SECRET", "has spaces and is long enough"),
                            ("TELEGRAM_CHAT_ID", "not-a-number"), ("JAA_PORT", "eighty")):
            with self.subTest(name=name), self.assertRaises(SettingsError):
                Settings.from_env(dict(self.ENV, **{name: value}))

    def test_telegram_errors_never_contain_the_token_or_the_address(self):
        transport = http_transport(TOKEN)
        failures = [urllib.error.URLError("boom"), OSError("down"),
                    urllib.error.HTTPError("https://api.telegram.org/bot" + TOKEN + "/x", 401, "Unauthorized", {}, None)]
        for failure in failures:
            with self.subTest(failure=failure), mock.patch("urllib.request.urlopen", side_effect=failure):
                with self.assertRaises(TelegramError) as ctx:
                    transport("sendMessage", {})
                self.assertNotIn(TOKEN, str(ctx.exception))
                self.assertNotIn("api.telegram.org", str(ctx.exception))


class CheckCommand(unittest.TestCase):
    """python3 -m bot check: config, environment, then a real round trip to Telegram (faked here)."""

    def run_check(self, transport):
        from bot import __main__ as cli
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = dict(Secrets.ENV, JAA_CONFIG=os.path.join(ROOT, "config.example.json"), JAA_STATE_FILE=os.path.join(tmp.name, "s.json"))
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("bot.__main__.http_transport", return_value=transport), \
                mock.patch("sys.stdout", out):
            code = cli.main(["check"])
        return code, out.getvalue()

    def test_it_asks_telegram_who_the_bot_is_and_sends_one_test_message(self):
        fake = FakeTelegram()

        def transport(method, payload):
            if method == "getMe":
                fake.calls.append((method, payload))
                return {"ok": True, "result": {"username": "example_alert_bot"}}
            return fake(method, payload)

        code, out = self.run_check(transport)
        self.assertEqual(code, 0)
        self.assertEqual([m for m, _ in fake.calls], ["getMe", "sendMessage"])
        self.assertEqual(fake.of("sendMessage")[0]["chat_id"], CHAT)
        self.assertIn("test message", fake.of("sendMessage")[0]["text"])
        self.assertIn("@example_alert_bot", out)

    def test_a_wrong_token_fails_loudly_without_the_token(self):
        def refuse(method, payload):
            raise TelegramError(f"{method}: HTTP 401 Unauthorized")
        with self.assertRaises(SystemExit) as ctx:
            self.run_check(refuse)
        self.assertIn("getMe: HTTP 401", str(ctx.exception))
        self.assertIn("TELEGRAM_BOT_TOKEN", str(ctx.exception))
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_a_wrong_chat_fails_loudly(self):
        def chat_not_found(method, payload):
            if method == "getMe":
                return {"ok": True, "result": {"username": "example_alert_bot"}}
            raise TelegramError("sendMessage: HTTP 400 Bad Request: chat not found")
        with self.assertRaises(SystemExit) as ctx:
            self.run_check(chat_not_found)
        self.assertIn("chat not found", str(ctx.exception))
        self.assertIn("TELEGRAM_CHAT_ID", str(ctx.exception))


class Repository(unittest.TestCase):
    def test_env_example_holds_placeholders_only(self):
        with open(os.path.join(ROOT, ".env.example"), encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        self.assertTrue(lines)
        for line in lines:
            name, _, value = line.partition("=")
            self.assertTrue(value == "" or value.startswith("replace-with-") or value in ("127.0.0.1", "8080", "config.json", "state.json"), line)

    def test_gitignore_keeps_secrets_and_state_out(self):
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
            ignored = f.read().split()
        for entry in (".env", "config.json", "state.json"):
            self.assertIn(entry, ignored)


if __name__ == "__main__":
    unittest.main()
