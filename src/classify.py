"""Classify a job title into one of: intern | new_grad | mid | senior.

Two layers:
  1. Keyword/regex pass — deterministic, free, handles the clear majority.
  2. Zero-shot NLI fallback — facebook/bart-large-mnli, runs locally, no API key.
     Model is downloaded once (~1.6 GB) and cached by HuggingFace.
"""

from __future__ import annotations

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

# Descriptive labels for zero-shot NLI — more context beats bare words.
_ZS_LABELS = {
    "intern": "internship, co-op, or summer position for students",
    "new_grad": "entry-level position for new or recent graduates",
    "mid": "mid-level position requiring a few years of experience",
    "senior": "senior, staff, lead, principal, or management role",
}

_pipeline = None  # lazily loaded


def _get_pipeline():
    global _pipeline
    if _pipeline is None:
        from transformers import pipeline
        _pipeline = pipeline(
            "zero-shot-classification",
            model="facebook/bart-large-mnli",
            device=-1,  # CPU; set to 0 for GPU
        )
    return _pipeline


def classify_by_keyword(title: str) -> str | None:
    """Return a role type, or None if no rule matches (inconclusive)."""
    for pattern, label in _COMPILED:
        if pattern.search(title):
            return label
    return None


def classify_by_zeroshot(title: str) -> str | None:
    """Zero-shot NLI classification. Returns a valid role type, or None on error."""
    try:
        clf = _get_pipeline()
        result = clf(title, list(_ZS_LABELS.values()), multi_label=False)
        best_desc = result["labels"][0]
        # Map description back to role type key
        desc_to_key = {v: k for k, v in _ZS_LABELS.items()}
        return desc_to_key.get(best_desc)
    except Exception as exc:
        print(f"  ! zero-shot classify failed for {title!r}: {exc}")
        return None


def classify(title: str, settings: dict) -> str:
    """Keyword pass first; zero-shot NLI only on inconclusive titles. Defaults to 'mid'."""
    label = classify_by_keyword(title)
    if label:
        return label

    if settings.get("use_llm_fallback"):
        label = classify_by_zeroshot(title)
        if label:
            return label

    return "mid"  # safe default when nothing else decides


def _zeroshot_many(titles: list[str]) -> dict[str, str]:
    """One batched zero-shot call for many titles. Returns title -> role type."""
    try:
        clf = _get_pipeline()
        results = clf(titles, list(_ZS_LABELS.values()), multi_label=False,
                      batch_size=8)
        if isinstance(results, dict):    # pipeline unwraps single-item lists
            results = [results]
        desc_to_key = {v: k for k, v in _ZS_LABELS.items()}
        out = {}
        for title, res in zip(titles, results):
            key = desc_to_key.get(res["labels"][0])
            if key:
                out[title] = key
        return out
    except Exception as exc:
        print(f"  ! zero-shot batch classify failed: {exc}")
        return {}


def classify_batch(titles: list[str], settings: dict) -> list[str]:
    """Classify many titles at once.

    Keyword pass first; the inconclusive remainder is deduped and sent through
    the zero-shot model in one batched call — orders of magnitude faster than
    one pipeline invocation per title on CPU.
    """
    labels = [classify_by_keyword(t) for t in titles]
    if settings.get("use_llm_fallback"):
        pending = sorted({t for t, l in zip(titles, labels) if l is None})
        if pending:
            print(f"  zero-shot classifying {len(pending)} unique ambiguous titles ...")
            resolved = _zeroshot_many(pending)
            labels = [l or resolved.get(t) for t, l in zip(titles, labels)]
    return [l or "mid" for l in labels]
