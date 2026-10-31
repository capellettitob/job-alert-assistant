"""Tests for the vetting rules. All jobs here are invented."""
import copy
import json
import os
import unittest

from bot.rules import (DEFAULT_LINK_HOSTS, ConfigError, JobError, build_draft, link_hosts, load_config, parse_job,
                       score_job, url_host, validate_config)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(ROOT, "config.example.json")


def job(**over):
    raw = {"id": "~01demojobalpha01", "url": "https://example.com/jobs/1", "title": "Clean up a spreadsheet",
           "description": "", "payment": "fixed", "budget": 250, "proposals": 4, "client_rating": 4.9}
    raw.update(over)
    return parse_job(raw)


class ParseJob(unittest.TestCase):
    def test_a_good_job_parses(self):
        j = job()
        self.assertEqual((j.id, j.payment, j.budget, j.proposals), ("~01demojobalpha01", "fixed", 250.0, 4))

    def test_whitespace_in_the_title_is_tidied_and_long_text_is_cut(self):
        j = job(title="  Clean   up\n a  sheet ", description="word " * 1000)
        self.assertEqual(j.title, "Clean up a sheet")
        self.assertLessEqual(len(j.description), 2000)

    def test_optional_fields_may_be_missing_or_null(self):
        j = parse_job({"id": "~01demojobalpha01", "url": "https://example.com/j", "title": "x"})
        self.assertIsNone(j.budget)
        self.assertIsNone(j.proposals)
        self.assertIsNone(j.client_rating)

    def test_bad_input_is_refused(self):
        bad = [
            "not an object",
            {"id": "x", "url": "https://example.com/j", "title": "t"},                 # id too short
            {"id": "~01demojobalpha01", "url": "http://example.com/j", "title": "t"},  # not https
            {"id": "~01demojobalpha01", "url": "https://example.com/j\"onclick=1", "title": "t"},
            {"id": "~01demojobalpha01", "url": "https://example.com/j", "title": "  "},
            {"id": "~01demojobalpha01", "url": "https://example.com/j", "title": "t", "payment": "barter"},
            {"id": "~01demojobalpha01", "url": "https://example.com/j", "title": "t", "budget": "250"},
            {"id": "~01demojobalpha01", "url": "https://example.com/j", "title": "t", "budget": -5},
            {"id": "~01demojobalpha01", "url": "https://example.com/j", "title": "t", "budget": True},
            {"id": "~01demojobalpha01", "url": "https://example.com/j", "title": "t", "proposals": 2.5},
            {"id": "~01demojobalpha01", "url": "https://example.com/j", "title": "t", "client_rating": 9},
        ]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(JobError):
                parse_job(raw)


class LinkHosts(unittest.TestCase):
    """The server's lock on job links: the second check behind the Gmail watcher's."""
    JOB_ID = "~01demojoblink001"

    def raw(self, url):
        return {"id": self.JOB_ID, "url": url, "title": "Spreadsheet cleanup"}

    def test_the_default_hosts(self):
        self.assertEqual(DEFAULT_LINK_HOSTS, ("www.upwork.com", "upwork.com"))
        self.assertEqual(link_hosts({}), DEFAULT_LINK_HOSTS)
        self.assertEqual(link_hosts(load_config(EXAMPLE)), DEFAULT_LINK_HOSTS)

    def test_the_config_list_replaces_the_default_and_is_lower_cased(self):
        self.assertEqual(link_hosts({"allowed_link_hosts": ["Jobs.Example.org"]}), ("jobs.example.org",))

    def test_allowed_hosts_pass(self):
        for url in ("https://www.upwork.com/jobs/" + self.JOB_ID, "https://upwork.com/jobs/" + self.JOB_ID,
                    "https://WWW.UPWORK.COM/jobs/" + self.JOB_ID + "?x=1&y=2", "https://www.upwork.com"):
            with self.subTest(url=url):
                self.assertEqual(parse_job(self.raw(url), DEFAULT_LINK_HOSTS).url, url)

    def test_foreign_and_lookalike_hosts_are_refused(self):
        bad = [
            "https://jobs.evil.example/jobs/" + self.JOB_ID,
            "https://upwork.com.evil.example/jobs/" + self.JOB_ID,
            "https://www.upwork.com.evil.example/jobs/" + self.JOB_ID,
            "https://evilupwork.com/jobs/" + self.JOB_ID,
            "https://www-upwork.com/jobs/" + self.JOB_ID,
            "https://www.upwork.com./jobs/" + self.JOB_ID,
            "https://evil.example/www.upwork.com/jobs/" + self.JOB_ID,
            "https://evil.example/?next=https://www.upwork.com/jobs/" + self.JOB_ID,
        ]
        for url in bad:
            with self.subTest(url=url), self.assertRaises(JobError) as ctx:
                parse_job(self.raw(url), DEFAULT_LINK_HOSTS)
            self.assertIn("allowed_link_hosts", str(ctx.exception))

    def test_tracking_redirects_are_refused_even_when_they_point_at_an_allowed_host(self):
        # The server never looks inside a link: the host it sees is the tracker's, which is not on the list.
        url = "https://track.example.com/click?u=https://www.upwork.com/jobs/" + self.JOB_ID
        with self.assertRaises(JobError):
            parse_job(self.raw(url), DEFAULT_LINK_HOSTS)

    def test_user_info_port_backslash_and_plain_http_are_refused(self):
        for url in ("https://www.upwork.com@evil.example/jobs/" + self.JOB_ID, "https://www.upwork.com:8443/jobs/" + self.JOB_ID,
                    "https://evil.example\\@www.upwork.com/jobs/" + self.JOB_ID, "https://user:pw@www.upwork.com/x"):
            with self.subTest(url=url), self.assertRaises(JobError):
                parse_job(self.raw(url), DEFAULT_LINK_HOSTS)
        with self.assertRaises(JobError):
            parse_job(self.raw("http://www.upwork.com/jobs/" + self.JOB_ID), DEFAULT_LINK_HOSTS)

    def test_without_a_host_list_nothing_is_checked(self):
        self.assertEqual(parse_job(self.raw("https://anything.example/x")).url, "https://anything.example/x")

    def test_url_host(self):
        self.assertEqual(url_host("https://WWW.Upwork.com/jobs/x"), "www.upwork.com")
        for odd in ("http://upwork.com/", "https://upwork.com:1/", "https://a@upwork.com/", "upwork.com", ""):
            self.assertEqual(url_host(odd), "", odd)

    def test_bad_config_values_are_refused(self):
        with open(EXAMPLE, encoding="utf-8") as f:
            good = json.load(f)
        for bad in ([], "www.upwork.com", [""], [5], ["https://www.upwork.com"], ["www.upwork.com/jobs"], ["bad host"], ["-x.com"], {"a": 1}):
            cfg = copy.deepcopy(good)
            cfg["allowed_link_hosts"] = bad
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                validate_config(cfg)
        cfg = copy.deepcopy(good)
        cfg["allowed_link_hosts"] = ["Jobs.Example.org", "example.com"]
        validate_config(cfg)
        del cfg["allowed_link_hosts"]
        validate_config(cfg)


class Scoring(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(EXAMPLE)

    def status(self, verdict, name):
        return next(c.status for c in verdict.checks if c.name == name)

    def test_a_strong_job_is_pinged_with_score_one(self):
        v = score_job(job(title="Spreadsheet dashboard for sales"), self.cfg)
        self.assertTrue(v.ping)
        self.assertEqual(v.score, 1.0)
        self.assertEqual(v.matched_skills, ["spreadsheet", "dashboard"])

    def test_the_job_type_check_is_a_gate(self):
        v = score_job(job(title="Paint my fence", description="Needs two coats"), self.cfg)
        self.assertFalse(v.ping)
        self.assertEqual(self.status(v, "job_type"), "fail")
        self.assertIn("job type", v.reason)

    def test_an_avoided_word_blocks_an_otherwise_matching_job(self):
        v = score_job(job(title="Spreadsheet for a gambling site"), self.cfg)
        self.assertFalse(v.ping)
        self.assertIn("gambling", v.reason)

    def test_skills_match_whole_words_only(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["profile"]["skills"] = ["sql"]
        self.assertFalse(score_job(job(title="Fix my MySQL server"), cfg).ping)
        self.assertTrue(score_job(job(title="Write a SQL report"), cfg).ping)

    def test_min_skill_matches_is_respected(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["rules"]["min_skill_matches"] = 2
        self.assertFalse(score_job(job(title="A spreadsheet task"), cfg).ping)
        self.assertTrue(score_job(job(title="A spreadsheet and dashboard task"), cfg).ping)

    def test_budget_minimum_depends_on_the_payment_type(self):
        self.assertEqual(self.status(score_job(job(payment="fixed", budget=99), self.cfg), "budget"), "fail")
        self.assertEqual(self.status(score_job(job(payment="fixed", budget=100), self.cfg), "budget"), "pass")
        self.assertEqual(self.status(score_job(job(payment="hourly", budget=19), self.cfg), "budget"), "fail")
        self.assertEqual(self.status(score_job(job(payment="hourly", budget=20), self.cfg), "budget"), "pass")

    def test_competition_limit(self):
        self.assertEqual(self.status(score_job(job(proposals=15), self.cfg), "competition"), "pass")
        self.assertEqual(self.status(score_job(job(proposals=16), self.cfg), "competition"), "fail")

    def test_client_rating_minimum(self):
        self.assertEqual(self.status(score_job(job(client_rating=4.5), self.cfg), "client_rating"), "pass")
        self.assertEqual(self.status(score_job(job(client_rating=4.4), self.cfg), "client_rating"), "fail")

    def test_unknown_fields_are_neutral_not_failures(self):
        v = score_job(job(budget=None, proposals=None, client_rating=None), self.cfg)
        self.assertEqual([c.status for c in v.checks], ["pass", "unknown", "unknown", "unknown"])
        self.assertEqual(v.score, 1.0)
        self.assertTrue(v.ping)

    def test_score_is_the_weight_of_passed_checks_over_known_weight(self):
        # weights 3, 2, 2, 1: job type and budget pass, competition fails, rating unknown
        v = score_job(job(proposals=40, client_rating=None), self.cfg)
        self.assertAlmostEqual(v.score, 5 / 7)
        self.assertTrue(v.ping)

    def test_a_low_score_is_not_pinged_and_says_why(self):
        v = score_job(job(budget=10, proposals=40, client_rating=3.0), self.cfg)   # 3 of 8 = 0.375
        self.assertFalse(v.ping)
        self.assertAlmostEqual(v.score, 3 / 8)
        self.assertIn("under", v.reason)

    def test_threshold_is_inclusive(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["rules"]["min_score_to_ping"] = 5 / 8   # job type and budget pass, competition and rating fail
        self.assertTrue(score_job(job(proposals=40, client_rating=3.0), cfg).ping)

    def test_a_known_budget_under_the_minimum_is_a_gate(self):
        # Alert emails rarely give the proposal count or the rating, so job type 3 + budget 2 decide the score:
        # 3 of 5 = 0.6 used to reach the sample threshold. The gate stops that.
        v = score_job(job(payment="fixed", budget=40, proposals=None, client_rating=None), self.cfg)
        self.assertAlmostEqual(v.score, 0.6)
        self.assertFalse(v.ping)
        self.assertEqual(v.reason, "budget: 40 fixed is under your minimum of 100")
        cfg = copy.deepcopy(self.cfg)
        cfg["rules"]["min_score_to_ping"] = 0
        self.assertFalse(score_job(job(payment="hourly", budget=5), cfg).ping, "even with no score threshold")

    def test_an_unknown_budget_does_not_block(self):
        for over in ({"budget": None}, {"payment": None}, {"payment": None, "budget": None}):
            with self.subTest(over=over):
                v = score_job(job(proposals=None, client_rating=None, **over), self.cfg)
                self.assertEqual(self.status(v, "budget"), "unknown")
                self.assertTrue(v.ping)

    def test_the_job_type_gate_is_checked_before_the_budget_gate(self):
        v = score_job(job(title="Paint my fence", budget=5), self.cfg)
        self.assertIn("job type", v.reason)


class Draft(unittest.TestCase):
    def test_the_template_is_filled_with_title_and_matched_skills(self):
        cfg = load_config(EXAMPLE)
        j = job(title="Spreadsheet cleanup")
        text = build_draft(j, score_job(j, cfg), cfg)
        self.assertIn('"Spreadsheet cleanup"', text)
        self.assertIn("spreadsheet", text)
        self.assertNotIn("$title", text)
        self.assertNotIn("$skills", text)

    def test_other_dollar_signs_in_the_template_do_not_crash(self):
        cfg = load_config(EXAMPLE)
        cfg["draft_template"] = "My rate starts at $50. About $title."
        j = job(title="Spreadsheet cleanup")
        self.assertEqual(build_draft(j, score_job(j, cfg), cfg), "My rate starts at $50. About Spreadsheet cleanup.")


class Config(unittest.TestCase):
    def test_the_example_config_is_valid(self):
        self.assertIn("draft_template", load_config(EXAMPLE))

    def test_example_values_are_samples_not_personal(self):
        with open(EXAMPLE, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("@", text)
        self.assertNotIn("http", text)

    def test_missing_and_broken_files_give_plain_errors(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(os.path.join(ROOT, "no-such-file.json"))
        self.assertIn("config.example.json", str(ctx.exception))

    def test_bad_values_are_refused(self):
        with open(EXAMPLE, encoding="utf-8") as f:
            good = json.load(f)
        edits = [
            lambda c: c["profile"].update(skills=[]),
            lambda c: c["profile"].update(skills=["ok", ""]),
            lambda c: c["rules"].update(min_skill_matches=0),
            lambda c: c["rules"]["min_budget"].pop("hourly"),
            lambda c: c["rules"].update(max_proposals=-1),
            lambda c: c["rules"].update(min_client_rating=6),
            lambda c: c["rules"]["weights"].update(job_type=0),
            lambda c: c["rules"]["weights"].pop("budget"),
            lambda c: c["rules"].update(min_score_to_ping=1.5),
            lambda c: c.update(draft_template=" "),
            lambda c: c.pop("rules"),
        ]
        for edit in edits:
            cfg = copy.deepcopy(good)
            edit(cfg)
            with self.subTest(cfg=cfg), self.assertRaises(ConfigError):
                validate_config(cfg)


if __name__ == "__main__":
    unittest.main()
