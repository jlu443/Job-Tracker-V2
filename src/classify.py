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
    (r"\b(intern|interns|internship|co-?op|summer\s+(?:analyst|associate)|"
     r"working\s+student|student|apprentice(?:ship)?|fellowship|externship)\b",
     "intern"),
    (r"\b(new\s*grad|new\s*graduate|new\s+college\s+grad(?:uate)?|ncg|"
     r"university\s*grad(?:uate)?|recent\s+grad(?:uate)?|campus|"
     r"early\s*(?:career|talent|in\s+career)|entry[\s-]*level|"
     r"graduate\s+(?:program|engineer|analyst|scientist|researcher|developer)|"
     r"class\s+of\s+20\d\d|rotation(?:al)?\s+(?:program|engineer)|"
     r"leadership\s+development\s+program|residency|junior|jr\.?|"
     r"associate\s+(?:software\s+)?(?:engineer|developer|product\s+manager|"
     r"data\s+scientist)|apm)\b", "new_grad"),
    # "Software Engineer I", "Analyst 1" — level one of a laddered title.
    (r"\b(?:engineer|developer|analyst|scientist|programmer|technician|"
     r"designer|specialist)\s*(?:i|1)\b", "new_grad"),
    # "Data Scientist - PhD (2026)": degree plus a graduation year.
    (r"\b(?:phd|ph\.d|ms|m\.s|bs|b\.s|masters?|bachelors?)\b.{0,20}\b20[2-3]\d\b",
     "new_grad"),
    (r"\b(senior|sr\.?|staff|principal|lead|architect|distinguished|"
     r"director|head\s+of|vp|manager|mgr|fellow)\b", "senior"),
    (r"\b(iii|iv|v)\b", "senior"),
    (r"\b(ii|2)\b", "mid"),
    (r"\b\d{2,}\+?\s*years?\b", "senior"),  # "5+ years", "10 years"
]

_COMPILED = [(re.compile(p, re.IGNORECASE), label) for p, label in _RULES]

# Job function. Ordered: first match wins, so specific families come before
# the generic "engineer" catch-all, and non-software engineering disciplines
# are claimed before it can mislabel them as software.
_CATEGORY_RULES: list[tuple[str, str]] = [
    (r"\b(quant|quantitative|trading|trader)\b", "quant"),
    (r"\b(asic|fpga|rtl|silicon|circuit|analog|mixed[\s-]signal|hardware|"
     r"physical\s+design|design\s+verification|dft|photonics|semiconductor|"
     r"pcb|signal\s+(?:and|&)\s+power\s+integrity|electrical\s+engineer|"
     r"cpu|gpu\s+architecture|soc|rf|transistor|avionics)\b", "hardware"),
    (r"\b(machine\s+learning|ml|ai|artificial\s+intelligence|deep\s+learning|"
     r"data\s+scien\w*|data\s+engineer\w*|data\s+analy\w*|analytics|"
     r"computer\s+vision|nlp|llm|research\s+scientist|applied\s+scientist|"
     r"business\s+intelligence)\b", "data_ml"),
    (r"\b(product\s+manag\w*|apm|technical\s+program\s+manag\w*|tpm)\b", "product"),
    (r"\b(security\s+(?:guard|officer))\b", "other"),
    (r"\b(software|swe|sde|developer|programmer|devops|sre|site\s+reliability|"
     r"back[\s-]?end|front[\s-]?end|full[\s-]?stack|web|mobile|ios|android|"
     r"cloud|platform|infrastructure|security|cyber\w*|computer\s+science|"
     r"embedded|firmware|qa|sdet|test\s+automation|forward\s+deployed|"
     r"it)\b", "software"),
    (r"\b(mechanical|civil|chemical|industrial|manufacturing|process|"
     r"structural|environmental|propulsion|thermal|materials|biomedical|"
     r"petroleum|mining|nuclear|construction|field\s+service|sales|"
     r"marketing|legal|finance|accounting|recruit\w*|hr|human\s+resources|"
     r"traffic|bridge|highway|roadway|transportation|geotechnical|water|"
     r"wastewater|hvac|production|operations\s+engineer\w*|power\s+delivery|"
     r"substation|utility|utilities|land\s+development|urban)\b",
     "other"),
    (r"\b(systems?\s+engineer\w*|solutions?\s+engineer|engineer\w*|"
     r"technolog\w*)\b", "software"),
]
_CATEGORY_COMPILED = [(re.compile(p, re.IGNORECASE), c) for p, c in _CATEGORY_RULES]

CATEGORIES = ("software", "data_ml", "hardware", "quant", "product", "other")


def categorize(title: str) -> str:
    for pattern, category in _CATEGORY_COMPILED:
        if pattern.search(title):
            return category
    return "other"


def is_entry_level(title: str) -> bool:
    return classify_by_keyword(title) in ("intern", "new_grad")

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
