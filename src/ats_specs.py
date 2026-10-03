"""Per-ATS knowledge for discovery: how to spot a board in any text (URL
patterns), how to confirm it exists (one API call), and where it's stored
(config/<ats>.yaml). discover.py feeds text from many sources through these.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import unquote

import requests
import yaml

from .config import CONFIG_DIR


HEADERS = {"User-Agent": "Mozilla/5.0 (job-tracker-discover)"}


MAX_RETRIES = 4


INITIAL_BACKOFF = 2


def request_json(method: str, url: str, *, payload: dict | None = None,
                  params: dict | None = None, timeout: int = 25):
    """Return parsed JSON, or None on a definitive miss (4xx) / repeated failure."""
    backoff = INITIAL_BACKOFF
    headers = dict(HEADERS)
    if payload is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.request(method, url, json=payload, params=params,
                                    headers=headers, timeout=timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.RequestException(f"HTTP {resp.status_code}")
            if resp.status_code != 200:
                return None
            return resp.json()
        except (requests.RequestException, ValueError):
            if attempt == MAX_RETRIES - 1:
                return None
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
    return None


_WD_RE = re.compile(
    r"https?://(?P<tenant>[a-z0-9-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com"
    r"/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)


_WD_BAD_SITES = {"wday", "cxs", "assets", "static", "login", "fonts"}


# Workday's alternate host puts the tenant in the path; the same tenant/site
# is served by the regular {tenant}.{wd}.myworkdayjobs.com API.
_WD_SITE_RE = re.compile(
    r"https?://(?P<wd>wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/"
    r"(?P<tenant>[a-z0-9-]+)/(?P<site>[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)


_ORACLE_RE = re.compile(
    r"https?://(?P<host>[a-z0-9-]+\.fa(?:\.[a-z0-9-]+)?\.oraclecloud\.com)"
    r"/hcmUI/CandidateExperience/[a-z-]+/sites/(?P<site>[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)


_ICIMS_RE = re.compile(r"https?://(?P<host>[a-z0-9-]+\.icims\.com)/jobs", re.IGNORECASE)


_JIBE_RE = re.compile(r"https?://(?P<host>(?:[\w-]+\.)+[a-z]{2,})/jobs/\d+/?\?(?:[^#\s]*&)?icims=1",
                      re.IGNORECASE)


_RIPPLING_RE = re.compile(r"ats\.rippling\.com/(?P<slug>[\w-]+)/jobs", re.IGNORECASE)


_ICIMS_BAD = {"www", "api", "developer", "community", "care", "status"}


_GH_RES = [
    re.compile(r"boards(?:-api)?\.greenhouse\.io(?:/v1/boards)?/([a-z0-9_-]+)", re.I),
    re.compile(r"job-boards(?:\.eu)?\.greenhouse\.io/([a-z0-9_-]+)", re.I),
    re.compile(r"greenhouse\.io/embed/job_board\?[^\s\"'<>]*?for=([a-z0-9_-]+)", re.I),
]


_GH_BAD = {"v1", "boards", "jobs", "embed", "js", "generic", "internal"}


_LV_RE = re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)", re.I)


_ASHBY_RE = re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I)


_ASHBY_BAD = {"api"}


_SR_RE = re.compile(
    r"(?:jobs|careers)\.smartrecruiters\.com/(?:oneclick-ui/company/)?([A-Za-z0-9]+)",
    re.I,
)


_SR_BAD = {"sitemap", "favicon"}


def _extract_workday(text: str) -> list[dict]:
    out = []
    for m in list(_WD_RE.finditer(text)) + list(_WD_SITE_RE.finditer(text)):
        site = m.group("site")
        if site.lower() in _WD_BAD_SITES:
            continue
        out.append({"tenant": m.group("tenant").lower(), "wd": m.group("wd").lower(),
                    "site": site, "name": m.group("tenant").lower()})
    return out


def _extract_greenhouse(text: str) -> list[dict]:
    out = []
    for rx in _GH_RES:
        for m in rx.finditer(text):
            token = m.group(1).lower()
            if token not in _GH_BAD:
                out.append({"token": token, "name": token})
    return out


def _extract_lever(text: str) -> list[dict]:
    return [{"slug": m.group(1).lower(), "name": m.group(1).lower()}
            for m in _LV_RE.finditer(text)]


def _extract_ashby(text: str) -> list[dict]:
    out = []
    for m in _ASHBY_RE.finditer(text):
        slug = m.group(1).rstrip(".")
        if slug.lower() in _ASHBY_BAD:
            continue
        out.append({"slug": slug, "name": unquote(slug)})
    return out


def _extract_smartrecruiters(text: str) -> list[dict]:
    out = []
    for m in _SR_RE.finditer(text):
        company = m.group(1)
        if company.lower() in _SR_BAD:
            continue
        out.append({"company": company, "name": company})
    return out


def _extract_oracle(text: str) -> list[dict]:
    return [{"host": m.group("host").lower(), "site": m.group("site"),
             "name": m.group("host").split(".")[0].lower()}
            for m in _ORACLE_RE.finditer(text)]


def _extract_icims(text: str) -> list[dict]:
    out = []
    for m in _ICIMS_RE.finditer(text):
        host = m.group("host").lower()
        if host.split(".")[0] not in _ICIMS_BAD:
            out.append({"host": host, "name": host.split(".")[0]})
    return out


def _extract_jibe(text: str) -> list[dict]:
    out = []
    for m in _JIBE_RE.finditer(text):
        host = m.group("host").lower()
        if not host.endswith(".icims.com"):
            out.append({"host": host, "name": host})
    return out


def _extract_rippling(text: str) -> list[dict]:
    return [{"slug": m.group("slug").lower(), "name": m.group("slug").lower()}
            for m in _RIPPLING_RE.finditer(text)]


def _validate_jibe(c: dict) -> bool:
    data = request_json("GET", f"https://{c['host']}/api/jobs", params={"limit": 1})
    return bool(data and data.get("totalCount"))


def _validate_rippling(c: dict) -> bool:
    data = request_json("GET", f"https://ats.rippling.com/api/v2/board/{c['slug']}/jobs")
    return bool(data and data.get("totalItems"))


def _validate_oracle(c: dict) -> bool:
    url = (f"https://{c['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
           f"?onlyData=true&finder=findReqs;siteNumber={c['site']},limit=1")
    data = request_json("GET", url)
    items = (data or {}).get("items") or [{}]
    return bool(items[0].get("TotalJobsCount"))


def _validate_icims(c: dict) -> bool:
    # No JSON API: a portal is valid if its listing page renders job links.
    try:
        resp = requests.get(f"https://{c['host']}/jobs/search",
                            params={"ss": 1, "in_iframe": 1},
                            headers=HEADERS, timeout=25)
    except requests.RequestException:
        return False
    time.sleep(1)       # iCIMS challenges bursts from one IP
    return resp.status_code == 200 and "iCIMS_Anchor" in resp.text


def _validate_workday(c: dict) -> bool:
    url = (f"https://{c['tenant']}.{c['wd']}.myworkdayjobs.com"
           f"/wday/cxs/{c['tenant']}/{c['site']}/jobs")
    data = request_json("POST", url, payload={
        "appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
    return bool(data and data.get("jobPostings"))


def _validate_greenhouse(c: dict) -> bool:
    url = f"https://boards-api.greenhouse.io/v1/boards/{c['token']}/jobs"
    data = request_json("GET", url)
    return bool(data and data.get("jobs"))


def _validate_lever(c: dict) -> bool:
    url = f"https://api.lever.co/v0/postings/{c['slug']}?mode=json"
    data = request_json("GET", url)
    return isinstance(data, list) and len(data) > 0


def _validate_ashby(c: dict) -> bool:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}"
    data = request_json("GET", url)
    return bool(data and data.get("jobs"))


def _validate_smartrecruiters(c: dict) -> bool:
    url = f"https://api.smartrecruiters.com/v1/companies/{c['company']}/postings"
    data = request_json("GET", url, params={"limit": 1})
    return bool(data and data.get("totalFound"))


@dataclass(frozen=True)
class ATSSpec:
    name: str                             # id used in --ats and reporting
    config_file: str                      # yaml file under config/
    extract: Callable[[str], list[dict]]  # text -> candidate dicts
    validate: Callable[[dict], bool]      # one API call: does this board exist?
    key: Callable[[dict], object]         # dedupe / merge identity
    cc_patterns: tuple[str, ...]          # Common Crawl URL-index queries

    @property
    def config_path(self) -> str:
        return os.path.join(CONFIG_DIR, self.config_file)


ATS_SPECS: list[ATSSpec] = [
    ATSSpec("workday", "companies.yaml", _extract_workday, _validate_workday,
            lambda c: (c["tenant"], c["wd"], c["site"]),
            ("*.myworkdayjobs.com/*",)),
    ATSSpec("greenhouse", "greenhouse.yaml", _extract_greenhouse, _validate_greenhouse,
            lambda c: c["token"],
            ("boards.greenhouse.io/*", "job-boards.greenhouse.io/*")),
    ATSSpec("lever", "lever.yaml", _extract_lever, _validate_lever,
            lambda c: c["slug"],
            ("jobs.lever.co/*",)),
    ATSSpec("ashby", "ashby.yaml", _extract_ashby, _validate_ashby,
            lambda c: c["slug"].lower(),
            ("jobs.ashbyhq.com/*",)),
    ATSSpec("smartrecruiters", "smartrecruiters.yaml", _extract_smartrecruiters,
            _validate_smartrecruiters,
            lambda c: c["company"].lower(),
            ("jobs.smartrecruiters.com/*", "careers.smartrecruiters.com/*")),
    ATSSpec("oracle", "oracle.yaml", _extract_oracle, _validate_oracle,
            lambda c: (c["host"], c["site"]),
            ("*.oraclecloud.com/hcmUI/CandidateExperience/*",)),
    ATSSpec("jibe", "jibe.yaml", _extract_jibe, _validate_jibe,
            lambda c: c["host"],
            ()),
    ATSSpec("rippling", "rippling.yaml", _extract_rippling, _validate_rippling,
            lambda c: c["slug"],
            ("ats.rippling.com/*",)),
    ATSSpec("icims", "icims.yaml", _extract_icims, _validate_icims,
            lambda c: c["host"],
            ("*.icims.com/jobs/*",)),
]


def extract_all(text: str, specs: list[ATSSpec]) -> dict[str, list[dict]]:
    return {spec.name: spec.extract(text) for spec in specs}


def dedupe_candidates(spec: ATSSpec, cands: list[dict]) -> list[dict]:
    seen, out = set(), []
    for c in cands:
        k = spec.key(c)
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def load_existing(spec: ATSSpec) -> tuple[list[dict], set]:
    if not os.path.exists(spec.config_path):
        return [], set()
    data = yaml.safe_load(open(spec.config_path, encoding="utf-8")) or {}
    companies = data.get("companies", []) or []
    return companies, {spec.key(c) for c in companies}


def save_config(spec: ATSSpec, companies: list[dict]) -> None:
    with open(spec.config_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump({"companies": companies}, fh, sort_keys=False,
                       allow_unicode=True)
