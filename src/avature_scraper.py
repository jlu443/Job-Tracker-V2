"""Fetch postings from Avature career sites (Deloitte, EA, Two Sigma, Koch, ...).

Avature sites have no public JSON API, but every site serves a search page
whose results are <article> blocks linking to the job:

    GET {base}/SearchJobs?search={term}&jobOffset={n}&jobRecordsPerPage=50
        base = https://{host}[/en_US]/careers
    <article ...> <h3 ...><a href="{base}/JobDetail/{slug}/{id}"> Title </a></h3>
                  ... location, in one of several layouts ... </article>

The location sits in a "list-item-location" span (EA), a "Location:" field
(Koch, Two Sigma) or the subtitle's last item (Deloitte). A site without a
search page (Two Sigma) lists its jobs on {base} itself. 40 curated
intern/new-grad listings were on Avature sites, 2026-10-17.
"""

from __future__ import annotations

import html
import logging
import re
import time

import requests

from . import classify, http_pool
from .posting import JobPosting

log = logging.getLogger(__name__)

_SESSION = http_pool.make_session({"User-Agent": "Mozilla/5.0 (job-tracker)"})
_TERMS = ("intern", "co-op", "graduate", "entry level", "analyst")
_PAGE = 50

_URL = re.compile(r"https?://([\w.-]+)/(?:[a-z]{2}_[A-Z]{2}/)?careers/JobDetail/(?:[^/?#\s]+/)?(\d+)")
_ARTICLE = re.compile(r"<article\b(.*?)</article>", re.S)
_LINK = re.compile(r'<a\b[^>]*href="([^"]*/careers/JobDetail/[^"]*?)"[^>]*>(.*?)</a>', re.S)
_LOCATION_SPAN = re.compile(r'class="[^"]*location[^"]*"[^>]*>(.*?)</', re.S | re.I)
_LABELLED = re.compile(r">\s*(Location|Experience Level|Job Type|Employment Type|Position Type)\s*:?\s*<"
                       r"/(?:strong|div|span|label|dt)>\s*(?:<[^>]+>\s*)*([^<]+)<", re.S | re.I)
_SUBTITLE = re.compile(r'class="article__header__text__subtitle"[^>]*>(.*?)</div>', re.S)
_HEADER_TEXT = re.compile(r'class="article__header__content__text"[^>]*>\s*<span[^>]*>(.*?)</span>',
                          re.S)
_HEADER_SUB = re.compile(r'class="article__header__content__sub-text"[^>]*>(.*?)</div>', re.S)


# A site answers on its avature.net name and its own domain; ids use one key
# so curated links (twosigma.avature.net) match scraped ones
# (careers.twosigma.com).
_HOST_KEYS = {"careers.twosigma.com": "twosigma"}


def site_key(host: str) -> str:
    host = host.lower()
    if host.endswith(".avature.net"):
        return host.split(".")[0]
    return _HOST_KEYS.get(host, host)


def job_id_for(host: str, job_id: str) -> str:
    return f"av_{site_key(host)}_{job_id}"


def canonical_id(url: str) -> str | None:
    m = _URL.search(url or "")
    return job_id_for(m.group(1), m.group(2)) if m else None


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).split())


def parse_results(page: str, name: str) -> list[JobPosting]:
    out = []
    for block in _ARTICLE.findall(page):
        link = _LINK.search(block)
        if not link:
            continue
        url, title = html.unescape(link.group(1)), _text(link.group(2))
        m = _URL.search(url)
        if not m or not title:
            continue
        fields = {k.lower(): _text(v) for k, v in _LABELLED.findall(block)}
        location = fields.get("location", "")
        if not location:
            span = _LOCATION_SPAN.search(block)
            location = _text(span.group(1)) if span else ""
        header = _HEADER_TEXT.search(block)   # Two Sigma's list pages
        if not location and header:
            location = _text(header.group(1))
        if not location:
            sub = _SUBTITLE.search(block)
            parts = [_text(p) for p in re.findall(r"<span[^>]*>(.*?)</span>", sub.group(1), re.S)] \
                if sub else []
            location = parts[-1] if parts else ""
        level = next((fields[k] for k in ("experience level", "job type", "employment type",
                                          "position type") if k in fields), "")
        if not level and header:              # "Quantitative Research · Early Careers"
            sub = _HEADER_SUB.search(block)
            level = _text(sub.group(1)) if sub else ""
        out.append(JobPosting(
            job_id=job_id_for(m.group(1), m.group(2)), company=name, title=title,
            apply_url=url, location=location, posted_on="", source="avature",
            role_hint=classify.hint_from_level(level)))
    return out


def fetch_company_jobs(company: dict, settings: dict) -> tuple[list[JobPosting], bool]:
    base = company["base"].rstrip("/")
    name = company.get("name", base)
    timeout = settings.get("request_timeout", 30)
    delay = settings.get("delay_between_requests", 0.5)
    seen: dict[str, JobPosting] = {}
    complete = True
    # A site without a search page lists its jobs on named pages instead
    # (Two Sigma: InternshipsAndEarlyCareers, OpenRoles), paged the same way.
    queries = ([(f"{base}/{path}", {}) for path in company["list_paths"]]
               if company.get("list_paths") else
               [(f"{base}/SearchJobs", {"search": term})
                for term in company.get("search_terms", _TERMS)])
    for url, params in queries:
        offset = 0
        for _ in range(20):
            try:
                resp = _SESSION.get(url, timeout=timeout,
                                    params={**params, "jobOffset": offset,
                                            "jobRecordsPerPage": _PAGE})
                resp.raise_for_status()
            except requests.RequestException as exc:
                log.warning(f"  ! {name} {url} {params} offset={offset}: {exc}")
                complete = False
                break
            jobs = parse_results(resp.text, name)
            new = [j for j in jobs if j.job_id not in seen]
            seen.update((j.job_id, j) for j in new)
            if not new:
                break
            offset += len(jobs)      # sites cap the page size (Two Sigma: 10)
            time.sleep(delay)
    return list(seen.values()), complete


def fetch_description(url: str, timeout: int = 30) -> str:
    resp = _SESSION.get(url, timeout=timeout)
    resp.raise_for_status()
    page = resp.text
    main = re.search(r"<main\b.*?</main>", page, re.S)
    return _text((main.group(0) if main else page)[:200000])
