from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.settings import ROOT

# (label, TaskResult field, is_subset). Ordered from the phases that dominate the hot
# path outwards. A subset is measured INSIDE another phase, so the shares below are not
# additive — summing them can exceed 100%, and that is corrected for in the footnote
# rather than hidden by dropping the numbers.
PHASES: list[tuple[str, str, bool]] = [
    ("LLM", "llm_time_ms", False),
    ("+ provider spawn", "provider_startup_ms", True),
    ("+ provider after spawn", "provider_wait_ms", True),
    ("UI observation", "observation_time_ms", False),
    ("Action", "execution_time_ms", False),
    ("Verification", "verification_time_ms", False),
    ("+ readiness waits", "waits_ms", True),
    ("Worker startup", "startup_time_ms", False),
    ("Planning (lower bound)", "planning_time_ms", False),
]


def load(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                records.append(json.loads(line))
    return records


def numbers(rows: Iterable[dict[str, Any]], key: str) -> list[float]:
    """Numeric values of one field; missing keys (older records) are skipped."""
    values: list[float] = []
    for row in rows:
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        values.append(float(value))
    return values


def median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def p95(values: Sequence[float]) -> float | None:
    """Nearest-rank P95, matching the existing report's definition."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def shown(value: float | None, digits: int = 1) -> str:
    return f"{value:.{digits}f}" if value is not None else "n/a"


def phase_table(rows: list[dict[str, Any]]) -> list[str]:
    total = median(numbers(rows, "duration_ms"))
    lines = ["", "Phase breakdown (ms; share of median total duration):",
             f"{'phase':<28}{'P50':>12}{'P95':>12}{'share':>10}"]
    for label, key, _ in [("total duration", "", False), *PHASES]:
        values = numbers(rows, "duration_ms") if not key else numbers(rows, key)
        middle, tail = median(values), p95(values)
        if not key:
            share = "100.0%" if total is not None else "n/a"
        elif total is None or middle is None:
            share = "n/a"
        else:
            share = f"{100 * middle / total:.1f}%" if total else "n/a"
        lines.append(f"{label:<28}{shown(middle):>12}{shown(tail):>12}{share:>10}")
    lines.append("'+' marks a subset measured inside another phase (provider_* live inside LLM,"
                 " readiness waits inside Action/UI observation): shares are NOT additive.")
    return lines


def cold_warm_table(rows: list[dict[str, Any]]) -> list[str]:
    """§6: the two kinds of run are reported apart, never merged into one median.

    A record that predates the cold_start field is neither: it is excluded and
    counted, so an unknown sample is never silently filed as a cold start.
    """
    tagged = [row for row in rows if isinstance(row.get("cold_start"), bool)]
    lines = ["", "Cold vs warm (duration ms, P50 / P95):"]
    if len(tagged) != len(rows):
        lines.append(f"{len(rows) - len(tagged)} record(s) without cold_start are excluded "
                     "(written before FASE 1).")
    for label, expected in (("COLD", True), ("WARM", False)):
        group = [row for row in tagged if row["cold_start"] is expected]
        if not group:
            lines.append(f"{label:<5} no samples")
            continue
        durations = numbers(group, "duration_ms")
        calls = median(numbers(group, "llm_calls"))
        lines.append(f"{label:<5} P50={shown(median(durations))}  P95={shown(p95(durations))}  n={len(group)}"
                     f"  median LLM calls={shown(calls)}")
    lines.append("A task that fails stops the UFO worker, so the task AFTER a failure is COLD: "
                 "WARM samples are not a random subset.")
    return lines


def prompt_accounting(rows: list[dict[str, Any]]) -> list[str]:
    per_task = numbers(rows, "prompt_chars")
    calls_by_agent: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        for call in row.get("prompt_calls") or []:
            if not isinstance(call, dict):
                continue
            chars = call.get("chars")
            if isinstance(chars, bool) or not isinstance(chars, (int, float)):
                continue
            calls_by_agent[str(call.get("agent") or "unknown")].append(float(chars))
    call_chars = [value for values in calls_by_agent.values() for value in values]
    lines = ["", "Prompt accounting (characters, not tokens):"]
    if per_task:
        lines.append(f"Per task: P50={shown(median(per_task), 0)}  P95={shown(p95(per_task), 0)}  n={len(per_task)}")
    else:
        lines.append("Per task: n/a (records written before prompt_chars was recorded)")
    if call_chars:
        lines.append(f"Per LLM call: P50={shown(median(call_chars), 0)}  P95={shown(p95(call_chars), 0)}"
                     f"  calls={len(call_chars)}")
    else:
        lines.append("Per LLM call: n/a (records written before prompt_calls was recorded)")
    for agent, values in sorted(calls_by_agent.items()):
        lines.append(f"  {agent:<28} calls={len(values):<4} P50={shown(median(values), 0)} chars")
    return lines


def reliability(rows: list[dict[str, Any]]) -> list[str]:
    # Defined as SUCCESS despite an empty evidence list; NOT_VERIFIED is a
    # separate outcome and is never counted here.
    false_success = sum(1 for row in rows if row.get("false_success") is True)
    kinds = Counter(str(row.get("failure_kind") or "none") for row in rows)
    lines = ["", "Reliability:",
             f"False success (exactly: status SUCCESS with an EMPTY evidence list): {false_success}",
             "Evidence that is present but irrelevant is not detected by this test.",
             "Failure kinds: " + ", ".join(f"{kind}={count}" for kind, count in sorted(kinds.items()))]
    return lines


def uia_block(rows: list[dict[str, Any]]) -> list[str]:
    collected = numbers(rows, "uia_controls_collected")
    sent = numbers(rows, "uia_controls_sent")
    lines = ["", "UIA controls offered to the AppAgent prompt (0 when no AppAgent ran):",
             f"Collected: P50={shown(median(collected))}  P95={shown(p95(collected))}  n={len(collected)}",
             f"Sent to brain: P50={shown(median(sent))}  P95={shown(p95(sent))}  n={len(sent)}",
             "No filtering stage exists today, so collected equals sent by construction, not by measurement;"
             " the two only diverge when adaptive filtering is added."]
    return lines


def summarize(records: list[dict[str, Any]], scope: str = "end_to_end", provider: str = "") -> str:
    rows = [row for row in records if row.get("scope") == scope]
    available = sorted({str(row.get("provider") or "?") for row in rows})
    if provider:
        rows = [row for row in rows if row.get("provider") == provider]
    lines = [f"Scope: {scope}", f"Provider: {provider or 'ALL (merged; use --provider)'}",
             f"Providers present: {', '.join(available) or 'none'}", f"Tasks: {len(rows)}"]
    if not rows:
        return "\n".join(lines + ["No measured tasks in this scope. Engine-only tests do not validate the LLM pipeline."])
    counts = Counter(str(row.get("status") or "UNKNOWN") for row in rows)
    for status in ("SUCCESS", "FAILED", "NOT_VERIFIED", "CANCELLED"):
        lines.append(f"{status}: {counts.get(status, 0)}")
    lines.append(f"Success rate: {100 * counts.get('SUCCESS', 0) / len(rows):.1f}%")
    times = sorted(float(row["duration_ms"]) / 1000 for row in rows)
    lines.extend([f"Median latency (all tasks): {statistics.median(times):.3f}s",
                  f"P95 latency (nearest rank): {times[math.ceil(0.95 * len(times)) - 1]:.3f}s",
                  f"Median steps: {statistics.median(row['steps'] for row in rows):g}",
                  f"Average LLM calls: {statistics.mean(row['llm_calls'] for row in rows):.2f}"])
    for label, key in [("LLM", "llm_time_ms"), ("UI observation", "observation_time_ms"),
                       ("Action", "execution_time_ms"), ("Verification", "verification_time_ms"),
                       ("Startup", "startup_time_ms")]:
        lines.append(f"Median {label}: {statistics.median(float(row[key]) for row in rows):.1f}ms")
    ram = [row["average_ram_mb"] for row in rows if row.get("average_ram_mb") is not None]
    peak = [row["peak_ram_mb"] for row in rows if row.get("peak_ram_mb") is not None]
    cpu = [row["cpu_percent"] for row in rows if row.get("cpu_percent") is not None]
    lines.extend([f"Average RAM: {statistics.mean(ram):.1f} MB" if ram else "Average RAM: unavailable",
                  f"Peak RAM (sampled): {max(peak):.1f} MB" if peak else "Peak RAM: unavailable",
                  f"Average CPU (one-core equivalent): {statistics.mean(cpu):.2f}%" if cpu else "CPU: unavailable"])
    tokens = [row["total_tokens"] for row in rows if row.get("total_tokens") is not None]
    lines.append(f"Reported tokens: {sum(tokens)}" if tokens else "Tokens: unavailable")
    lines.append("Resources: app + Python automation workers, excludes controlled apps; RAM sampled every 250ms.")
    lines.extend(phase_table(rows))
    lines.extend(cold_warm_table(rows))
    lines.extend(prompt_accounting(rows))
    lines.extend(reliability(rows))
    lines.extend(uia_block(rows))
    return "\n".join(lines)


def side_metrics(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    counts = Counter(str(row.get("status") or "UNKNOWN") for row in rows)
    durations = [value / 1000 for value in numbers(rows, "duration_ms")]
    return {
        "n": float(len(rows)),
        "success rate %": 100 * counts.get("SUCCESS", 0) / len(rows) if rows else None,
        "false success": float(sum(1 for row in rows if row.get("false_success") is True)),
        "median duration s": median(durations),
        "P95 duration s": p95(durations),
        "median LLM calls": median(numbers(rows, "llm_calls")),
        "median prompt chars": median(numbers(rows, "prompt_chars")),
        "median total tokens": median(numbers(rows, "total_tokens")),
        "median controls collected": median(numbers(rows, "uia_controls_collected")),
    }


def compare(before: list[dict[str, Any]], after: list[dict[str, Any]], scope: str = "end_to_end",
            provider: str = "") -> str:
    def keep(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        selected = [row for row in rows if row.get("scope") == scope]
        return [row for row in selected if row.get("provider") == provider] if provider else selected
    left, right = keep(before), keep(after)
    old, new = side_metrics(left), side_metrics(right)
    lines = [f"Scope: {scope}", f"Provider: {provider or 'ALL (merged; use --provider)'}",
             f"BEFORE n={len(left)}  AFTER n={len(right)}", ""]
    if not left or not right:
        lines.append("WARNING: one side has no measured samples; this table is not a comparison.")
    lines.append(f"{'metric':<28}{'BEFORE':>14}{'AFTER':>14}{'DELTA':>14}")
    for key, old_value in old.items():
        new_value = new[key]
        if key == "n":
            delta = f"{new_value - old_value:+.0f}" if old_value is not None and new_value is not None else "n/a"
        elif old_value and new_value is not None:
            delta = f"{100 * (new_value - old_value) / abs(old_value):+.1f}%"
        else:
            # A zero or absent baseline has no meaningful percentage change.
            delta = "n/a"
        lines.append(f"{key:<28}{shown(old_value):>14}{shown(new_value):>14}{delta:>14}")
    lines.extend(["", "Only measured differences are shown; the direction is not claimed as a win."])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, default=ROOT / "logs" / "tasks.jsonl")
    parser.add_argument("--scope", choices=("end_to_end", "engine_only"), default="end_to_end")
    parser.add_argument("--compare", type=Path, nargs=2, metavar=("BEFORE", "AFTER"),
                        help="compare two jsonl logs instead of summarizing one")
    parser.add_argument("--provider", default="",
                        help="only tasks from this provider (the logs hold several)")
    args = parser.parse_args()
    if args.compare:
        paths = list(args.compare)
        for path in paths:
            if not path.exists():
                print(f"Missing log: {path}; comparison was not generated.")
                return 0
        try:
            print(compare(load(paths[0]), load(paths[1]), args.scope, args.provider))
            return 0
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"Invalid task log ({type(exc).__name__}); comparison was not generated.")
            return 1
    if not args.file.exists():
        print("No task log yet. Run a real task or benchmark first.")
        return 0
    try:
        print(summarize(load(args.file), args.scope, args.provider))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Invalid task log ({type(exc).__name__}); report was not generated.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
