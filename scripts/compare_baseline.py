#!/usr/bin/env python3
"""Compare current architecture analysis against a baseline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", required=True)
    return parser.parse_args()


def load_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_regressions(current: dict, baseline: dict) -> tuple[str, list[str], dict[str, int]]:
    current_summary = current["summary"]
    baseline_summary = baseline["summary"]

    deltas = {
        "architecture_score": current_summary["architecture_score"]
        - baseline_summary["architecture_score"],
        "cycle_count": current_summary["cycle_count"] - baseline_summary["cycle_count"],
        "internal_edge_count": current_summary["internal_edge_count"]
        - baseline_summary["internal_edge_count"],
        "module_count": current_summary["module_count"] - baseline_summary["module_count"],
    }

    regressions: list[str] = []
    if deltas["cycle_count"] > 0:
        regressions.append("New circular dependency groups were introduced.")
    if deltas["architecture_score"] <= -10:
        regressions.append("Architecture score dropped by 10 points or more.")

    current_hotspot = (
        current["hotspots"]["highest_fan_in"][0]["fan_in"]
        if current["hotspots"]["highest_fan_in"]
        else 0
    )
    baseline_hotspot = (
        baseline["hotspots"]["highest_fan_in"][0]["fan_in"]
        if baseline["hotspots"]["highest_fan_in"]
        else 0
    )
    deltas["highest_fan_in"] = current_hotspot - baseline_hotspot
    if deltas["highest_fan_in"] >= 2:
        regressions.append("Highest fan-in module became significantly more central.")

    status = "pass"
    if regressions:
        status = "fail"
    elif any(value != 0 for value in deltas.values()):
        status = "warn"

    return status, regressions, deltas


def build_markdown(
    status: str, regressions: list[str], deltas: dict[str, int], current: dict, baseline: dict
) -> str:
    status_label = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}[status]
    lines = [
        f"## Architecture Baseline Comparison: {status_label}",
        "",
        f"- Score: {baseline['summary']['architecture_score']} -> {current['summary']['architecture_score']} ({deltas['architecture_score']:+d})",
        f"- Circular dependency groups: {baseline['summary']['cycle_count']} -> {current['summary']['cycle_count']} ({deltas['cycle_count']:+d})",
        f"- Internal dependency edges: {baseline['summary']['internal_edge_count']} -> {current['summary']['internal_edge_count']} ({deltas['internal_edge_count']:+d})",
        f"- Modules analyzed: {baseline['summary']['module_count']} -> {current['summary']['module_count']} ({deltas['module_count']:+d})",
        f"- Highest fan-in delta: {deltas['highest_fan_in']:+d}",
        "",
    ]

    if regressions:
        lines.append("### Regressions")
        lines.append("")
        for regression in regressions:
            lines.append(f"- {regression}")
        lines.append("")
    else:
        lines.append("No blocking regressions were detected against the current baseline.")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    args = parse_args()
    current = load_json(args.current)
    baseline = load_json(args.baseline)
    status, regressions, deltas = build_regressions(current, baseline)
    markdown = build_markdown(status, regressions, deltas, current, baseline)

    output = {
        "status": status,
        "regressions": regressions,
        "deltas": deltas,
        "current_score": current["summary"]["architecture_score"],
        "baseline_score": baseline["summary"]["architecture_score"],
    }

    output_json = Path(args.output_json)
    output_md = Path(args.output_md)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    output_md.write_text(markdown, encoding="utf-8")
    return 1 if status == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
