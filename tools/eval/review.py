"""Human review of an evaluation run (docs/eval.md).

`export` writes, for each case of the last run, the needs and every handoff of the case under a neutral
letter, in a random order, and one sheet to fill in. The key that maps the letters back is written
beside the folder, not into it, so the people reviewing don't see it. Blinding is partial: plain notes
look different from ContextRail's sections, so it hides which cap or version a document is, not
always whether it is a baseline.

`agreement` reads filled-in sheets and reports, per handoff, the coverage each reviewer found, and how
often the reviewers and the model judge gave the same verdict, as a share and as Cohen's kappa."""

from __future__ import annotations

import csv
import json
import random
import string
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import report, steps
from .steps import VERDICTS, EvalError, Harness, read_json

INSTRUCTIONS = """Review sheets for ContextRail's handoff evaluation

Each case-NN.md holds the needs of one session that started from a handoff (what it needed from the
session before), and two or more documents written at the same point of that earlier session. Each
document is everything the next session would get. They are in a random order under letters.

For every need of a case, fill in sheet.csv with one verdict per document letter:
  covered  - it gives the information, specific enough to act on
  partial  - it gives part of it, or only vaguely
  missing  - it doesn't give it
  wrong    - it says something that contradicts it
On the case's "best" row, put the letter of the document that would serve the next session best, or
"tie", in the first letter column. Judge the content, not the length or the format. Work alone: don't
compare notes with other reviewers until everyone has finished.
"""
# Verdicts by (case number, need id) and document letter.
Sheet = Dict[Tuple[str, str], Dict[str, str]]


def _run(h: Harness) -> Dict[str, object]:
    path = h.root / "run.json"
    if not path.exists():
        raise EvalError(f"no run yet ({path} is missing)")
    return read_json(path)


def export(h: Harness, out: Path, seed: Optional[int] = None) -> int:
    run = _run(h)
    if out.exists() and any(out.iterdir()):
        raise EvalError(f"{out} is not empty")
    results = [result for result in report.load(h, run) if result.needs]
    if not results:
        raise EvalError("no case of the last run has its needs listed yet")
    seed = random.SystemRandom().randrange(2**32) if seed is None else seed
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    (out / "README.txt").write_text(INSTRUCTIONS, encoding="utf-8")
    width = max(len(result.bodies) for result in results)
    letters = list(string.ascii_uppercase[:width])
    key: Dict[str, object] = {"run_started": run.get("started"), "seed": seed, "cases": {}}
    rows: List[List[str]] = []
    for number, result in enumerate(results, 1):
        case_id = f"{number:02d}"
        shown = sorted(result.bodies)
        rng.shuffle(shown)
        labels = dict(zip(letters, shown))
        key["cases"][case_id] = {"key": result.case.key, "labels": labels}  # type: ignore[index]
        parts = [f"# Case {case_id}", "", "## Needs", ""]
        parts += [f"{item['id']}. {item['need']}" for item in result.needs]
        for letter, variant in labels.items():
            parts += ["", f"## Document {letter}", "", steps.material(h, result.case, variant).strip()]
        (out / f"case-{case_id}.md").write_text("\n".join(parts) + "\n", encoding="utf-8")
        rows += [[case_id, str(item["id"]), item["need"]] + [""] * width for item in result.needs]
        rows.append([case_id, "best", "Letter of the best document, or tie, in column A"] + [""] * width)
    with open(out / "sheet.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["case", "need", "text", *letters])
        writer.writerows(rows)
    key_path = out.with_name(out.name + ".key.json")
    key_path.write_text(json.dumps(key, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(results)} case(s) to {out}: a README, one case-NN.md each and sheet.csv to fill in.")
    print(f"The key is {key_path}: keep it away from the reviewers.")
    return 0


def read_sheet(path: Path) -> Sheet:
    found: Sheet = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            marks = {
                letter: (row.get(letter) or "").strip().lower()
                for letter in row if len(letter) == 1 and letter.isupper()
            }
            found[(str(row.get("case") or "").strip(), str(row.get("need") or "").strip())] = marks
    return found


def kappa(pairs: Sequence[Tuple[str, str]]) -> Optional[float]:
    """Cohen's kappa of two raters' verdicts on the same items; None without items."""
    if not pairs:
        return None
    total = len(pairs)
    observed = sum(a == b for a, b in pairs) / total
    first, second = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(first[c] / total * second[c] / total for c in set(first) | set(second))
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)


def _verdicts(sheet: Sheet, labels: Dict[str, Dict[str, str]]) -> Dict[Tuple[str, str, str], str]:
    """(case, need, variant) -> verdict, for the verdicts a sheet gives, checked against VERDICTS."""
    found = {}
    for (case_id, need), marks in sheet.items():
        if need == "best" or case_id not in labels:
            continue
        for letter, mark in marks.items():
            if not mark or letter not in labels[case_id]:
                continue
            if mark not in VERDICTS:
                raise EvalError(f"case {case_id}, need {need}, document {letter}: {mark!r} is not one of {', '.join(VERDICTS)}")
            found[(case_id, need, labels[case_id][letter])] = mark
    return found


def _model(h: Harness, run: Dict[str, object], keys: Dict[str, str]) -> Dict[Tuple[str, str, str], str]:
    """The model judge's verdicts on the same items: per need and handoff, the one it gave most often."""
    by_key = {result.case.key: result for result in report.load(h, run)}
    found = {}
    for case_id, key in keys.items():
        result = by_key.get(key)
        if result is None:
            continue
        given: Dict[Tuple[str, str], List[str]] = {}
        for judgment in result.judgments:
            for row in judgment["verdicts"]:
                for variant in (judgment["first"], judgment["second"]):
                    given.setdefault((str(row["id"]), variant), []).append(row[variant])
        for (need, variant), verdicts in given.items():
            found[(case_id, need, variant)] = Counter(verdicts).most_common(1)[0][0]
    return found


def _compare(a: Dict[Tuple[str, str, str], str], b: Dict[Tuple[str, str, str], str]) -> str:
    shared = [(a[item], b[item]) for item in a if item in b]
    if not shared:
        return "no items in common"
    agree = sum(x == y for x, y in shared) / len(shared)
    value = kappa(shared)
    return f"{len(shared)} items, {agree * 100:.0f}% the same verdict, kappa {value:.2f}"


def agreement(h: Harness, sheets: List[Path], key_path: Path) -> int:
    key = read_json(key_path)
    labels = {case_id: entry["labels"] for case_id, entry in key["cases"].items()}
    keys = {case_id: entry["key"] for case_id, entry in key["cases"].items()}
    reviewers = {path.stem: _verdicts(read_sheet(path), labels) for path in sheets}
    model = _model(h, _run(h), keys)
    variants = sorted({variant for found in reviewers.values() for (_, _, variant) in found})
    print(f"{len(reviewers)} reviewer(s), {len(labels)} case(s). Coverage by handoff (a partly covered need counts half):")
    for name, found in reviewers.items():
        cells = []
        for variant in variants:
            marks = [mark for (_, _, v), mark in found.items() if v == variant]
            cells.append(f"{variant} {report.coverage(marks) * 100:.0f}%" if marks else f"{variant} –")
        print(f"  {name}: " + ", ".join(cells))
    print("Agreement:")
    names = list(reviewers)
    for name in names:
        print(f"  {name} and the model judge: {_compare(reviewers[name], model)}")
    for i, first in enumerate(names):
        for second in names[i + 1:]:
            print(f"  {first} and {second}: {_compare(reviewers[first], reviewers[second])}")
    return 0
