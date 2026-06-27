"""Classify a job title into one of: intern | new_grad | mid | senior.

Two layers:
  1. Keyword/regex pass — deterministic, free, handles the clear majority.
  2. Claude Haiku fallback — only for titles the keyword pass can't decide,
     and only when use_llm_fallback is on and ANTHROPIC_API_KEY is set.
"""

from __future__ import annotations

import os
import re

ROLE_TYPES = ("intern", "new_grad", "mid", "senior")

# Ordered most-specific first. First matching pattern wins.
_RULES: list[tuple[str, str]] = [
    (r"\b(intern|internship|co-?op|summer\s+(?:analyst|associate))\b", "intern"),
    (r"\b(new\s*grad|new\s*graduate|university\s*grad|campus|early\s*career|"
     r"entry[\s-]*level|graduate\s+(?:program|engineer|analyst)|associate\s+"
     r"(?:engineer|developer))\b", "new_grad"),
    (r"\b(senior|sr\.?|staff|principal|lead|architect|distinguished|"
     r"director|head\s+of|vp|manager|mgr|fellow)\b", "senior"),
    (r"\b(iii|iv|v)\b", "senior"),
    (r"\b(ii)\b", "mid"),
    (r"\b\d{2,}\+?\s*years?\b", "senior"),  # "5+ years", "10 years"
]

_COMPILED = [(re.compile(p, re.IGNORECASE), label) for p, label in _RULES]


def classify_by_keyword(title: str) -> str | None:
    """Return a role type, or None if no rule matches (inconclusive)."""
    for pattern, label in _COMPILED:
        if pattern.search(title):
            return label
    return None


# --- Claude Haiku fallback ---------------------------------------------------

_SYSTEM = (
    "You classify software/tech job titles by seniority. "
    "Reply with exactly one of these words and nothing else: "
    "intern, new_grad, mid, senior. "
    "Use 'new_grad' for entry-level/university/early-career roles, "
    "'mid' for roles needing a few years of experience, "
    "'senior' for senior/staff/principal/lead/management roles."
)

_client = None  # lazily constructed


def _get_client():
    global _client
    if _client is None:
        import anthropic  # imported lazily so the scraper runs without the dep
        _client = anthropic.Anthropic()
    return _client


def classify_by_llm(title: str) -> str | None:
    """Single Haiku call. Returns a valid role type, or None on any problem."""
    try:
        resp = _get_client().messages.create(
            model="claude-haiku-4-5",
            max_tokens=8,
            system=_SYSTEM,
            messages=[{"role": "user", "content": title}],
        )
    except Exception as exc:  # network, auth, rate limit — never fatal
        print(f"  ! LLM classify failed for {title!r}: {exc}")
        return None

    text = next((b.text for b in resp.content if b.type == "text"), "").strip().lower()
    return text if text in ROLE_TYPES else None


def classify(title: str, settings: dict) -> str:
    """Keyword pass first; Haiku only on inconclusive titles. Defaults to 'mid'."""
    label = classify_by_keyword(title)
    if label:
        return label

    if settings.get("use_llm_fallback") and os.environ.get("ANTHROPIC_API_KEY"):
        label = classify_by_llm(title)
        if label:
            return label

    return "mid"  # safe default when nothing else decides
