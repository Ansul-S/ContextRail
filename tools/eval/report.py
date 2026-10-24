"""The numbers of an evaluation run, as Markdown for docs/eval.md. The report holds
numbers only, never what a session or a handoff says."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional, Tuple

from contextrail import handoff

from . import cases, steps
from .cases import Case
from .steps import BASELINES, FACTS, NOTES, SAVED, EvalError, Harness, read_json

START = "<!-- eval:results -->"
END = "<!-- /eval:results -->"
DOC = Path(__file__).resolve().parents[2] / "docs" / "eval.md"


def coverage(verdicts: List[str]) -> float:
    """The share of needs covered, a partly covered one counting half."""
    if not verdicts:
        return 0.0
    return (verdicts.count("covered") + 0.5 * verdicts.count("partial")) / len(verdicts)


@dataclass
class CaseResult:
    case: Case
    needs_file: Dict[str, Any]  # needs.json, or {} before it is written
    bodies: Dict[str, str]
    gens: Dict[str, Dict[str, Any]]
    judgments: List[Dict[str, Any]]

    @property
    def needs(self) -> List[Dict[str, Any]]:
        return list(self.needs_file.get("needs") or [])

    def verdicts(self, variant: str, gaps_only: bool = False) -> List[List[str]]:
        """Per judging of this variant, its verdict on each need. Gaps are needs the
        handoff B loaded did not meet: everything but what B used from it."""
        kinds = {item["id"]: item["kind"] for item in self.needs}
        return [
            [row[variant] for row in judgment["verdicts"] if not gaps_only or kinds.get(row["id"]) != "used"]
            for judgment in self.judgments if variant in (judgment["first"], judgment["second"])
        ]

    def cost(self) -> float:
        files = [self.needs_file, *self.gens.values(), *self.judgments]
        return sum(float(data.get("cost_usd") or 0) for data in files)


@dataclass
class Row:
    variant: str
    coverage: Optional[float]
    gaps: Optional[float]
    wrong: Optional[float]
    chars: float
    over: Optional[int]  # handoffs ContextRail would have sent back for a fix; None for a baseline
    count: int
    seconds: Optional[float]


def load(h: Harness, run: Dict[str, Any]) -> List[CaseResult]:
    baselines = run.get("baselines") or []
    results = []
    for key in run["cases"]:
        folder = h.root / "cases" / key
        if not (folder / "case.json").exists():
            raise EvalError(f"case {key} has no case.json; run it first")
        case = Case.from_json(read_json(folder / "case.json"))
        needs_file = steps.needs_path(h, case)
        gens = {str(cap): read_json(steps.gen_path(h, case, cap)) for cap in run["caps"] if steps.gen_path(h, case, cap).exists()}
        if NOTES in baselines and steps.notes_path(h, case).exists():
            gens[NOTES] = read_json(steps.notes_path(h, case))
        judgments = [
            read_json(steps.judge_path(h, case, first, second))
            for first, second in steps.pairs(run["caps"], baselines) if steps.judge_path(h, case, first, second).exists()
        ]
        bodies = {SAVED: case.saved_body, **{name: str(data["body"]) for name, data in gens.items()}}
        if FACTS in baselines:
            bodies[FACTS] = steps.body(h, case, FACTS)
        results.append(CaseResult(case, read_json(needs_file) if needs_file.exists() else {}, bodies, gens, judgments))
    return results


def _mean(values: List[Optional[float]]) -> Optional[float]:
    found = [value for value in values if value is not None]
    return mean(found) if found else None


def _cap(result: CaseResult, variant: str) -> Optional[int]:
    return result.case.saved_cap if variant == SAVED else int(variant)


def _pairs(run: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Each judged pair once, in the order the report lists them."""
    found: List[Tuple[str, str]] = []
    for first, second in steps.pairs(run["caps"], run.get("baselines") or []):
        if (second, first) not in found:
            found.append((first, second))
    return found


def rows(results: List[CaseResult], names: List[str]) -> List[Row]:
    table = []
    for name in names:
        covered, gaps, wrong, chars, seconds, over, count = [], [], [], [], [], 0, 0
        for result in results:
            if name not in result.bodies:
                continue
            text = result.bodies[name]
            count += 1
            chars.append(len(text))
            if name not in BASELINES:  # a baseline isn't held to ContextRail's template
                over += bool(handoff.problems(text, _cap(result, name) or len(text)))
            if name in result.gens and result.gens[name].get("duration_ms") is not None:
                seconds.append(result.gens[name]["duration_ms"] / 1000)
            judged = result.verdicts(name)
            if judged:
                covered.append(mean(coverage(each) for each in judged))
                wrong.append(mean(each.count("wrong") for each in judged))
            gap_judged = [each for each in result.verdicts(name, gaps_only=True) if each]
            if gap_judged:
                gaps.append(mean(coverage(each) for each in gap_judged))
        table.append(Row(name, _mean(covered), _mean(gaps), _mean(wrong), mean(chars) if chars else 0.0,
                         None if name in BASELINES else over, count, _mean(seconds)))
    return table


def wins(results: List[CaseResult], x: str, y: str) -> Dict[str, int]:
    """How often each of two handoffs was judged the better one, over both orders."""
    tally = {x: 0, y: 0, "tie": 0}
    for result in results:
        for judgment in result.judgments:
            if {judgment["first"], judgment["second"]} == {x, y}:
                tally[judgment["better"]] += 1
    return tally


def _label(name: str, results: List[CaseResult]) -> str:
    if name == NOTES:
        return "Plain notes"
    if name == FACTS:
        return "Facts only"
    if name != SAVED:
        return f"Cap {int(name):,}"
    caps = {result.case.saved_cap for result in results}
    return f"Saved (cap {caps.pop():,})" if len(caps) == 1 and None not in caps else "Saved"


def _pct(value: Optional[float]) -> str:
    return "–" if value is None else f"{value * 100:.0f}%"


def _num(value: Optional[float], digits: int = 1) -> str:
    return "–" if value is None else f"{value:,.{digits}f}"


def render(results: List[CaseResult], run: Dict[str, Any]) -> str:
    names = steps.variants(run["caps"], run.get("baselines") or [])
    writers = sorted({f"{r.case.model} ({r.case.effort} effort)" if r.case.effort else r.case.model for r in results})
    day = str(run.get("started") or "")[:10]
    version = f" with Claude Code {run['claude'].split()[0]}" if run.get("claude") else ""
    method = f", benchmark {run['benchmark']}, ContextRail {run['contextrail']}" if run.get("benchmark") else ""
    lines = [
        f"Run of {day}{version}{method}: {len(results)} cases, {sum(len(r.needs) for r in results)} needs. "
        f"Handoffs written by {', '.join(writers)}; needs and verdicts by {run['judge']}. "
        "Each pair of handoffs was judged in both orders. "
        f"Cost as Claude Code reported it: ${sum(r.cost() for r in results):.2f}.",
        "",
        "| Handoff | Coverage | Gaps covered | Wrong per case | Mean length | Over a limit | Mean time |",
        "|---|--:|--:|--:|--:|--:|--:|",
    ]
    for row in rows(results, names):
        time = "–" if row.seconds is None else f"{row.seconds:.0f} s"
        over = "–" if row.over is None else f"{row.over} of {row.count}"
        lines.append(
            f"| {_label(row.variant, results)} | {_pct(row.coverage)} | {_pct(row.gaps)} | {_num(row.wrong)} "
            f"| {row.chars:,.0f} | {over} | {time} |"
        )
    held_out = [result for result in results if cases.split(result.case.key) == "test"]
    if held_out and len(held_out) < len(results):
        shown = ", ".join(f"{_label(row.variant, results)} {_pct(row.coverage)}" for row in rows(held_out, names))
        lines += ["", f"Coverage on the {len(held_out)} held-out case(s) alone: {shown}."]
    lines += ["", "| Pair | Wins | Ties |", "|---|--:|--:|"]
    for x, y in _pairs(run):
        tally = wins(results, x, y)
        lines.append(f"| {_label(x, results)} vs {_label(y, results)} | {tally[x]} – {tally[y]} | {tally['tie']} |")
    lines += [
        "",
        "| Case | Context at the handoff | Messages in B | Needs | " + " | ".join(_label(n, results) for n in names) + " |",
        "|---|--:|--:|--:|" + "--:|" * len(names),
    ]
    for number, result in enumerate(results, 1):
        cells = [_pct(_mean([coverage(each) for each in result.verdicts(name)])) for name in names]
        lines.append(
            f"| {number} | {result.case.tokens:,} | {result.case.messages} | {len(result.needs)} | " + " | ".join(cells) + " |"
        )
    return "\n".join(lines)


def write(path: Path, text: str) -> None:
    """Replace what is between the result markers in `path`, and nothing else."""
    document = path.read_text(encoding="utf-8")
    start, end = document.find(START), document.find(END)
    if start < 0 or end < start:
        raise EvalError(f"{path} has no {START} … {END} section")
    path.write_text(document[: start + len(START)] + "\n" + text.strip() + "\n" + document[end:], encoding="utf-8")
