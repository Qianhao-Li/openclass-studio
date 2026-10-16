#!/usr/bin/env python3
"""Read-only RIDOC range-editing inventory and in-memory 3.5 upgrade check."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api"))

from openclass_core.services.ridoc_range_edit import baseline_withdrawn, range_result
from openclass_core.services.ridoc_document_actions import DocumentActionBlocked
from openclass_core.services.ridoc_v3 import read_ridoc_v3, validate_ridoc_v3
from openclass_core.services.ridoc_v35 import prepare_range_archive


def audit_file(path: Path) -> dict:
    result = {"path": str(path.resolve()), "status": "blocked", "blockers": [], "branches": []}
    try:
        archive = read_ridoc_v3(path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Parser exceptions may include raw rejected values; never print them.
        result["blockers"].append({"stage": "read", "reason": "RIDOC 文件或完整性校验失败", "error_type": type(exc).__name__})
        return result
    result.update(package_id=archive.package.id, format=archive.manifest["spec_version"],
                  revision=archive.revision.value,
                  counts={"lessons": len(archive.package.lessons), "nodes": len(archive.nodes),
                          "branches": sum(len(graph.branches) for graph in archive.package.lessons.values()),
                          "messages": sum(node.kind == "M" for node in archive.nodes.values()),
                          "document_actions": sum(node.kind == "C" and not node.initial for node in archive.nodes.values()),
                          "documents": len(archive.documents), "resources": len(archive.assets), "evidence": len(archive.evidence)})
    try:
        upgraded = deepcopy(archive)
        prepare_range_archive(upgraded)
        validate_ridoc_v3(upgraded)
        result["upgrade_dry_run"] = {"status": "passed", "target_format": "3.5"}
    except (ValueError, KeyError, TypeError) as exc:
        result["upgrade_dry_run"] = {"status": "blocked", "target_format": "3.5", "error_type": type(exc).__name__}
        result["blockers"].append({"stage": "upgrade", "reason": "内存格式升级未通过校验"})
    fine, expanded = set(), set()
    fine_occurrences = expanded_occurrences = 0
    for lesson_id, graph in archive.package.lessons.items():
        for name in graph.branches:
            branch = {"lesson_id": lesson_id, "branch": name, "actions": [], "status": "ready"}
            try:
                replay = range_result(archive, lesson_id, name, baseline_withdrawn(archive, lesson_id, name))
                for entry in replay["entries"]:
                    identity = entry["node_id"]
                    reasons = [item["reason"] for item in entry.get("ambiguities", [])]
                    if entry.get("scope_reason"):
                        reasons.append(entry["scope_reason"])
                    if entry.get("checkpoint"):
                        reasons.append("保留独立文档状态边界，依赖范围采用保守校验")
                    enlarged = entry["scope"] == "document" or bool(reasons)
                    (expanded if enlarged else fine).add((lesson_id, identity))
                    fine_occurrences += not enlarged
                    expanded_occurrences += enlarged
                    branch["actions"].append({"node_id": identity, "scope": entry["scope"],
                                               "precision": "expanded" if enlarged else "fine",
                                               "expansion_reasons": reasons,
                                               "blocking_node_ids": entry.get("blocking_node_ids", []),
                                               "blocked_reason": entry.get("blocked_reason")})
            except (ValueError, KeyError, TypeError) as exc:
                reason = str(exc) if isinstance(exc, DocumentActionBlocked) else "该分支范围差分或重放校验未通过"
                branch.update(status="blocked", reason=reason, error_type=type(exc).__name__)
                result["blockers"].append({"stage": "range_replay", "lesson_id": lesson_id, "branch": name,
                                           "reason": reason})
            result["branches"].append(branch)
    result["counts"].update(fine_range_c=len(fine), expanded_range_c=len(expanded),
                            fine_range_occurrences=fine_occurrences, expanded_range_occurrences=expanded_occurrences)
    if not result["blockers"]:
        result["status"] = "ready"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="RIDOC files or directories to inspect recursively")
    parser.add_argument("--output", type=Path, help="Optional JSON report file; course files are never rewritten")
    args = parser.parse_args()
    if args.output and args.output.suffix.lower() != ".json":
        parser.error("--output must name a .json report, never a course file")
    files: set[Path] = set()
    missing = []
    for path in args.inputs:
        if path.is_dir():
            files.update(value.resolve() for value in path.rglob("*.ridoc") if value.is_file())
        elif path.is_file():
            files.add(path.resolve())
        else:
            missing.append({"path": str(path), "status": "blocked", "blockers": [{"stage": "input", "reason": "输入路径不存在"}]})
    if args.output and args.output.resolve() in files:
        parser.error("--output cannot overwrite an input file")
    reports = [audit_file(path) for path in sorted(files)] + missing
    payload = {"dry_run": True, "course_files_written": 0, "target_format": "3.5",
               "ready": sum(item["status"] == "ready" for item in reports),
               "blocked": sum(item["status"] == "blocked" for item in reports), "packages": reports}
    output = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 1 if payload["blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
