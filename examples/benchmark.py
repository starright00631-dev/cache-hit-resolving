"""Compare default and capped Codex runs on a small read-only task."""

from __future__ import annotations

import argparse
import json
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
    prompt = (
        "Run python sample.py once. Reply with the first and last lines of its output "
        "joined by |, with no spaces, quotes, or markdown. Do not read the source file."
    )
    start = time.monotonic()
    result = subprocess.run(
        [*command, prompt], cwd=workspace, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
    )
    seconds = round(time.monotonic() - start, 2)
    messages = []
    failure = False
    commands_run = 0
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
            commands_run += 1
        if item.get("type") == "agent_message":
            messages.append(item.get("text"))
    passed = result.returncode == 0 and not failure and commands_run == 1 and bool(
        messages and isinstance(messages[-1], str) and messages[-1].strip() == expected
    )
    # These captures may contain tool output. They exist only in the temp directory.
    log.write_bytes(result.stdout)
    return {
        "passed": passed, "seconds": seconds, "exit_code": result.returncode,
        "commands_run": commands_run,
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
            "exec", "--ignore-user-config", "--skip-git-repo-check",
            "--sandbox", "read-only", "--ephemeral", "--json",
        ]
        if args.model:
            options.extend(("--model", args.model))

        with tempfile.TemporaryDirectory(prefix="codex-cache-bench-") as scratch:
            root = Path(scratch)
            workspace = root / "workspace"
            workspace.mkdir()
            first, last = secrets.token_hex(4), secrets.token_hex(4)
            (workspace / "sample.py").write_text(
                f"print({first!r})\n"
                "for i in range(1400):\n"
                "    print(f'record {i:04d}: alpha bravo charlie delta echo foxtrot golf hotel')\n"
                f"print({last!r})\n",
                encoding="ascii",
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
                runs[name] = probe(arms[name], workspace, log, f"{first}|{last}", args.timeout)
                usage[name] = collect([log], Counter())

            result = {"trial_order": order, "tool_output_token_limit": args.tool_limit, "runs": runs}
            if all(run["passed"] for run in runs.values()):
                result["usage"] = compare(usage["baseline"], usage["candidate"], 0.99)
                print(json.dumps(result, indent=2))
                return 0
            print(json.dumps(result, indent=2))
            print("A trial failed the answer or one-command check. No savings claim.", file=sys.stderr)
            return 1
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        if isinstance(error, subprocess.TimeoutExpired):
            print("A trial timed out. No savings claim.", file=sys.stderr)
        else:
            print(f"Benchmark incomplete: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
