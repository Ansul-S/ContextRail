"""python3 -m tools.eval cases | run | report | export | agreement. See docs/eval.md."""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from contextrail import __version__, config

from . import cases, report, review, steps
from .cases import Case
from .steps import BASELINES, EvalError, Harness, read_json

# (case, step, its arguments)
Call = Tuple[Case, str, Tuple[Any, ...]]
ACTIONS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "generate": steps.generate,
    "notes": steps.generate_notes,
    "needs": steps.needs,
    "judge": steps.judge,
}


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def describe(step: str, args: Tuple[Any, ...]) -> str:
    if step == "generate":
        return f"regenerate the handoff at cap {args[0]:,}"
    if step == "notes":
        return "write plain notes (baseline)"
    if step == "needs":
        return "list what B needed"
    return f"judge {args[0]} (shown first) against {args[1]}"


def plan(h: Harness, chosen: List[Case], caps: List[int], baselines: List[str] = ()) -> List[Call]:
    """The calls still to make. Per case, the regenerations come back to back, as they
    share A's context and so its prompt cache; then the needs, then the judging."""
    todo: List[Call] = []
    for case in chosen:
        todo += [(case, "generate", (cap,)) for cap in caps if not steps.gen_path(h, case, cap).exists()]
        if steps.NOTES in baselines and not steps.notes_path(h, case).exists():
            todo.append((case, "notes", ()))
        if not steps.needs_path(h, case).exists():
            todo.append((case, "needs", ()))
        todo += [
            (case, "judge", pair) for pair in steps.pairs(caps, baselines)
            if not steps.judge_path(h, case, *pair).exists()
        ]
    return todo


def stored(h: Harness) -> Dict[str, Case]:
    """Cases saved by earlier runs, by key."""
    found = {}
    for path in sorted((h.root / "cases").glob("*/case.json")):
        case = Case.from_json(read_json(path))
        found[case.key] = case
    return found


def select(h: Harness, discovered: List[Case], keys: List[str], limit: int, split: str = "all") -> List[Case]:
    """The cases to run: the keys given, or the oldest `limit` of the split. A stored case wins over
    the same one found again, so a rerun works on what the first run saw."""
    known = {case.key: case for case in discovered}
    known.update(stored(h))
    if not keys:
        wanted = [case for case in discovered if split == "all" or cases.split(case.key) == split]
        return [known[case.key] for case in wanted[:limit]]
    unknown = [key for key in keys if key not in known]
    if unknown:
        raise EvalError(f"unknown case keys {', '.join(unknown)}; `python3 -m tools.eval cases` lists them")
    if len(keys) > limit:
        raise EvalError(f"{len(keys)} cases asked for, more than --limit {limit}")
    return [known[key] for key in keys]


def run(h: Harness, chosen: List[Case], caps: List[int], dry_run: bool, baselines: List[str] = ()) -> int:
    todo = plan(h, chosen, caps, baselines)
    against = f", baselines {', '.join(baselines)}" if baselines else ""
    print(f"{len(chosen)} case(s), caps {', '.join(f'{cap:,}' for cap in caps)}{against}, judge {h.judge_model}: "
          f"{len(todo)} call(s) to make.")
    for case, step, args in todo:
        print(f"  {case.key}: {describe(step, args)}")
    if dry_run:
        return 0
    h.root.mkdir(parents=True, exist_ok=True)
    record: Dict[str, Any] = {
        "started": _now(), "benchmark": steps.BENCHMARK, "contextrail": __version__, "caps": caps, "baselines": list(baselines), "cases": [case.key for case in chosen],
        "splits": {case.key: cases.split(case.key) for case in chosen}, "judge": h.judge_model,
        "claude": steps.claude_version(h), "calls": 0, "error": None,
    }
    try:
        for case in chosen:
            path = h.case_dir(case) / "case.json"
            if not path.exists():
                steps.write_json(path, case.to_json())
        for case, step, args in todo:
            began = time.monotonic()
            result = ACTIONS[step](h, case, *args)
            record["calls"] += 1
            cost = result.get("cost_usd")
            print(f"  {case.key}: {describe(step, args)}: done in {time.monotonic() - began:.0f} s"
                  + (f", ${cost:.2f}" if isinstance(cost, (int, float)) else ""), flush=True)
    except EvalError as error:
        record["error"] = str(error)
        print(f"Stopped: {error}", file=sys.stderr)
        return 1
    finally:
        record["finished"] = _now()
        steps.write_json(h.root / "run.json", record)
    print("Done. `python3 -m tools.eval report` shows the numbers.")
    return 0


def show_report(h: Harness, write: bool) -> int:
    path = h.root / "run.json"
    if not path.exists():
        raise EvalError(f"no run yet ({path} is missing)")
    data = read_json(path)
    text = report.render(report.load(h, data), data)
    print(text)
    if write:
        report.write(report.DOC, text)
        print(f"Written to {report.DOC}.")
    return 0


def show_cases(project: Path) -> int:
    found, notes = cases.discover(project)
    for case in found:
        print(f"{case.key}  {cases.split(case.key):4}  B {case.b[:8]}  {case.tokens:,} tokens at the fork  {case.messages} message(s) in B  "
              f"asked by {case.asked_by}  saved {len(case.saved_body):,} characters")
    for note in notes:
        print(f"  left out: {note}")
    return 0


def _caps(text: str) -> List[int]:
    try:
        caps = [int(part.replace("_", "").replace(",", "")) for part in text.split(",") if part.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a list of numbers: {text!r}") from None
    if not caps or any(cap < 1_000 for cap in caps):
        raise argparse.ArgumentTypeError("caps are character counts of at least 1,000")
    return caps


def _baselines(text: str) -> List[str]:
    names = [part.strip() for part in text.split(",") if part.strip()]
    unknown = [name for name in names if name not in BASELINES]
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown baselines {', '.join(unknown)}; there are {', '.join(BASELINES)}")
    return names


def main(argv: Optional[List[str]] = None, harness: Optional[Harness] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m tools.eval", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("cases", help="list the cases found in this project's transcripts")
    listing.add_argument("--project", type=Path, default=Path.cwd())
    running = commands.add_parser("run", help="regenerate, list needs and judge; skips what exists")
    running.add_argument("--caps", type=_caps, default=[6_000, 8_000])
    running.add_argument("--limit", type=int, default=5)
    running.add_argument("--cases", default="", help="comma-separated keys from `cases`")
    running.add_argument("--judge", default="opus", help="model for the needs and the verdicts")
    running.add_argument("--baselines", type=_baselines, default=[], help=f"also judge against: {', '.join(BASELINES)}")
    running.add_argument("--split", choices=("all", "tune", "test"), default="all",
                         help="only cases of this split (cases lists each case's)")
    running.add_argument("--dry-run", action="store_true", help="show the calls without making them")
    running.add_argument("--project", type=Path, default=Path.cwd())
    reporting = commands.add_parser("report", help="the numbers of the last run")
    reporting.add_argument("--write", action="store_true", help="also update docs/eval.md")
    exporting = commands.add_parser("export", help="blinded review sheets for people, from the last run")
    exporting.add_argument("--out", type=Path, required=True, help="a new folder for the sheets")
    exporting.add_argument("--seed", type=int, default=None, help="for the order of the documents")
    agreeing = commands.add_parser("agreement", help="reviewers' sheets against each other and the model judge")
    agreeing.add_argument("sheets", type=Path, nargs="+")
    agreeing.add_argument("--key", type=Path, required=True, help="the key export wrote beside the folder")
    args = parser.parse_args(argv)

    h = harness or Harness(config.home() / "eval")
    try:
        if args.command == "cases":
            return show_cases(args.project)
        if args.command == "report":
            return show_report(h, args.write)
        if args.command == "export":
            return review.export(h, args.out, args.seed)
        if args.command == "agreement":
            return review.agreement(h, args.sheets, args.key)
        h.judge_model = args.judge
        discovered, _ = cases.discover(args.project)
        keys = [key.strip() for key in args.cases.split(",") if key.strip()]
        return run(h, select(h, discovered, keys, args.limit, args.split), args.caps, args.dry_run, args.baselines)
    except EvalError as error:
        print(f"tools.eval: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
