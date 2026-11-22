# Codex Cache Resolve

Measure how much of a Codex prompt was reused. Then compare the same task with a smaller tool-output budget. The project works with local logs from Codex Desktop and CLI and uses only Python's standard library at runtime.

A cache hit saves work reading the unchanged beginning of a prompt. Aim for high reuse on **warm follow-ups**, and compare **fresh input tokens, elapsed time, and task correctness** across the whole task. Startup and compaction can lower reuse. Codex's provider controls what is cached and when. [OpenAI prompt caching guide](https://developers.openai.com/api/docs/guides/prompt-caching)

## Get started

Install Python 3.11 or later, then run these commands in PowerShell or a terminal.

```sh
git clone https://github.com/starright00631-dev/cache-hit-resolving.git
cd cache-hit-resolving
python -m pip install .
codex-cache-resolve audit
```

From the checkout, `python -m codex_cache` can replace `codex-cache-resolve` without installing. The audit needs local logs and can run offline. The launcher and live benchmark require the Codex CLI on PATH and its normal sign-in.

### Audit desktop or CLI

```sh
codex-cache-resolve audit --days 7
codex-cache-resolve audit --workspace .
codex-cache-resolve audit path/to/rollout.jsonl --json
```

By default, the audit finds recent rollouts under `$CODEX_HOME/sessions` or `~/.codex/sessions`. Pass a file directly to include an archived session. The workspace filter uses the working directory recorded in the rollout. A chat running remotely or with local persistence turned off may have no rollout to audit.

- **Reuse** is cached input divided by total input, weighted by token count. **Fresh input** is total input minus cached input and can include cache writes.
- Rollout reports classify the first observed call, model changes, the first call after compaction, and calls after gaps of at least 30 minutes when timestamps are available. The groups describe what was recorded. The tool cannot see the provider's exact cache decision.
- Logs from `codex exec --json` contain **turn** totals. A turn may include several model calls. The comparison keeps turn totals and per-request rollout totals separate.
- The tool reads usage locally and reports counts, model names, and client versions. It sends no telemetry and prints no prompts, tool output, session paths, or request IDs in reports.

### Try a smaller tool-output budget

```sh
codex-cache-resolve run --tool-limit 2000
codex-cache-resolve run --tool-limit 2000 -- exec "Find the cause of the failing test"
```

The launcher passes `-c tool_output_token_limit=2000` to one CLI invocation. The setting caps individual tool results stored in history. Smaller results can save input tokens and can also remove information the model needs. Start with a task that emits long logs, and check the result. To try the same setting in Desktop, set `tool_output_token_limit = 2000` at the root of your Codex user configuration and start a fresh chat after the app loads the setting. Record any previous value so you can restore it. [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference#configtoml)

### Compare a baseline and a candidate

Capture two fresh runs using the **same model, task prompt, workspace state, and tools**. Prefer read-only tasks, or reset a disposable workspace to the same committed state between runs. Check that both runs pass your task's acceptance test. The comparison reads counts and detects reported CLI failures. Task-specific correctness stays with your acceptance test.

```sh
codex exec --json --sandbox read-only "Read the test report and identify the failing test" > baseline.jsonl
codex-cache-resolve run --tool-limit 2000 -- exec --json --sandbox read-only "Read the test report and identify the failing test" > candidate.jsonl
codex-cache-resolve compare baseline.jsonl candidate.jsonl
```

Compare fresh and total input even when the cache percentage moves in the other direction. For instance, 95% reuse of 10,000 input tokens leaves 500 fresh tokens. At 99% reuse of 100,000 input tokens, 1,000 are fresh.

### Run the included live benchmark

```sh
python examples/benchmark.py --model MODEL
python examples/benchmark.py --model MODEL --candidate-first
```

Replace `MODEL` with a model available to your account, or omit the flag to use the CLI's default. The script makes two real Codex calls in separate sessions in a temporary directory. Both runs are asked to read a long file with one command and must return its generated header. The script also checks command exit codes and extra commands when the CLI reports them. Both runs use your Codex configuration and a read-only sandbox, with the candidate overriding the tool-output budget. It reports elapsed time and usage after both pass. Reverse the order and repeat to see how much results vary with cache warmth. This synthetic task demonstrates tool-output behavior. It cannot establish savings on a different workload or guarantee 99% reuse.

## Development

```sh
python -m unittest discover -s tests -v
```

The legacy usage accounting approach was informed by [Codex Cache Monitor](https://github.com/Nver-theless/codex-cache-monitor/blob/main/src/codex_cache_monitor/parser.py). The idea of measuring reuse around history changes was informed by the [Duck harness measurements](https://github.com/da-fr/arc-agi-3-solution/blob/main/WRITEUP.md). The scanner uses both modern per-response records and older cumulative snapshots so that one response contributes to the report once.
