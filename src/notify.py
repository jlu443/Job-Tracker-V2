"""Post newly-discovered jobs to a Discord channel via webhook.

Only `first_seen == this run` jobs are passed in, so each job is announced once.
Set DISCORD_WEBHOOK_URL to enable; if unset, this is a no-op (prints instead).
"""

from __future__ import annotations

import logging
import os
import re
import time

import requests

from . import dates, geo

log = logging.getLogger(__name__)

# Discord limits: 10 embeds per message, 4096 characters per embed description.
_MAX_EMBEDS = 10
_MAX_DESC = 4000

_CATEGORY_HEADINGS = {
    "software": ("💻 Software", 0x3498DB),
    "data_ml": ("📊 Data / ML", 0x9B59B6),
    "hardware": ("🔧 Hardware", 0xE67E22),
    "quant": ("📈 Quant", 0x2ECC71),
    "product": ("🧭 Product", 0x1ABC9C),
}
_FLAG = {"relisted": "♻️ re-listed", "linkedin": "♻️ LinkedIn repost",
         "stale": "🕰️ old post", "bumped": "♻️ re-dated"}


def is_hot(job: dict) -> bool:
    """Worth applying to first: posted in the last 2 days, not a repost, and
    not already swamped (where the source reports applicants)."""
    age = dates.age_days(job.get("posted_on") or "")
    if age is None or age > 2 or job.get("repost"):
        return False
    applicants = job.get("applicants") or ""
    count = re.search(r"\d+", applicants.replace(",", ""))
    return not count or "first" in applicants.lower() or int(count.group()) < 50


_WD_SITE = re.compile(r"^[A-Z]{2}-[A-Z]{2}-([A-Za-z .]+?)(?:-\d+)?$")
_N_LOCATIONS = re.compile(r"^(\d+) Locations?$", re.I)


def _city(raw: str) -> str:
    """'Austin, TX' -> Austin; Workday site codes 'US-AZ-TUCSON-801 ~ 1151 E
    Hermans Rd' -> Tucson; 'Salem-Virginia-United States of America' -> Salem."""
    text = re.sub(r"\(.*?\)", "", raw.split("~")[0]).strip()
    m = _WD_SITE.match(text)
    if m:
        return m.group(1).strip().title()
    city = text.split(",")[0].strip()
    if "," not in text and "-" in city and not city.lower().startswith("remote"):
        city = city.split("-")[0].strip()
    if city.lower().startswith("remote"):
        return "Remote"
    return city


def _place(location: str) -> str:
    """First city only, with a count of the rest: "Austin +3"."""
    parts = [p for p in (location or "").replace(" +", "; +").split("; ") if p]
    if not parts:
        return ""
    m = _N_LOCATIONS.match(parts[0].strip())
    if m:
        return f"{m.group(1)} locations"
    more = sum(int(p[1:].split()[0]) if p.startswith("+") else 1 for p in parts[1:])
    return _city(parts[0]) + (f" +{more}" if more else "")


def _line(job: dict) -> str:
    """One short line: ⭐ Company · Title · City · 2d, plus flags only when
    they matter (visa, clearance, pay, repost, new grad)."""
    title = job["title"] if len(job["title"]) <= 70 else job["title"][:67] + "…"
    url = job.get("direct_url") or job["apply_url"]
    bits = [f"{'⭐ ' if is_hot(job) else ''}**{job['company'] or 'Company not listed'}** · [{title}]({url})"]
    place = _place(job.get("location") or "")
    if place:
        bits.append(place)
    age = dates.age_days(job.get("posted_on") or "")
    if age is not None:
        bits.append("today" if age <= 0 else f"{age}d")
    flags = []
    if job["role_type"] == "new_grad":
        flags.append("🎓")
    if job.get("citizenship") == "required":
        flags.append("🇺🇸 only")
    elif job.get("sponsorship") == "no":
        flags.append("❌ visa")
    elif job.get("sponsorship") == "yes":
        flags.append("✅ visa")
    if job.get("clearance") == "yes":
        flags.append("🔒")
    if job.get("pay"):
        flags.append(f"💵 {job['pay']}")
    if job.get("repost"):
        flags.append("🕰️" if job["repost"] == "stale" else "♻️")
    return " · ".join(bits) + ("  " + " ".join(flags) if flags else "")


def _embeds(jobs: list[dict]) -> list[dict]:
    """One embed per category (split when long); hot jobs listed first."""
    out = []
    for category, (heading, color) in _CATEGORY_HEADINGS.items():
        group = sorted((j for j in jobs if j.get("category") == category),
                       key=lambda j: j.get("posted_on") or "", reverse=True)
        group.sort(key=lambda j: not is_hot(j))     # stable: hot first, newest within
        lines, part = [], 1
        for line in [_line(j) for j in group] + [None]:
            if line is None or sum(len(x) + 1 for x in lines) + len(line) > _MAX_DESC:
                if lines:
                    title = f"{heading} ({len(group)})" + (f" · {part}" if part > 1 else "")
                    out.append({"title": title, "description": "\n".join(lines),
                                "color": color})
                    part += 1
                lines = []
            if line is not None:
                lines.append(line)
    return out


def _send(webhook: str, payload: dict) -> None:
    resp = requests.post(webhook, json=payload, timeout=30)
    if resp.status_code == 429:       # rate limited: Discord says how long to wait
        time.sleep(float(resp.json().get("retry_after", 1)) + 0.5)
        resp = requests.post(webhook, json=payload, timeout=30)
    resp.raise_for_status()


_ANNOUNCE_ROLES = {"intern", "new_grad"}
_REPOST_LABEL = {"relisted": "♻️ Re-listed", "linkedin": "♻️ LinkedIn repost",
                 "stale": "🕰️ Old posting"}


def announceable(jobs: list[dict], settings: dict) -> list[dict]:
    """Jobs worth a notification: entry-level, US, in a wanted job function."""
    categories = set(settings.get("announce_categories") or ())
    return [j for j in jobs
            if j.get("role_type") in _ANNOUNCE_ROLES
            and geo.is_us(j.get("location", ""))
            and not geo.title_names_foreign_place(j.get("title", ""))
            and (not categories or j.get("category") in categories)]


def post_alert(problems: list[str]) -> None:
    """Pipeline-health alert. Goes to DISCORD_ALERT_WEBHOOK_URL when set (a
    separate channel), else the jobs channel."""
    if not problems:
        return
    text = "⚠️ **Job tracker health check**\n" + "\n".join(f"• {p}" for p in problems)
    log.info(text)
    webhook = os.environ.get("DISCORD_ALERT_WEBHOOK_URL") or os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        return
    try:
        requests.post(webhook, json={"content": text[:2000]}, timeout=30).raise_for_status()
    except requests.RequestException as exc:
        log.warning(f"  ! Discord alert failed: {exc}")


def post_new_jobs(jobs_to_post: list[dict]) -> None:
    if not jobs_to_post:
        log.info("No new intern/new_grad US jobs to announce.")
        return
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        log.info(f"DISCORD_WEBHOOK_URL not set — would announce {len(jobs_to_post)} jobs:")
        for j in jobs_to_post:
            log.info(f"  [{j['role_type']}] {j['company']}: {j['title']}")
        return
    batches = _batches(_embeds(jobs_to_post))
    failed = 0
    for batch in batches:
        try:
            _send(webhook, {"embeds": batch})
        except requests.RequestException as exc:
            failed += 1
            log.warning(f"  ! Discord post failed: {exc}")
        time.sleep(0.5)
    log.info(f"Announced {len(jobs_to_post)} new jobs to Discord in {len(batches)} messages"
          + (f" ({failed} failed)" if failed else "") + ".")


# Discord rejects a message whose embeds total more than 6,000 characters
# (titles + descriptions), whatever the per-embed limits.
_MAX_MESSAGE_CHARS = 5800


def _batches(embeds: list[dict]) -> list[list[dict]]:
    out: list[list[dict]] = []
    size = 0
    for e in embeds:
        n = len(e.get("title", "")) + len(e.get("description", ""))
        if not out or len(out[-1]) >= _MAX_EMBEDS or size + n > _MAX_MESSAGE_CHARS:
            out.append([])
            size = 0
        out[-1].append(e)
        size += n
    return out


def post_digest(jobs: list[dict], since_label: str, sheet_url: str = "") -> None:
    """Every few hours: one header line, then the jobs announced since the
    last digest as compact per-category lists. Replaces a post per run."""
    if not jobs:
        log.info("Discord digest: nothing new since the last one.")
        return
    counts: dict = {}
    for j in jobs:
        counts[j.get("category")] = counts.get(j.get("category"), 0) + 1
    parts = [f"{n} {_CATEGORY_HEADINGS[c][0].split(' ', 1)[1].lower()}"
             for c, n in sorted(counts.items(), key=lambda kv: -kv[1])
             if c in _CATEGORY_HEADINGS]
    hot = sum(is_hot(j) for j in jobs)
    header = (f"📬 **{len(jobs)} new intern/new-grad jobs since {since_label}**"
              + (f" ({', '.join(parts)})" if parts else "")
              + (f" · ⭐ {hot} fresh, few applicants" if hot else "")
              + "\n-# ⭐ apply first · 🎓 new grad · ❌/✅ visa · 🇺🇸 citizens only · 🔒 clearance"
              + " · ♻️ repost · 🕰️ old post"
              + (f"\n-# Full list with status tracking: <{sheet_url}>" if sheet_url else ""))
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        log.info(header)
        for j in jobs:
            log.info("  " + _line(j))
        return
    try:
        _send(webhook, {"content": header[:2000]})
    except requests.RequestException as exc:
        log.warning(f"  ! Discord digest header failed: {exc}")
    post_new_jobs(jobs)

