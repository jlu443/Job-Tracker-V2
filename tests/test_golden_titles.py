"""Classifier regression set: every title in golden_titles.tsv must keep its label."""
from pathlib import Path

from src import classify

GOLDEN = Path(__file__).with_name("golden_titles.tsv")


def _cases():
    for n, line in enumerate(GOLDEN.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip() and not line.startswith("#"):
            label, title = line.split("\t", 1)
            yield n, label, title


def test_golden_file_is_well_formed():
    cases = list(_cases())
    assert len(cases) >= 200
    assert {label for _, label, _ in cases} == {"intern", "new_grad", "no"}
    titles = [t for _, _, t in cases]
    assert len(titles) == len(set(titles)), "duplicate title"


def test_golden_titles():
    wrong = []
    for n, label, title in _cases():
        got = classify.classify_by_keyword(title)
        if (got if got in ("intern", "new_grad") else "no") != label:
            wrong.append(f"line {n}: {title!r} expected {label}, got {got}")
    assert not wrong, f"{len(wrong)} golden titles misclassified:\n" + "\n".join(wrong)
