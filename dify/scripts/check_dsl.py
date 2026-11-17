"""Static checks for the Dify DSL before it is imported by hand.

Catches the failures that waste the most time during import and debugging:
dangling edges, type mismatches, unreachable nodes, and prompt variables that
reference a node output which does not exist.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

VARIABLE_PATTERN = re.compile(r"\{\{#([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)#\}\}")


def _fail(errors: list[str], message: str) -> None:
    errors.append(message)


def check(path: Path) -> list[str]:
    errors: list[str] = []
    document = yaml.safe_load(path.read_text(encoding="utf-8"))

    for key in ("app", "kind", "version", "workflow"):
        if key not in document:
            _fail(errors, f"missing top-level key: {key}")
    if document.get("kind") != "app":
        _fail(errors, f"kind must be 'app', got {document.get('kind')!r}")

    graph = (document.get("workflow") or {}).get("graph") or {}
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    if not nodes:
        _fail(errors, "workflow.graph.nodes is empty")
        return errors

    by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        node_id = node.get("id")
        if not node_id:
            _fail(errors, "a node has no id")
            continue
        if node_id in by_id:
            _fail(errors, f"duplicate node id: {node_id}")
        by_id[node_id] = node

    node_types = {node_id: (node.get("data") or {}).get("type") for node_id, node in by_id.items()}

    for edge in edges:
        source = edge.get("source")
        target = edge.get("target")
        data = edge.get("data") or {}
        if source not in by_id:
            _fail(errors, f"edge {edge.get('id')} has unknown source {source!r}")
            continue
        if target not in by_id:
            _fail(errors, f"edge {edge.get('id')} has unknown target {target!r}")
            continue
        if data.get("sourceType") != node_types[source]:
            _fail(
                errors,
                f"edge {edge.get('id')} sourceType {data.get('sourceType')!r} "
                f"does not match node type {node_types[source]!r}",
            )
        if data.get("targetType") != node_types[target]:
            _fail(
                errors,
                f"edge {edge.get('id')} targetType {data.get('targetType')!r} "
                f"does not match node type {node_types[target]!r}",
            )

    adjacency: dict[str, set[str]] = {node_id: set() for node_id in by_id}
    for edge in edges:
        if edge.get("source") in by_id and edge.get("target") in by_id:
            adjacency[edge["source"]].add(edge["target"])

    starts = [node_id for node_id, node_type in node_types.items() if node_type == "start"]
    if len(starts) != 1:
        _fail(errors, f"expected exactly one start node, found {len(starts)}")

    reachable: set[str] = set()
    frontier = list(starts)
    while frontier:
        current = frontier.pop()
        if current in reachable:
            continue
        reachable.add(current)
        frontier.extend(adjacency.get(current, ()))
    unreachable = sorted(set(by_id) - reachable)
    if unreachable:
        _fail(errors, f"unreachable nodes: {unreachable}")

    answers = {node_id for node_id, node_type in node_types.items() if node_type == "answer"}
    if not answers:
        _fail(errors, "no answer node")
    terminal = {node_id for node_id in by_id if node_id not in adjacency or not adjacency[node_id]}
    dead_ends = sorted(terminal - answers)
    if dead_ends:
        _fail(errors, f"nodes with no outgoing edge that are not answer nodes: {dead_ends}")

    known_outputs: dict[str, set[str]] = {}
    for node_id, node in by_id.items():
        data = node.get("data") or {}
        outputs: set[str] = set()
        if node_types[node_id] == "code":
            outputs = set((data.get("outputs") or {}).keys())
        elif node_types[node_id] == "if-else":
            outputs = {"result"}
        else:
            outputs = {
                "text",
                "body",
                "status_code",
                "headers",
                "files",
                "query",
                "user_id",
                "conversation_id",
                "dialogue_count",
            }
        known_outputs[node_id] = outputs
    known_outputs.setdefault("sys", set()).update(
        {"query", "user_id", "conversation_id", "dialogue_count", "files", "app_id", "workflow_run_id"}
    )

    def walk(value: Any, where: str) -> None:
        if isinstance(value, str):
            for node_id, variable in VARIABLE_PATTERN.findall(value):
                if node_id == "env":
                    continue
                if node_id not in by_id and node_id != "sys":
                    _fail(errors, f"{where} references unknown node {node_id!r}")
                    continue
                if node_id == "sys":
                    continue
                if variable not in known_outputs[node_id]:
                    allowed = sorted(known_outputs[node_id])
                    _fail(errors, f"{where} references {node_id}.{variable} which is not an output ({allowed})")
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{where}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{where}[{index}]")

    for node_id, node in by_id.items():
        walk(node.get("data") or {}, f"node {node_id}")

    errors.extend(check_code_nodes(document))
    return errors


SAMPLE_INPUTS = {
    "player_id": "demo-player",
    "utterance": "你还记得我吗？",
    "context_body": '{"memory_block": "- （偏好）不吃香菜", "memory_count": 1}',
    "raw": (
        "记得，小林嘛。你上回把杯子碰倒了。\n<<<MEMORY>>>\n"
        '{"remember":[{"kind":"preference","text":"小林不吃香菜"}],"forget":[]}'
    ),
}


def check_code_nodes(document: dict[str, Any]) -> list[str]:
    """Execute every code node with sample inputs and verify its outputs."""
    errors: list[str] = []
    graph = (document.get("workflow") or {}).get("graph") or {}
    for node in graph.get("nodes") or []:
        data = node.get("data") or {}
        if data.get("type") != "code":
            continue
        node_id = node.get("id")
        declared = set((data.get("outputs") or {}).keys())
        namespace: dict[str, Any] = {}
        try:
            exec(compile(data.get("code") or "", f"<{node_id}>", "exec"), namespace)  # noqa: S102
        except SyntaxError as error:
            _fail(errors, f"code node {node_id} does not compile: {error}")
            continue
        main = namespace.get("main")
        if not callable(main):
            _fail(errors, f"code node {node_id} defines no main()")
            continue
        order = [str((variable or {}).get("variable")) for variable in data.get("variables") or []]
        args = [SAMPLE_INPUTS.get(name, "") for name in order]
        try:
            result = main(*args)
        except Exception as error:
            _fail(errors, f"code node {node_id} raised {type(error).__name__}: {error}")
            continue
        if not isinstance(result, dict):
            _fail(errors, f"code node {node_id} did not return a dict")
            continue
        missing = declared - set(result)
        extra = set(result) - declared
        if missing:
            _fail(errors, f"code node {node_id} never returns declared output(s): {sorted(missing)}")
        if extra:
            _fail(errors, f"code node {node_id} returns undeclared output(s): {sorted(extra)}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the Lethe Tavern Dify DSL")
    parser.add_argument(
        "path",
        nargs="?",
        default=str(Path(__file__).resolve().parents[1] / "dsl" / "lethe-tavern.chatflow.yml"),
    )
    args = parser.parse_args()
    path = Path(args.path)
    errors = check(path)
    if errors:
        print(f"{path.name}: {len(errors)} problem(s)")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(f"{path.name}: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
