"""Job-function regression set: tests/golden_categories.tsv is a hand-labeled
random sample of announced titles. What matters for the feed is announce or
not (tech vs 'other'), so that's held to a floor; a few known misses remain."""
from pathlib import Path

import pytest

from src import classify

GOLDEN = Path(__file__).with_name("golden_categories.tsv")


def _cases():
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            label, title = line.split("\t", 1)
            yield label, title


def test_golden_file_is_well_formed():
    cases = list(_cases())
    assert len(cases) >= 200
    assert {label for label, _ in cases} <= set(classify.CATEGORIES)
    titles = [t for _, t in cases]
    assert len(titles) == len(set(titles)), "duplicate title"


def test_announce_precision_and_recall():
    tp = fp = fn = 0
    wrong = []
    for want, title in _cases():
        got = classify.categorize(title)
        if got != "other" and want != "other":
            tp += 1
        elif got != "other":
            fp += 1
            wrong.append(f"announced, isn't tech: {title!r} ({got})")
        elif want != "other":
            fn += 1
            wrong.append(f"not announced, is {want}: {title!r}")
    # 2026-10-11: precision 0.98 (was 0.69), recall 0.99.
    assert tp / (tp + fp) >= 0.97 and tp / (tp + fn) >= 0.98, "\n".join(wrong)


# Past mistakes outside the random sample (2026-10-11 end-to-end run).


@pytest.mark.parametrize("title,category", [
    ("Ultrasound Technologist - PRN", "other"),
    ("MRI Technologist - 10 hours", "other"),
    ("Research Technician - Antibiotic Discovery Center", "other"),
    ("Postdoctoral Researcher (Geotechnical Engineering)", "other"),
    ("Research Scientist New Grad - Trust and Safety", "data_ml"),
    ("People Research and Analytics Co-op", "data_ml"),
    ("Medical Device Software Engineer", "software"),
    # 2026-10-16 sample of announcements
    ("Automotive Mobile Glass Technician Apprentice", "other"),
    ("Summer 2027 Power Engineering Intern - Fort Worth, TX", "other"),
    ("Metrology Engineering Intern (Summer 2027)", "other"),
    ("Postdoctoral Associate - Cardioimmunology Research", "other"),
    ("Mobile Software Engineer Intern", "software"),
    ("Genomic Data Scientist", "data_ml"),
    ("MBA Strategy & Operations Intern - Rolling Application", "other"),
    ("Application Developer Intern", "software"),
])
def test_past_mistakes(title, category):
    assert classify.categorize(title) == category
