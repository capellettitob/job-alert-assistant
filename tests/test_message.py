"""Tests for the Telegram message builder. All data is invented."""
import os
import re
import unittest

from bot import message
from bot.rules import build_draft, load_config, parse_job, score_job

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = load_config(os.path.join(ROOT, "config.example.json"))

# Every entity Python's html.escape can produce; anything else after "&" is a broken cut.
ENTITY = re.compile(r"&(amp|lt|gt|quot|#x27);")


def make(**over):
    raw = {"id": "~01demojobalpha01", "url": "https://example.com/jobs/1", "title": "Spreadsheet cleanup",
           "description": "", "payment": "fixed", "budget": 250, "proposals": 4, "client_rating": 4.9}
    raw.update(over)
    j = parse_job(raw)
    v = score_job(j, CFG)
    return j, v


def no_broken_entities(text):
    return ENTITY.sub("", text).count("&") == 0


class Escaping(unittest.TestCase):
    def test_markup_in_the_title_and_draft_is_escaped(self):
        j, v = make(title="Spreadsheet <b>bold</b> & <script>alert(1)</script>")
        text = message.render_ping(j, v, "Hi <i>there</i> & welcome")
        self.assertNotIn("<script>", text)
        self.assertNotIn("<i>there</i>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("&amp; welcome", text)
        # the only tags left are the ones the builder wrote itself
        self.assertEqual(sorted(set(re.findall(r"</?(\w+)", text))), ["a", "b", "pre"])

    def test_the_link_cannot_break_out_of_its_attribute(self):
        j, v = make(url="https://example.com/a?x=1&y=2")
        text = message.render_ping(j, v, "draft")
        self.assertIn('href="https://example.com/a?x=1&amp;y=2"', text)

    def test_plain_output_is_not_escaped(self):
        j, v = make(title="Spreadsheet & dashboard")
        text = message.render_ping(j, v, "A & B", as_html=False)
        self.assertIn("Spreadsheet & dashboard", text)
        self.assertNotIn("<b>", text)
        self.assertNotIn("&amp;", text)


class Length(unittest.TestCase):
    def check_fits(self, **over):
        draft = over.pop("draft")
        j, v = make(**over)
        text = message.render_ping(j, v, draft)
        self.assertLessEqual(len(text), message.TELEGRAM_LIMIT)
        self.assertTrue(no_broken_entities(text), "a cut left a half entity")
        return text

    def test_a_normal_message_is_short_and_whole(self):
        j, v = make()
        text = message.render_ping(j, v, build_draft(j, v, CFG))
        self.assertLess(len(text), 1500)
        self.assertNotIn("(cut)", text)

    def test_a_huge_draft_is_cut_to_fit(self):
        text = self.check_fits(draft="word " * 5000)
        self.assertIn("(cut)", text)

    def test_worst_case_characters_never_break_the_limit(self):
        # "&" grows five times when escaped, the worst case for the length.
        amp = "&" * 6000
        self.check_fits(draft=amp, title="&" * 400, url="https://example.com/" + "&" * 170,
                        description="&" * 3000)

    def test_the_go_message_also_fits(self):
        text = message.render_go("&" * 400, "https://example.com/" + "&" * 170, "<" * 9000)
        self.assertLessEqual(len(text), message.TELEGRAM_LIMIT)
        self.assertTrue(no_broken_entities(text))

    def test_fit_measures_after_escaping(self):
        cut = message.fit("a&b " * 100, 50)
        self.assertLessEqual(len(message.esc(cut)), 50)


class Buttons(unittest.TestCase):
    def test_two_buttons_go_and_skip(self):
        kb = message.keyboard("~01demojobalpha01")
        row = kb["inline_keyboard"][0]
        self.assertEqual([b["text"] for b in row], ["Go", "Skip"])
        self.assertEqual([b["callback_data"] for b in row], ["go:~01demojobalpha01", "skip:~01demojobalpha01"])

    def test_button_data_fits_telegrams_64_byte_limit_for_the_longest_id(self):
        row = message.keyboard("~" + "a" * 39)["inline_keyboard"][0]
        self.assertTrue(all(len(b["callback_data"].encode()) <= 64 for b in row))

    def test_callbacks_parse_and_junk_does_not(self):
        self.assertEqual(message.parse_callback("go:~01demojobalpha01"), ("go", "~01demojobalpha01"))
        self.assertEqual(message.parse_callback("skip:abc123"), ("skip", "abc123"))
        for junk in (None, 5, "", "go:", "go", "drop:abc123", "go:../etc", "go:a b", "go:" + "a" * 41):
            with self.subTest(junk=junk):
                self.assertIsNone(message.parse_callback(junk))

    def test_closed_buttons_do_nothing_when_tapped(self):
        for status in ("go", "skip"):
            data = message.closed_keyboard(status)["inline_keyboard"][0][0]["callback_data"]
            self.assertEqual(message.parse_callback(data)[0], "noop")


class Layout(unittest.TestCase):
    def test_the_ping_names_the_checks_and_the_unknowns(self):
        j, v = make(client_rating=None, proposals=None)
        text = message.render_ping(j, v, "draft", as_html=False)
        for label in ("Job type: pass", "Budget: pass", "Competition: unknown", "Client rating: unknown"):
            self.assertIn(label, text)
        self.assertIn("Score: 100 out of 100", text)

    def test_the_draft_is_in_a_copyable_block(self):
        j, v = make()
        self.assertIn("<pre>my draft</pre>", message.render_ping(j, v, "my draft"))

    def test_go_message_has_the_link_and_the_draft(self):
        text = message.render_go("Spreadsheet cleanup", "https://example.com/jobs/1", "my draft")
        self.assertIn('<a href="https://example.com/jobs/1">', text)
        self.assertIn("<pre>my draft</pre>", text)


if __name__ == "__main__":
    unittest.main()
