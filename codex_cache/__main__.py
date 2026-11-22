"""Command-line interface for Codex Cache Resolve."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import logs, report

DEFAULT_TOOL_LIMIT = 2000


def positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return result


def ratio(value: str) -> float:
    result = float(value)
    if not 0 < result <= 1:
        raise argparse.ArgumentTypeError("expected a fraction greater than 0 and at most 1")
    return result


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        prog="codex-cache-resolve",
        description="Measure prompt reuse and test a smaller Codex tool-output budget.",
    )
    commands = cli.add_subparsers(dest="command", required=True)
    audit = commands.add_parser("audit", help="summarize local Codex usage")
    audit.add_argument("logs", type=Path, nargs="*", help="rollout files or Codex exec JSONL captures")
    audit.add_argument("--codex-home", type=Path, help="defaults to CODEX_HOME or ~/.codex")
    audit.add_argument("--days", type=positive_int, help="lookback (default 7 days for discovery)")
    audit.add_argument("--workspace", type=Path, help="match the recorded working directory exactly")
    compare = commands.add_parser("compare", help="compare two matched runs")
    compare.add_argument("baseline", type=Path)
    compare.add_argument("candidate", type=Path)
    for command in (audit, compare):
        command.add_argument("--target", type=ratio, default=0.99, help="reference cache fraction")
        command.add_argument("--json", action="store_true", help="print an aggregate JSON report")
    run = commands.add_parser("run", help="launch Codex with a one-session tool-output budget")
    run.add_argument("--tool-limit", type=positive_int, default=DEFAULT_TOOL_LIMIT)
    run.add_argument("codex_args", nargs=argparse.REMAINDER, help="Codex arguments after --")
    return cli


def reuse(summary: dict) -> str:
    value = summary["cache_reuse"]
    return "n/a" if value is None else f"{value:.2%}"


def row(label: str, summary: dict) -> None:
    print(
        f"  {label}: {summary['samples']:,} samples, {reuse(summary)} reused, "
        f"{summary['fresh_input_tokens']:,} fresh / {summary['input_tokens']:,} input"
    )


def print_notes(notes: dict) -> None:
    for name, value in sorted(notes.items()):
        print(f"  {name.replace('_', ' ')}: {value}", file=sys.stderr)


def audit_command(args: argparse.Namespace) -> int:
    note_counts: Counter = Counter()
    days = args.days if args.days is not None else (None if args.logs else 7)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    workspace = args.workspace.expanduser().resolve() if args.workspace else None

    paths = [path.expanduser() for path in args.logs]
    if not paths:
        home = args.codex_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        paths = logs.recent_rollouts(home.expanduser(), cutoff)

    samples = logs.collect(paths, note_counts, workspace, cutoff)
    summaries = report.audit(samples, args.target)
    result = {
        "target": args.target,
        "files_considered": len(paths),
        "summaries": summaries,
        "notes": dict(note_counts),
    }
    if args.json:
        print(json.dumps(result, indent=2))
    elif summaries:
        for scope, summary in summaries.items():
            print(f"{scope.title()} usage")
            row("All", summary["total"])
            for phase, breakdown in summary["by_phase"].items():
                row(phase.replace("_", " "), breakdown)
            for group in summary["by_model_and_client"]:
                row(f"{group['model']} ({group['client_version']})", group)
            for advice in summary["suggestions"]:
                print(f"  - {advice}")
        print_notes(note_counts)
    else:
        print("No usable usage records found. Check the log path, date range, or workspace filter.", file=sys.stderr)
        print_notes(note_counts)
    return 0 if samples else 1


def compare_command(args: argparse.Namespace) -> int:
    left_notes: Counter = Counter()
    right_notes: Counter = Counter()
    baseline = logs.collect([args.baseline.expanduser()], left_notes)
    candidate = logs.collect([args.candidate.expanduser()], right_notes)
    problems = {"baseline": dict(left_notes), "candidate": dict(right_notes)}
    try:
        if args.baseline.resolve() == args.candidate.resolve():
            raise ValueError("Choose two different logs.")
        if left_notes["failed_cli_turns"] or right_notes["failed_cli_turns"]:
            raise ValueError("A captured Codex turn failed. Compare two successful runs.")
        result = report.compare(baseline, candidate, args.target)
    except ValueError as error:
        if args.json:
            print(json.dumps({"error": str(error), "notes": problems}, indent=2))
        else:
            print(error, file=sys.stderr)
            print_notes(left_notes + right_notes)
        return 1

    result["notes"] = problems
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Comparison ({result['scope']} usage)")
        row("Baseline", result["baseline"])
        row("Candidate", result["candidate"])
        print(f"  Fresh input change: {result['fresh_input_change']:+,} tokens")
        print(f"  Total input change: {result['input_change']:+,} tokens")
        print(f"  Reuse change: {result['reuse_change_percentage_points']:+.2f} percentage points")
        if result["known_model_mismatch"]:
            print("  Recorded models differ. Rerun with the same model.")
        print_notes(left_notes + right_notes)
    return 0


def codex_command(lookup=shutil.which, windows: bool = os.name == "nt") -> list[str]:
    if not windows:
        executable = lookup("codex")
        if executable:
            return [executable]
    else:
        executable = lookup("codex.exe")
        if executable:
            return [executable]
        shim = lookup("codex.cmd")
        node = lookup("node.exe")
        if shim and node:
            entrypoint = Path(shim).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            if entrypoint.is_file():
                return [node, str(entrypoint)]
    raise FileNotFoundError("Codex CLI was not found on PATH. Install Codex or run the override directly.")


def run_command(args: argparse.Namespace) -> int:
    extra = args.codex_args
    if extra[:1] == ["--"]:
        extra = extra[1:]
    command = [*codex_command(), "-c", f"tool_output_token_limit={args.tool_limit}", *extra]
    return subprocess.call(command)


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    try:
        if args.command == "audit":
            return audit_command(args)
        if args.command == "compare":
            return compare_command(args)
        return run_command(args)
    except (OSError, OverflowError) as error:
        print(f"codex-cache-resolve: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
