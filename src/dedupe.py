"""Cross-source deduplication.

The same role often appears both on a company's own ATS board and on an
aggregator (Indeed etc.) with unrelated job ids, so it would be stored and
announced twice. Two layers deal with that:

  * dedupe_postings() collapses cross-source duplicates within one run.
    main.py scrapes first-party ATS boards before the aggregators, so the
    first-party copy wins. Same-source twins are kept — two identical titles
    on one company board are usually genuinely separate requisitions.
  * fuzzy_key() is also matched against rows already active in the DB to
    suppress announcing a posting that is a re-listing of a known job under
    a new id/source (e.g. first seen on Indeed, later on the ATS board).
"""

from __future__ import annotations

import re

# Trailing legal suffixes stripped from company names before comparison.
_SUFFIXES = {"inc", "llc", "ltd", "corp", "co", "corporation", "incorporated",
             "company", "plc", "gmbh", "limited"}

_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")


def _tokens(s: str) -> list[str]:
    return _NON_ALNUM.sub(" ", s.lower()).split()


def _norm_company(s: str) -> str:
    tokens = _tokens(s)
    while tokens and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _norm_title(s: str) -> str:
    # Token-sorted so "Intern, Software (Summer 2026)" matches
    # "Software Intern - Summer 2026" across sources.
    return " ".join(sorted(_tokens(s)))


def _norm_location(s: str) -> str:
    # City only: "San Francisco, CA" and "San Francisco, California, US" match.
    return " ".join(_tokens((s or "").split(",")[0]))


def fuzzy_key(company: str, title: str, location: str) -> str | None:
    """Stable identity for 'same role, different listing'. None if unkeyable."""
    c, t = _norm_company(company or ""), _norm_title(title or "")
    if not c or not t:
        return None
    return f"{c}|{t}|{_norm_location(location or '')}"


def dedupe_postings(postings: list) -> tuple[list, int]:
    """Drop postings whose fuzzy key was already seen under a different source.

    Returns (kept postings in original order, number dropped).
    """
    kept: list = []
    first_source: dict[str, str] = {}
    dropped = 0
    for p in postings:
        key = fuzzy_key(p.company, p.title, p.location)
        if key is not None:
            source = getattr(p, "source", "workday")
            prior = first_source.get(key)
            if prior is not None and prior != source:
                dropped += 1
                continue
            first_source.setdefault(key, source)
        kept.append(p)
    return kept, dropped
