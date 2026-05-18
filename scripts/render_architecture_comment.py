#!/usr/bin/env python3
"""Render a rich PR comment for architecture analysis results."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--compare")
    parser.add_argument("--baseline")
    parser.add_argument("--run-url")
    parser.add_argument("--tooling-repository")
    return parser.parse_args()


def load_json(path: str | None) -> dict | None:
    if not path:
        return None
    file_path = Path(path)
    if not file_path.exists():
        return None
    return json.loads(file_path.read_text(encoding="utf-8"))


def score_label(score: int) -> str:
    if score >= 85:
        return "Excellent"
    if score >= 70:
        return "Good"
    if score >= 55:
        return "Fair"
    return "Needs attention"


def trend_label(compare: dict | None) -> tuple[str, str]:
    if not compare:
        return "🟡", "No baseline yet"
    delta = compare["deltas"].get("architecture_score", 0)
    if delta > 0:
        return "🟢", "Improving"
    if delta < 0:
        return "🔴", "Regressing"
    return "⚪", "Stable"


def metric_delta_icon(delta: int | float) -> str:
    if delta > 0:
        return "🟢"
    if delta < 0:
        return "🔴"
    return "⚪"


def format_delta(delta: int | float) -> str:
    if isinstance(delta, float):
        return f"{delta:+.1f}"
    return f"{delta:+d}"


def summarize_category_edges(current: dict) -> dict[tuple[str, str], int]:
    modules = {module["name"]: module for module in current["modules"]}
    category_edges: dict[tuple[str, str], int] = defaultdict(int)
    for module in current["modules"]:
        source_category = module["category"]
        for dependency_name in module["internal_imports"]:
            dependency = modules.get(dependency_name)
            if not dependency:
                continue
            target_category = dependency["category"]
            category_edges[(source_category, target_category)] += 1
    return category_edges


def render_mermaid(current: dict) -> str:
    categories = Counter(module["category"] for module in current["modules"])
    edges = summarize_category_edges(current)
    lines = ["```mermaid", "flowchart LR"]
    for category in sorted(categories):
        label = f"{category.title()} ({categories[category]})"
        node = category.replace("-", "_")
        lines.append(f'    {node}["{label}"]')

    if edges:
        for (source, target), count in sorted(edges.items()):
            source_node = source.replace("-", "_")
            target_node = target.replace("-", "_")
            lines.append(f"    {source_node} -->|{count}| {target_node}")
    else:
        ordered = [category.replace("-", "_") for category in sorted(categories)]
        for left, right in zip(ordered, ordered[1:]):
            lines.append(f"    {left} --> {right}")

    lines.append("```")
    return "\n".join(lines)


def render_metric_evolution(
    current: dict, baseline: dict | None, compare: dict | None
) -> list[str]:
    if not baseline or not compare:
        return [
            "### 📈 Metric Evolution",
            "",
            "Baseline snapshot not found yet. Merge one successful run into `main` to enable before/after comparison on the next PR.",
            "",
        ]

    current_summary = current["summary"]
    baseline_summary = baseline["summary"]
    metrics = [
        ("📦 Modules", baseline_summary["module_count"], current_summary["module_count"]),
        (
            "🔗 Internal edges",
            baseline_summary["internal_edge_count"],
            current_summary["internal_edge_count"],
        ),
        (
            "🚪 Entrypoints",
            baseline_summary["entrypoint_count"],
            current_summary["entrypoint_count"],
        ),
        ("🔁 Circular groups", baseline_summary["cycle_count"], current_summary["cycle_count"]),
        ("📏 Avg LOC", baseline_summary["avg_loc"], current_summary["avg_loc"]),
        (
            "🎯 Architecture score",
            baseline_summary["architecture_score"],
            current_summary["architecture_score"],
        ),
    ]

    lines = [
        "### 📈 Metric Evolution",
        "",
        f"Baseline snapshot: `{baseline.get('generated_at', 'unknown')}`",
        "",
        "| Metric | Baseline | Current | Change |",
        "| --- | ---: | ---: | --- |",
    ]

    for label, before, after in metrics:
        delta = after - before
        icon = metric_delta_icon(delta)
        lines.append(f"| {label} | {before} | {after} | {icon} {format_delta(delta)} |")

    lines.append("")
    return lines


def render_current_architecture(current: dict) -> list[str]:
    summary = current["summary"]
    highest_fan_in = (
        current["hotspots"]["highest_fan_in"][0] if current["hotspots"]["highest_fan_in"] else None
    )
    highest_fan_out = (
        current["hotspots"]["highest_fan_out"][0]
        if current["hotspots"]["highest_fan_out"]
        else None
    )

    lines = [
        "### 🏛️ Current Architecture",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| 📦 Modules | {summary['module_count']} |",
        f"| 🔗 Internal edges | {summary['internal_edge_count']} |",
        f"| 🚪 Entrypoints | {summary['entrypoint_count']} |",
        f"| 🔁 Circular groups | {summary['cycle_count']} |",
        f"| 📏 Avg LOC/module | {summary['avg_loc']} |",
        f"| 🎯 Architecture score | {summary['architecture_score']} ({score_label(summary['architecture_score'])}) |",
    ]
    if highest_fan_in:
        lines.append(
            f"| 🧲 Highest fan-in | `{highest_fan_in['module']}` ({highest_fan_in['fan_in']}) |"
        )
    if highest_fan_out:
        lines.append(
            f"| 🌐 Highest fan-out | `{highest_fan_out['module']}` ({highest_fan_out['fan_out']}) |"
        )
    lines.append("")
    return lines


def render_smells(current: dict) -> list[str]:
    lines = ["### 🚨 Architectural Smells", ""]
    smells = [risk for risk in current["risks"] if risk["severity"] in {"high", "medium"}]
    if not smells:
        lines.extend(["✅ No architectural smells detected.", ""])
        return lines

    for smell in smells:
        severity = smell["severity"].upper()
        evidence = "; ".join(smell.get("evidence", [])[:3])
        if evidence:
            lines.append(
                f"- {severity}: **{smell['title']}** — {smell['details']} Evidence: {evidence}"
            )
        else:
            lines.append(f"- {severity}: **{smell['title']}** — {smell['details']}")
    lines.append("")
    return lines


def render_insights(current: dict, compare: dict | None, run_url: str | None) -> list[str]:
    summary = current["summary"]
    trend_icon, trend = trend_label(compare)
    compare_status = compare["status"] if compare else "no-baseline"
    stability = "High" if summary["cycle_count"] == 0 else "Needs review"
    smell_count = len([risk for risk in current["risks"] if risk["severity"] in {"high", "medium"}])

    lines = [
        "### 💡 CI/CD Insights",
        "",
        f"- Quality Score: **{summary['architecture_score']}/100** ({score_label(summary['architecture_score'])})",
        f"- Trend: {trend_icon} {trend}",
        f"- Architecture Stability: {'🟢' if summary['cycle_count'] == 0 else '🔴'} {stability}",
        f"- Smells: {'✅ Clean' if smell_count == 0 else f'⚠️ {smell_count} risk(s) flagged'}",
        f"- Baseline Gate: `{compare_status}`",
    ]
    if run_url:
        lines.append(f"- 📄 [View workflow run and artifacts]({run_url})")
    lines.extend(
        [
            "",
            "This comment is auto-generated by the architecture-analysis workflow and updates on every push to this PR.",
            "",
        ]
    )
    return lines


def render_comment(
    current: dict,
    baseline: dict | None,
    compare: dict | None,
    run_url: str | None,
    tooling_repository: str | None,
) -> str:
    lines = [
        current.get("comment_marker", "<!-- architecture-analysis -->"),
        "## 🤖 Architecture Analysis Summary",
        "",
    ]
    if tooling_repository:
        lines.append(f"Powered by [{tooling_repository}](https://github.com/{tooling_repository})")
        lines.append("")

    lines.extend(render_metric_evolution(current, baseline, compare))
    lines.extend(render_current_architecture(current))
    lines.append("### 🕸️ High-Level Design")
    lines.append("")
    lines.append(render_mermaid(current))
    lines.append("")
    lines.extend(render_smells(current))
    lines.extend(render_insights(current, compare, run_url))

    lines.extend(
        [
            "<details><summary>Raw analysis snapshot</summary>",
            "",
            "```json",
            json.dumps(current["summary"], indent=2),
            "```",
            "",
            "</details>",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    current = load_json(args.current)
    if current is None:
        raise SystemExit("Current analysis JSON is required.")
    baseline = load_json(args.baseline)
    compare = load_json(args.compare)
    comment = render_comment(current, baseline, compare, args.run_url, args.tooling_repository)
    Path(args.output).write_text(comment, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
