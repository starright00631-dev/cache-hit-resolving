"""Summaries use token-weighted ratios and keep accounting units separate."""

from collections import defaultdict

from .logs import Sample

PHASE_ADVICE = {
    "first_observed": "Keep the opening tools and shared instructions stable across similar tasks.",
    "after_compaction": "Check the following call too. Compaction can reduce reuse while shortening history.",
    "after_idle": "Check short-gap calls separately. A long pause can reduce reuse.",
    "model_changed": "Keep the same model when testing consecutive requests.",
    "warm_follow_up": "Try a smaller tool-output budget and compare fresh tokens on the same task.",
}


def total(samples: list[Sample], target: float) -> dict:
    inputs = sum(row.input_tokens for row in samples)
    cached = sum(row.cached for row in samples)
    outputs = [row.output for row in samples if row.output is not None]
    writes = [row.cache_write for row in samples if row.cache_write is not None]
    return {
        "samples": len(samples),
        "input_tokens": inputs,
        "cached_input_tokens": cached,
        "fresh_input_tokens": inputs - cached,
        "output_tokens": sum(outputs) if samples and len(outputs) == len(samples) else None,
        "cache_write_input_tokens": sum(writes) if samples and len(writes) == len(samples) else None,
        "cache_reuse": cached / inputs if inputs else None,
        "zero_reuse_samples": sum(row.cached == 0 for row in samples),
        "at_target_samples": sum(row.cached / row.input_tokens >= target for row in samples),
    }


def audit(samples: list[Sample], target: float) -> dict:
    scopes = defaultdict(list)
    for row in samples:
        scopes[row.scope].append(row)

    report = {}
    for scope, rows in sorted(scopes.items()):
        phases = defaultdict(list)
        models = defaultdict(list)
        for row in rows:
            phases[row.phase].append(row)
            models[row.model, row.version].append(row)

        by_phase = {phase: total(group, target) for phase, group in sorted(phases.items())}
        report[scope] = {
            "total": total(rows, target),
            "by_phase": by_phase if scope == "request" else {},
            "by_model_and_client": [
                {"model": model, "client_version": version, **total(group, target)}
                for (model, version), group in sorted(models.items())
            ],
            "suggestions": [
                PHASE_ADVICE[phase] for phase, group in by_phase.items()
                if phase in PHASE_ADVICE and group["cache_reuse"] < target
            ],
        }
    return report


def compare(baseline: list[Sample], candidate: list[Sample], target: float) -> dict:
    units = {row.scope for row in baseline + candidate}
    if not baseline or not candidate:
        raise ValueError("Each comparison log must have usable input and cached token counts.")
    if len(units) != 1:
        raise ValueError("Compare two rollout logs or two Codex exec logs of the same kind.")
    left, right = total(baseline, target), total(candidate, target)
    models = {row.model for row in baseline + candidate} - {"unknown"}
    return {
        "scope": units.pop(),
        "baseline": left,
        "candidate": right,
        "fresh_input_change": right["fresh_input_tokens"] - left["fresh_input_tokens"],
        "input_change": right["input_tokens"] - left["input_tokens"],
        "reuse_change_percentage_points": 100 * (right["cache_reuse"] - left["cache_reuse"]),
        "known_model_mismatch": len(models) > 1,
    }
