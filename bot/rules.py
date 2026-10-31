"""Vetting rules: score one job against a profile.

Four checks, each with a weight from the config:

  job_type      the post mentions at least N of your profile skills, and none of
                the words you want to avoid. This one is a gate: without it the
                job is never pinged, whatever the rest says.
  budget        the budget reaches your minimum for that payment type.
  competition   few enough proposals so far.
  client_rating the client's rating reaches your minimum.

A check is "pass", "fail" or "unknown". Unknown means the post did not say
(job alert emails often leave out the proposal count and the client rating).
Unknown checks are left out of the score instead of counting against the job,
and the ping says so, so you can look it up yourself.

score = weight of passed checks / weight of known checks, from 0 to 1.
A job is pinged when the job_type gate passes and score >= min_score_to_ping.

Standard library only.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

PAYMENT_TYPES = ("fixed", "hourly")
CHECK_NAMES = ("job_type", "budget", "competition", "client_rating")

JOB_ID_RE = re.compile(r"^[~0-9A-Za-z_-]{6,40}$")
DEFAULT_LINK_HOSTS = ("www.upwork.com", "upwork.com")
# Plain https://host/... only: no port, no user info (user@host), no backslash, no odd characters in the host.
_LINK_HOST_RE = re.compile(r"^https://([A-Za-z0-9.-]+)(?:[/?#]|$)")
_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
MAX_TITLE = 200
MAX_DESCRIPTION = 2000


class ConfigError(ValueError):
    """The config file is missing something or has a value that makes no sense."""


class JobError(ValueError):
    """A job posted to the webhook is malformed."""


@dataclass(frozen=True)
class Job:
    id: str
    url: str
    title: str
    description: str = ""
    payment: Optional[str] = None
    budget: Optional[float] = None
    proposals: Optional[int] = None
    client_rating: Optional[float] = None


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # "pass", "fail" or "unknown"
    detail: str
    weight: float


@dataclass(frozen=True)
class Verdict:
    ping: bool
    score: Optional[float]
    checks: List[CheckResult]
    matched_skills: List[str] = field(default_factory=list)
    reason: str = ""


# ---------- jobs ----------

def _number(value: Any, name: str) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JobError(f"{name} must be a number or null")
    if value < 0 or value != value:
        raise JobError(f"{name} must be 0 or more")
    return float(value)


def url_host(url: str) -> str:
    """The lower-case host of a plain https link; '' for anything else (so it never matches an allowed host)."""
    m = _LINK_HOST_RE.match(url)
    return m.group(1).lower() if m else ""


def link_hosts(cfg: Dict[str, Any]) -> tuple:
    """The hosts a job link may point to: allowed_link_hosts from the config, else the default."""
    hosts = cfg.get("allowed_link_hosts")
    return DEFAULT_LINK_HOSTS if hosts is None else tuple(h.lower() for h in hosts)


def parse_job(raw: Any, allowed_hosts: Optional[Sequence[str]] = None) -> Job:
    """Check one posted job and turn it into a Job. Raises JobError.

    allowed_hosts: when given, the job's url must be on one of these hosts (the server always passes it).
    """
    if not isinstance(raw, dict):
        raise JobError("a job must be an object")
    job_id = raw.get("id")
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        raise JobError("id must be 6 to 40 letters, digits, ~, _ or -")
    url = raw.get("url")
    if not isinstance(url, str) or not url.startswith("https://") or len(url) > 200 or re.search(r"[\s\"'<>]", url):
        raise JobError("url must be an https link")
    if allowed_hosts is not None and url_host(url) not in allowed_hosts:
        raise JobError("the url is not on a host in allowed_link_hosts")
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise JobError("title is missing")
    description = raw.get("description") or ""
    if not isinstance(description, str):
        raise JobError("description must be text")
    payment = raw.get("payment") or None
    if payment is not None and payment not in PAYMENT_TYPES:
        raise JobError("payment must be fixed, hourly or null")
    budget = _number(raw.get("budget"), "budget")
    proposals = _number(raw.get("proposals"), "proposals")
    if proposals is not None and proposals != int(proposals):
        raise JobError("proposals must be a whole number")
    rating = _number(raw.get("client_rating"), "client_rating")
    if rating is not None and rating > 5:
        raise JobError("client_rating must be from 0 to 5")
    return Job(
        id=job_id,
        url=url,
        title=" ".join(title.split())[:MAX_TITLE],
        description=" ".join(description.split())[:MAX_DESCRIPTION],
        payment=payment,
        budget=budget,
        proposals=None if proposals is None else int(proposals),
        client_rating=rating,
    )


# ---------- config ----------

def validate_config(cfg: Any) -> Dict[str, Any]:
    """Return the config if it is usable, else raise ConfigError with the reason."""
    if not isinstance(cfg, dict):
        raise ConfigError("the config must be a JSON object")
    profile, rules = cfg.get("profile"), cfg.get("rules")
    if not isinstance(profile, dict) or not isinstance(rules, dict):
        raise ConfigError("the config needs a 'profile' and a 'rules' section")
    for key in ("skills", "avoid"):
        words = profile.get(key, [] if key == "avoid" else None)
        if not isinstance(words, list) or not all(isinstance(w, str) and w.strip() for w in words):
            raise ConfigError(f"profile.{key} must be a list of words")
    if not profile["skills"]:
        raise ConfigError("profile.skills needs at least one skill")
    if not isinstance(rules.get("min_skill_matches"), int) or rules["min_skill_matches"] < 1:
        raise ConfigError("rules.min_skill_matches must be a whole number, 1 or more")
    mins = rules.get("min_budget")
    if not isinstance(mins, dict) or set(mins) != set(PAYMENT_TYPES) or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 for v in mins.values()):
        raise ConfigError("rules.min_budget needs a number for fixed and for hourly")
    if not isinstance(rules.get("max_proposals"), int) or rules["max_proposals"] < 0:
        raise ConfigError("rules.max_proposals must be a whole number, 0 or more")
    rating = rules.get("min_client_rating")
    if not isinstance(rating, (int, float)) or isinstance(rating, bool) or not 0 <= rating <= 5:
        raise ConfigError("rules.min_client_rating must be from 0 to 5")
    weights = rules.get("weights")
    if not isinstance(weights, dict) or set(weights) != set(CHECK_NAMES) or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 for v in weights.values()):
        raise ConfigError("rules.weights needs a number of 0 or more for " + ", ".join(CHECK_NAMES))
    if weights["job_type"] <= 0:
        raise ConfigError("rules.weights.job_type must be above 0")
    threshold = rules.get("min_score_to_ping")
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not 0 <= threshold <= 1:
        raise ConfigError("rules.min_score_to_ping must be from 0 to 1")
    hosts = cfg.get("allowed_link_hosts")
    if hosts is not None and (not isinstance(hosts, list) or not hosts or not all(
            isinstance(h, str) and _HOSTNAME_RE.match(h.lower()) for h in hosts)):
        raise ConfigError("allowed_link_hosts must be a list of host names such as www.example.com (no https://, no path)")
    if not isinstance(cfg.get("draft_template"), str) or not cfg["draft_template"].strip():
        raise ConfigError("draft_template must be text")
    return cfg


def load_config(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path} (copy config.example.json to config.json)")
    except json.JSONDecodeError as err:
        raise ConfigError(f"{path} is not valid JSON: {err}")
    return validate_config(cfg)


# ---------- scoring ----------

def _find_words(words: List[str], text: str) -> List[str]:
    """The words that appear in the text as whole words, without regard to case."""
    found = []
    for w in words:
        if re.search(r"(?<![0-9A-Za-z])" + re.escape(w.strip()) + r"(?![0-9A-Za-z])", text, re.I):
            found.append(w.strip())
    return found


def _money(value: float) -> str:
    return f"{value:g}"


def score_job(job: Job, cfg: Dict[str, Any]) -> Verdict:
    """Run the four checks and decide whether the job is worth a ping."""
    profile, rules = cfg["profile"], cfg["rules"]
    weights = rules["weights"]
    text = job.title + " " + job.description
    checks: List[CheckResult] = []

    matched = _find_words(profile["skills"], text)
    avoided = _find_words(profile.get("avoid", []), text)
    if avoided:
        checks.append(CheckResult("job_type", "fail", "mentions " + ", ".join(avoided) + ", which you avoid", weights["job_type"]))
    elif len(matched) >= rules["min_skill_matches"]:
        checks.append(CheckResult("job_type", "pass", "matches " + ", ".join(matched), weights["job_type"]))
    else:
        need = rules["min_skill_matches"]
        checks.append(CheckResult("job_type", "fail", f"matches {len(matched)} of your skills, needs {need}", weights["job_type"]))

    if job.budget is None or job.payment is None:
        checks.append(CheckResult("budget", "unknown", "budget or payment type not in the post", weights["budget"]))
    else:
        floor = rules["min_budget"][job.payment]
        label = "an hour" if job.payment == "hourly" else "fixed"
        status = "pass" if job.budget >= floor else "fail"
        word = "reaches" if status == "pass" else "is under"
        checks.append(CheckResult("budget", status, f"{_money(job.budget)} {label} {word} your minimum of {_money(floor)}", weights["budget"]))

    if job.proposals is None:
        checks.append(CheckResult("competition", "unknown", "proposal count not in the post", weights["competition"]))
    else:
        status = "pass" if job.proposals <= rules["max_proposals"] else "fail"
        checks.append(CheckResult("competition", status, f"{job.proposals} proposals so far, your limit is {rules['max_proposals']}", weights["competition"]))

    if job.client_rating is None:
        checks.append(CheckResult("client_rating", "unknown", "client rating not in the post", weights["client_rating"]))
    else:
        status = "pass" if job.client_rating >= rules["min_client_rating"] else "fail"
        checks.append(CheckResult("client_rating", status, f"rated {job.client_rating:g}, your minimum is {rules['min_client_rating']:g}", weights["client_rating"]))

    known = [c for c in checks if c.status != "unknown"]
    known_weight = sum(c.weight for c in known)
    score = None if known_weight == 0 else sum(c.weight for c in known if c.status == "pass") / known_weight

    gate = checks[0]
    if gate.status != "pass":
        return Verdict(False, score, checks, matched, "job type: " + gate.detail)
    if score is not None and score < rules["min_score_to_ping"]:
        return Verdict(False, score, checks, matched, f"score {score:.2f} is under {rules['min_score_to_ping']:g}")
    return Verdict(True, score, checks, matched, "")


def build_draft(job: Job, verdict: Verdict, cfg: Dict[str, Any]) -> str:
    """Fill the draft template. $title and $skills are the only placeholders."""
    from string import Template

    skills = ", ".join(verdict.matched_skills) or "this kind of work"
    return Template(cfg["draft_template"]).safe_substitute(title=job.title, skills=skills).strip()
