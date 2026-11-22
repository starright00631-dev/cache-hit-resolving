"""Compare default and capped Codex runs on a small read-only task."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

# This script also works from a checkout before installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_cache.__main__ import codex_command, positive_int
from codex_cache.logs import collect
from codex_cache.report import compare


def probe(command: list[str], workspace: Path, log: Path, expected: str, timeout: int):
    read_fixture = "Get-Content -LiteralPath sample.txt" if os.name == "nt" else "cat sample.txt"
    prompt = (
        f"Read the entire file with exactly this one command: `{read_fixture}`. "
        "Use an output budget of 8000 tokens if the tool offers one. "
        "Reply with the first line of the file only, with no quotes or markdown."
    )
    start = time.monotonic()
    result = subprocess.run(
        [*command, prompt], cwd=workspace, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
    )
    seconds = round(time.monotonic() - start, 2)
    messages = []
    failure = False
    command_exits = []
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        failure |= kind == "turn.failed"
        item = event.get("item")
        if kind != "item.completed" or not isinstance(item, dict):
            continue
        if item.get("type") == "command_execution":
            exit_code = item.get("exit_code")
            command_exits.append(exit_code if type(exit_code) is int else None)
        if item.get("type") == "agent_message":
            messages.append(item.get("text"))
    answer_matches = bool(
        messages and isinstance(messages[-1], str) and messages[-1].strip() == expected
    )
    # Some tool implementations do not emit per-command items in exec JSON.
    passed = (
        result.returncode == 0 and not failure and answer_matches
        and len(command_exits) <= 1 and all(code in (0, None) for code in command_exits)
    )
    # These captures may contain tool output. They exist only in the temp directory.
    log.write_bytes(result.stdout)
    return {
        "passed": passed, "seconds": seconds, "exit_code": result.returncode,
        "command_exit_codes": command_exits, "answer_matches": answer_matches,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Codex model for both arms")
    parser.add_argument("--tool-limit", type=positive_int, default=2000)
    parser.add_argument("--timeout", type=positive_int, default=150, help="seconds per trial")
    parser.add_argument("--candidate-first", action="store_true", help="reverse the trial order")
    args = parser.parse_args()

    try:
        codex = codex_command()
        options = [
            "exec", "--skip-git-repo-check",
            "--sandbox", "read-only", "--ephemeral", "--json",
        ]
        if args.model:
            options.extend(("--model", args.model))

        with tempfile.TemporaryDirectory(prefix="codex-cache-bench-") as scratch:
            root = Path(scratch)
            workspace = root / "workspace"
            workspace.mkdir()
            marker = f"HEADER-{secrets.token_hex(6)}"
            lines = [
                f"record {i:04d}: alpha bravo charlie delta echo foxtrot golf hotel"
                for i in range(700)
            ]
            (workspace / "sample.txt").write_text(
                "\n".join([marker, *lines]) + "\n", encoding="ascii",
            )
            arms = {
                "baseline": [*codex, *options],
                "candidate": [*codex, "-c", f"tool_output_token_limit={args.tool_limit}", *options],
            }
            order = ("candidate", "baseline") if args.candidate_first else tuple(arms)
            runs = {}
            usage = {}
            for name in order:
                log = root / f"{name}.jsonl"
                runs[name] = probe(arms[name], workspace, log, marker, args.timeout)
                usage[name] = collect([log], Counter())

            result = {"trial_order": order, "tool_output_token_limit": args.tool_limit, "runs": runs}
            if all(run["passed"] for run in runs.values()):
                result["usage"] = compare(usage["baseline"], usage["candidate"], 0.99)
                print(json.dumps(result, indent=2))
                return 0
            print(json.dumps(result, indent=2))
            print("A trial failed the task or command-count check. No savings claim.", file=sys.stderr)
            return 1
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        if isinstance(error, subprocess.TimeoutExpired):
            print("A trial timed out. No savings claim.", file=sys.stderr)
        else:
            print(f"Benchmark incomplete: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
