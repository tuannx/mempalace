#!/usr/bin/env python3
"""Static architecture analysis for Python repositories."""

from __future__ import annotations

import argparse
import ast
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


EXCLUDED_DIRS = {
    ".git",
    ".github",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "site-packages",
    "venv",
}

ANALYSIS_MARKER = "<!-- architecture-analysis -->"


@dataclass
class ModuleRecord:
    name: str
    path: str
    loc: int
    internal_imports: list[str]
    external_imports: list[str]
    category: str
    fan_in: int = 0
    fan_out: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-path", required=True)
    parser.add_argument("--primary-algorithm", default="pkg")
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", required=True)
    return parser.parse_args()


def discover_python_files(source_path: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(source_path.rglob("*.py")):
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        if any(part == "tests" for part in path.parts):
            continue
        files.append(path)
    return files


def detect_package_roots(source_path: Path, python_files: Iterable[Path], algorithm: str) -> list[Path]:
    if algorithm != "pkg":
        return [source_path]

    roots = sorted(
        {
            path.parent
            for path in python_files
            if path.name == "__init__.py" and path.parent != source_path
        }
    )
    top_level_roots = []
    for root in roots:
        if not any(parent in roots for parent in root.parents if parent != root):
            top_level_roots.append(root)
    return top_level_roots or [source_path]


def module_name_for(path: Path, source_path: Path, package_roots: list[Path]) -> str:
    for root in package_roots:
        if root in path.parents or path == root:
            relative = path.relative_to(root.parent).with_suffix("")
            parts = list(relative.parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            return ".".join(parts)
    relative = path.relative_to(source_path).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def relative_path(path: Path, source_path: Path) -> str:
    return path.relative_to(source_path).as_posix()


def classify_module(module_name: str) -> str:
    leaf = module_name.rsplit(".", 1)[-1]
    if leaf in {"cli", "__main__", "mcp_server"}:
        return "entrypoint"
    if leaf in {"config", "palace_graph", "knowledge_graph", "searcher"}:
        return "infrastructure"
    if leaf in {"miner", "convo_miner", "onboarding", "layers"}:
        return "orchestration"
    return "core"


def resolve_relative_import(module_name: str, imported: str | None, level: int) -> str | None:
    parts = module_name.split(".")
    base = parts[:-1]
    if level > len(parts):
        return imported
    anchor = parts[:-level]
    if imported:
        return ".".join([*anchor, imported])
    return ".".join(anchor)


def extract_imports(module_name: str, source: str) -> tuple[set[str], set[str]]:
    tree = ast.parse(source)
    internal_candidates: set[str] = set()
    external_imports: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                external_imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                resolved = resolve_relative_import(module_name, node.module, node.level)
                if resolved:
                    internal_candidates.add(resolved)
                continue
            if node.module:
                external_imports.add(node.module)

    return internal_candidates, external_imports


def normalize_internal_import(import_name: str, module_index: set[str]) -> str | None:
    current = import_name
    while current:
        if current in module_index:
            return current
        if "." not in current:
            return None
        current = current.rsplit(".", 1)[0]
    return None


def tarjan(graph: dict[str, list[str]]) -> list[list[str]]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[list[str]] = []

    def strongconnect(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for neighbor in graph[node]:
            if neighbor not in indices:
                strongconnect(neighbor)
                lowlinks[node] = min(lowlinks[node], lowlinks[neighbor])
            elif neighbor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[neighbor])

        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1:
                components.append(sorted(component))

    for node in graph:
        if node not in indices:
            strongconnect(node)

    return sorted(components)


def score_architecture(cycle_count: int, max_fan_in: int, max_fan_out: int, shared_deps: int) -> int:
    score = 100
    score -= cycle_count * 20
    if max_fan_in > 4:
        score -= (max_fan_in - 4) * 4
    if max_fan_out > 6:
        score -= (max_fan_out - 6) * 3
    if shared_deps > 3:
        score -= (shared_deps - 3) * 2
    return max(0, min(100, score))


def build_risks(records: list[ModuleRecord], cycles: list[list[str]], shared_deps: list[dict[str, object]]) -> list[dict[str, object]]:
    risks: list[dict[str, object]] = []
    if cycles:
        risks.append(
            {
                "severity": "high",
                "title": "Circular dependencies detected",
                "details": "Circular imports make modules harder to change and test in isolation.",
                "evidence": [" -> ".join(cycle) for cycle in cycles],
            }
        )

    if shared_deps:
        top_dep = shared_deps[0]
        if top_dep["module_count"] >= 4:
            risks.append(
                {
                    "severity": "medium",
                    "title": f"Shared dependency spread: {top_dep['dependency']}",
                    "details": "A cross-cutting dependency is imported from many modules, which increases change blast radius.",
                    "evidence": top_dep["modules"],
                }
            )

    hotspots = sorted(records, key=lambda record: (record.fan_in, record.fan_out), reverse=True)
    if hotspots and hotspots[0].fan_in >= 4:
        risks.append(
            {
                "severity": "medium",
                "title": f"High fan-in module: {hotspots[0].name}",
                "details": "Many modules depend on the same module. Changes here require extra review.",
                "evidence": [f"fan_in={hotspots[0].fan_in}", f"fan_out={hotspots[0].fan_out}"],
            }
        )

    if not risks:
        risks.append(
            {
                "severity": "info",
                "title": "No structural regression signals detected",
                "details": "The current dependency graph is acyclic and major hotspots remain bounded.",
                "evidence": [],
            }
        )

    return risks


def summarize(records: list[ModuleRecord], cycles: list[list[str]], package_roots: list[Path], source_path: Path) -> dict[str, object]:
    external_counter: Counter[str] = Counter()
    dependency_index: dict[str, list[str]] = defaultdict(list)
    for record in records:
        for dependency in record.external_imports:
            top_level = dependency.split(".", 1)[0]
            external_counter[top_level] += 1
            dependency_index[top_level].append(record.name)

    shared_deps = [
        {
            "dependency": dependency,
            "module_count": count,
            "modules": sorted(set(dependency_index[dependency])),
        }
        for dependency, count in external_counter.most_common()
        if count >= 2
    ]

    highest_fan_in = [
        {"module": record.name, "fan_in": record.fan_in, "path": record.path}
        for record in sorted(records, key=lambda item: (item.fan_in, item.fan_out, item.name), reverse=True)[:5]
    ]
    highest_fan_out = [
        {"module": record.name, "fan_out": record.fan_out, "path": record.path}
        for record in sorted(records, key=lambda item: (item.fan_out, item.fan_in, item.name), reverse=True)[:5]
    ]

    loc_values = [record.loc for record in records]
    max_fan_in = highest_fan_in[0]["fan_in"] if highest_fan_in else 0
    max_fan_out = highest_fan_out[0]["fan_out"] if highest_fan_out else 0
    score = score_architecture(len(cycles), max_fan_in, max_fan_out, len(shared_deps))

    summary = {
        "module_count": len(records),
        "python_file_count": len(records),
        "package_roots": [root.relative_to(source_path).as_posix() for root in package_roots],
        "internal_edge_count": sum(record.fan_out for record in records),
        "entrypoint_count": sum(1 for record in records if record.category == "entrypoint"),
        "cycle_count": len(cycles),
        "avg_loc": round(sum(loc_values) / len(loc_values), 1) if loc_values else 0,
        "architecture_score": score,
    }

    return {
        "summary": summary,
        "hotspots": {
            "highest_fan_in": highest_fan_in,
            "highest_fan_out": highest_fan_out,
            "shared_external_dependencies": shared_deps[:5],
        },
    }


def generate_markdown(report: dict[str, object]) -> str:
    summary = report["summary"]
    hotspots = report["hotspots"]
    risks = report["risks"]

    lines = [
        "# Architecture Analysis",
        "",
        f"Score: **{summary['architecture_score']}/100**",
        "",
        "## Summary",
        "",
        f"- Modules analyzed: {summary['module_count']}",
        f"- Package roots: {', '.join(summary['package_roots']) or '(repo root)'}",
        f"- Internal dependency edges: {summary['internal_edge_count']}",
        f"- Circular dependency groups: {summary['cycle_count']}",
        f"- Entrypoints: {summary['entrypoint_count']}",
        f"- Average LOC per module: {summary['avg_loc']}",
        "",
        "## Coupling Hotspots",
        "",
    ]

    if hotspots["highest_fan_in"]:
        lines.append("Top fan-in modules:")
        for item in hotspots["highest_fan_in"]:
            lines.append(f"- {item['module']}: fan-in {item['fan_in']}")
        lines.append("")

    if hotspots["highest_fan_out"]:
        lines.append("Top fan-out modules:")
        for item in hotspots["highest_fan_out"]:
            lines.append(f"- {item['module']}: fan-out {item['fan_out']}")
        lines.append("")

    if hotspots["shared_external_dependencies"]:
        lines.append("Shared external dependencies:")
        for item in hotspots["shared_external_dependencies"]:
            lines.append(
                f"- {item['dependency']}: used by {item['module_count']} modules"
            )
        lines.append("")

    lines.extend(["## Risks", ""])
    for risk in risks:
        lines.append(f"- {risk['severity'].upper()}: {risk['title']} — {risk['details']}")
    lines.append("")

    if report["cycles"]:
        lines.extend(["## Circular Dependencies", ""])
        for cycle in report["cycles"]:
            lines.append(f"- {' -> '.join(cycle)}")
        lines.append("")

    lines.extend(["## Modules", ""])
    for module in report["modules"]:
        lines.append(
            f"- {module['name']} ({module['category']}, fan-in {module['fan_in']}, fan-out {module['fan_out']})"
        )

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    args = parse_args()
    source_path = Path(args.source_path).resolve()
    python_files = discover_python_files(source_path)
    package_roots = detect_package_roots(source_path, python_files, args.primary_algorithm)
    if args.primary_algorithm == "pkg" and package_roots != [source_path]:
        python_files = [
            path
            for path in python_files
            if any(root == path.parent or root in path.parents for root in package_roots)
        ]

    module_map: dict[str, Path] = {}
    file_lookup: dict[Path, str] = {}
    selected_files: list[Path] = []
    for path in python_files:
        module_name = module_name_for(path, source_path, package_roots)
        if not module_name:
            continue
        module_map[module_name] = path
        file_lookup[path] = module_name
        selected_files.append(path)

    module_index = set(module_map)
    internal_graph: dict[str, list[str]] = {}
    external_imports_by_module: dict[str, list[str]] = {}

    for path in selected_files:
        module_name = file_lookup[path]
        source = path.read_text(encoding="utf-8")
        internal_candidates, external_imports = extract_imports(module_name, source)
        normalized_internal = {
            normalized
            for candidate in internal_candidates
            if (normalized := normalize_internal_import(candidate, module_index))
        }
        internal_graph[module_name] = sorted(normalized_internal)
        external_imports_by_module[module_name] = sorted(external_imports)

    fan_in: Counter[str] = Counter()
    for dependencies in internal_graph.values():
        for dependency in dependencies:
            fan_in[dependency] += 1

    records = []
    for module_name in sorted(module_map):
        path = module_map[module_name]
        source = path.read_text(encoding="utf-8")
        records.append(
            ModuleRecord(
                name=module_name,
                path=relative_path(path, source_path),
                loc=len(source.splitlines()),
                internal_imports=internal_graph[module_name],
                external_imports=external_imports_by_module[module_name],
                category=classify_module(module_name),
                fan_in=fan_in[module_name],
                fan_out=len(internal_graph[module_name]),
            )
        )

    cycles = tarjan(internal_graph)
    summary_data = summarize(records, cycles, package_roots, source_path)
    risks = build_risks(records, cycles, summary_data["hotspots"]["shared_external_dependencies"])

    report = {
        "repo_name": source_path.name,
        "source_path": str(source_path),
        "primary_algorithm": args.primary_algorithm,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **summary_data,
        "modules": [record.__dict__ for record in records],
        "cycles": cycles,
        "risks": risks,
        "comment_marker": ANALYSIS_MARKER,
    }

    output_json = Path(args.output_json)
    output_md = Path(args.output_md)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    output_md.write_text(generate_markdown(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())