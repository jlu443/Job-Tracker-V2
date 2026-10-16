"""Fetch postings from JazzHR job boards ({slug}.applytojob.com/apply).

JazzHR has no public JSON API; each board's server-rendered page lists every
open job:

    GET https://{slug}.applytojob.com/apply

    <h1 class="brand-text"> Company name </h1>
    <li class="list-group-item">
      <h3 class='list-group-item-heading'>
        <a href="https://{slug}.applytojob.com/apply/{id}/{title-slug}"> Title </a>
      </h3>
      <ul class='list-inline list-group-item-text'>
        <li><i class='fa fa-map-marker'></i>Cinnaminson, NJ</li>
        <li><i class='fa fa-sitemap'></i>Department</li>

The job page (/apply/{id}) carries the description, which enrichment reads.
Mostly small companies: 90 curated intern/new-grad listings on 61 boards
were out of reach before this scraper (2026-10-16).
"""

from __future__ import annotations

import html
import logging
import re

import requests

from . import http_pool
from .posting import JobPosting

log = logging.getLogger(__name__)

_SESSION = http_pool.make_session({"User-Agent": "Mozilla/5.0 (job-tracker)"})

_URL = re.compile(r"https?://([a-z0-9-]+)\.applytojob\.com/apply/(?:jobs/details/)?([A-Za-z0-9]{6,})",
                  re.I)
_ITEM = re.compile(r'<li class="list-group-item">(.*?)</li>\s*(?=<li class="list-group-item">|</ul>)',
                   re.S)
_LINK = re.compile(r'<a href="(https?://[^"]+/apply/([A-Za-z0-9]+)/[^"]*)"[^>]*>\s*(.*?)\s*</a>', re.S)
_PLACE = re.compile(r"fa-map-marker'?\"?></i>\s*([^<]*)<", re.S)
_BRAND = re.compile(r'class="brand-text"[^>]*>\s*(.*?)\s*<', re.S)
_DESCRIPTION = re.compile(r'id="job-description"[^>]*>(.*?)</div>\s*</div>', re.S)


def job_id_for(slug: str, job_id: str) -> str:
    return f"jazz_{slug.lower()}_{job_id}"


def canonical_id(url: str) -> str | None:
    m = _URL.search(url or "")
    return job_id_for(m.group(1), m.group(2)) if m else None


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).split())


def parse_board(page: str, slug: str, name: str) -> list[JobPosting]:
    out = []
    for item in _ITEM.findall(page):
        link = _LINK.search(item)
        if not link:
            continue
        place = _PLACE.search(item)
        out.append(JobPosting(
            job_id=job_id_for(slug, link.group(2)), company=name,
            title=_text(link.group(3)), apply_url=link.group(1).replace("http://", "https://", 1),
            location=_text(place.group(1)) if place else "", posted_on="", source="jazzhr"))
    return out


def fetch_company_jobs(company: dict, settings: dict) -> tuple[list[JobPosting], bool]:
    slug = company["slug"]
    try:
        resp = _SESSION.get(f"https://{slug}.applytojob.com/apply",
                            timeout=settings.get("request_timeout", 30))
        if resp.status_code == 404:
            log.warning(f"  ! {company.get('name', slug)}: no JazzHR board '{slug}' (404)")
            return [], True
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning(f"  ! {company.get('name', slug)}: {exc}")
        return [], False
    brand = _BRAND.search(resp.text)
    name = company.get("name") or (_text(brand.group(1)) if brand else slug)
    return parse_board(resp.text, slug, name), True


def fetch_description(url: str, timeout: int) -> str:
    resp = _SESSION.get(url, timeout=timeout)
    resp.raise_for_status()
    m = _DESCRIPTION.search(resp.text)
    return _text(m.group(1)) if m else ""
