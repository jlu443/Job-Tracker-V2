"""Post newly-discovered jobs to a Discord channel via webhook.

Only `first_seen == this run` jobs are passed in, so each job is announced once.
Set DISCORD_WEBHOOK_URL to enable; if unset, this is a no-op (prints instead).
"""

from __future__ import annotations

import os
import re
import time

import requests

from . import dates, geo

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


def _line(job: dict) -> str:
    title = job["title"] if len(job["title"]) <= 90 else job["title"][:87] + "…"
    bits = [f"{'⭐ ' if is_hot(job) else ''}**{job['company'] or '—'}** · "
            f"[{title}]({job.get('direct_url') or job['apply_url']})"]
    if job.get("location"):
        bits.append(job["location"][:40])
    age = dates.age_days(job.get("posted_on") or "")
    if age is not None:
        bits.append("today" if age <= 0 else f"{age}d ago")
    if job["role_type"] == "new_grad":
        bits.append("new grad")
    if job.get("citizenship") == "required":
        bits.append("🇺🇸 US citizens only")
    elif job.get("sponsorship") == "no":
        bits.append("❌ no visa")
    elif job.get("sponsorship") == "yes":
        bits.append("✅ visa")
    if job.get("clearance") == "yes":
        bits.append("🔒 clearance")
    if job.get("pay"):
        bits.append(f"💵 {job['pay']}")
    if job.get("applicants"):
        bits.append(job["applicants"])
    if job.get("repost"):
        bits.append(_FLAG.get(job["repost"], "♻️ repost"))
    return " · ".join(bits)


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
    print(text)
    webhook = os.environ.get("DISCORD_ALERT_WEBHOOK_URL") or os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        return
    try:
        requests.post(webhook, json={"content": text[:2000]}, timeout=30).raise_for_status()
    except requests.RequestException as exc:
        print(f"  ! Discord alert failed: {exc}")


def post_new_jobs(jobs_to_post: list[dict]) -> None:
    if not jobs_to_post:
        print("No new intern/new_grad US jobs to announce.")
        return
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        print(f"DISCORD_WEBHOOK_URL not set — would announce {len(jobs_to_post)} jobs:")
        for j in jobs_to_post:
            print(f"  [{j['role_type']}] {j['company']}: {j['title']}")
        return
    batches = _batches(_embeds(jobs_to_post))
    failed = 0
    for batch in batches:
        try:
            _send(webhook, {"embeds": batch})
        except requests.RequestException as exc:
            failed += 1
            print(f"  ! Discord post failed: {exc}")
        time.sleep(0.5)
    print(f"Announced {len(jobs_to_post)} new jobs to Discord in {len(batches)} messages"
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


def post_daily_summary(jobs_last_24h: list[dict], sheet_url: str = "") -> None:
    """One line a day: what came in, by category, and where to see it all."""
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    counts = {}
    for j in jobs_last_24h:
        counts[j.get("category")] = counts.get(j.get("category"), 0) + 1
    parts = [f"{n} {_CATEGORY_HEADINGS[c][0].split(' ', 1)[1].lower()}"
             for c, n in sorted(counts.items(), key=lambda kv: -kv[1])
             if c in _CATEGORY_HEADINGS]
    hot = sum(is_hot(j) for j in jobs_last_24h)
    text = (f"📬 **Last 24 hours: {len(jobs_last_24h)} new intern/new-grad jobs**"
            + (f" ({', '.join(parts)})" if parts else "")
            + (f" · ⭐ {hot} fresh with few applicants" if hot else "")
            + (f"\nFull list with status tracking: {sheet_url}" if sheet_url else ""))
    print(text)
    if webhook:
        try:
            _send(webhook, {"content": text[:2000]})
        except requests.RequestException as exc:
            print(f"  ! Discord summary failed: {exc}")
