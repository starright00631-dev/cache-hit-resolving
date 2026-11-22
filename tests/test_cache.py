import codecs
import contextlib
import io
import json
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from codex_cache.__main__ import codex_command, main
from codex_cache.logs import collect
from codex_cache.report import compare, audit


def event(kind, payload, at="2026-11-21T00:00:00Z"):
    return {"type": kind, "timestamp": at, "payload": payload}


def usage(inputs, cached, output=1):
    return {"input_tokens": inputs, "cached_input_tokens": cached, "output_tokens": output}


def modern(response_id, inputs, cached, at="2026-11-21T00:00:01Z"):
    return event("token_usage_record", {"response_id": response_id, "usage": usage(inputs, cached)}, at)


def legacy(last, total):
    return event(
        "event_msg",
        {"type": "token_count", "info": {"last_token_usage": last, "total_token_usage": total}},
    )


class CacheTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.path = Path(scratch.name).resolve()

    def log(self, name, rows, encoding="utf-8"):
        logfile = self.path / name
        logfile.write_bytes(("\n".join(json.dumps(row) for row in rows) + "\n").encode(encoding))
        return logfile

    def test_modern_and_legacy_do_not_double_count(self):
        log = self.log("rollout.jsonl", [
            event("turn_context", {"model": "test-model"}),
            legacy(usage(200, 100), usage(200, 100)),
            modern("r1", 200, 100),
            legacy(usage(200, 100), usage(200, 100)),
            modern("r2", 300, 250),
            legacy(usage(300, 250), usage(500, 350)),
            event("turn_context", {"model": "test-model"}),
            legacy(usage(400, 350), usage(900, 700)),
            legacy(usage(400, 350), usage(900, 700)),
        ])
        rows = collect([log], Counter())
        self.assertEqual([(row.input_tokens, row.cached) for row in rows], [(200, 100), (300, 250), (400, 350)])
        self.assertAlmostEqual(audit(rows, .99)["request"]["total"]["cache_reuse"], 700 / 900)

    def test_phases_workspace_and_date_filter(self):
        log = self.log("rollout.jsonl", [
            event("session_meta", {"cwd": str(self.path), "cli_version": "0.171.0"}),
            event("turn_context", {"model": "test-model"}),
            modern("r1", 200, 100),
            modern("r2", 300, 250, "2026-11-21T00:00:02Z"),
            event("compacted", {"message": "secret summary"}),
            modern("r3", 250, 100, "2026-11-21T00:00:03Z"),
            modern("r4", 260, 100, "2026-11-21T00:30:03Z"),
            event("turn_context", {"model": "another-model"}),
            modern("r5", 275, 0, "2026-11-21T00:30:04Z"),
        ])
        self.assertFalse(collect([log], Counter(), self.path / "another"))
        rows = collect(
            [log], Counter(), self.path,
            datetime(2026, 11, 21, 0, 0, 2, tzinfo=timezone.utc),
        )
        self.assertEqual([row.phase for row in rows], [
            "warm_follow_up", "after_compaction", "after_idle", "model_changed",
        ])
        self.assertEqual(rows[0].version, "0.171.0")

    def test_duplicate_responses_and_partial_live_log(self):
        row = modern("duplicate", 400, 300)
        one = self.log("one.jsonl", [row])
        two = self.log("two.jsonl", [row, modern("new", 600, 550)])
        with two.open("ab") as stream:
            stream.write(b'{"type":"token_usage_record","payload":')
        notes = Counter()
        self.assertEqual(len(collect([one, two, one], notes)), 2)
        self.assertEqual(notes, {"duplicate_responses": 1, "invalid_usage_related_lines": 1})

    def test_usage_validation_and_utf16_cli_capture(self):
        log = self.log("exec.jsonl", [
            {"type": "turn.completed", "usage": usage(100, 80)},
            {"type": "turn.completed", "usage": usage(99, 100)},
            {"type": "turn.completed", "usage": {"input_tokens": 100}},
        ], encoding="utf-16")
        self.assertTrue(log.read_bytes().startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)))
        notes = Counter()
        rows = collect([log], notes)
        self.assertEqual(notes["invalid_or_missing_usage"], 2)
        self.assertEqual([(row.scope, row.fresh) for row in rows], [("turn", 20)])

    def test_comparison_refuses_different_accounting_units(self):
        one = self.log("base.jsonl", [modern("r1", 100, 90)])
        two = self.log("candidate.jsonl", [{"type": "turn.completed", "usage": usage(75, 70)}])
        with self.assertRaisesRegex(ValueError, "same kind"):
            compare(collect([one], Counter()), collect([two], Counter()), .99)

    def test_audit_report_omits_conversation_text_and_identifiers(self):
        log = self.log("private-log.jsonl", [
            event("session_meta", {"cwd": str(self.path), "id": "secret-thread"}),
            event("response_item", {"text": "secret-prompt"}),
            modern("secret-response", 100, 80),
        ])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["audit", str(log), "--json"]), 0)
        encoded = stdout.getvalue()
        self.assertNotIn("secret", encoded)
        self.assertNotIn(self.path.name, encoded)
        self.assertEqual(json.loads(encoded)["summaries"]["request"]["total"]["fresh_input_tokens"], 20)

    def test_compare_reports_a_gain_even_with_a_lower_percentage(self):
        one = self.log("base.jsonl", [{"type": "turn.completed", "usage": usage(1000, 900)}])
        two = self.log("candidate.jsonl", [{"type": "turn.completed", "usage": usage(500, 425)}])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["compare", str(one), str(two), "--json"]), 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["fresh_input_change"], -25)
        self.assertAlmostEqual(result["reuse_change_percentage_points"], -5)

    def test_compare_rejects_failed_exec_even_with_usage(self):
        one = self.log("base.jsonl", [{"type": "turn.completed", "usage": usage(1000, 900)}])
        two = self.log("candidate.jsonl", [
            {"type": "turn.completed", "usage": usage(500, 450)}, {"type": "turn.failed"},
        ])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["compare", str(one), str(two), "--json"]), 1)
        self.assertIn("failed", json.loads(stdout.getvalue())["error"])

    def test_run_passes_arguments_without_shell_interpretation(self):
        with patch("codex_cache.__main__.codex_command", return_value=["codex.exe"]), patch(
            "codex_cache.__main__.subprocess.call", return_value=7,
        ) as launch:
            self.assertEqual(main(["run", "--tool-limit", "900", "--", "exec", "x & y"]), 7)
        launch.assert_called_once_with(["codex.exe", "-c", "tool_output_token_limit=900", "exec", "x & y"])

    def test_windows_npm_fallback_uses_node_without_running_batch_script(self):
        script = self.path / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        script.parent.mkdir(parents=True)
        script.touch()
        paths = {"codex.cmd": str(self.path / "codex.cmd"), "node.exe": "node.exe"}
        self.assertEqual(codex_command(paths.get, windows=True), ["node.exe", str(script)])
        self.assertEqual(codex_command({"codex": "/bin/codex"}.get, windows=False), ["/bin/codex"])


if __name__ == "__main__":
    unittest.main()
