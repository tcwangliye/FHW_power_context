"""Validate the DSL with Dify's own importer.

`check_dsl.py` does static analysis only. This script goes further: it feeds the
document to `graphon.dsl.loads`, the importer Dify 1.19.0 uses, so every node is
built through its real `*NodeData` Pydantic model. A schema mistake that would
fail the first import surfaces here instead.

The LLM node normally needs the `dify-plugin-daemon-slim` executable, which only
exists inside a Dify deployment, so `SlimLLM` is stubbed. The node still goes
through `LLMNodeData.model_validate`, which is the part that matters.

Usage:
    python -m pip install "graphon==0.8.0"
    python scripts/validate_with_dify.py [path/to/app.yml]

Exit codes: 0 accepted, 1 rejected, 2 graphon missing.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import traceback
from pathlib import Path

DEFAULT_DSL = Path(__file__).resolve().parents[1] / "dsl" / "lethe-tavern.chatflow.yml"
EXPECTED_NODE_TYPES = {
    "start_node": "StartNode",
    "input_node": "CodeNode",
    "context_node": "HttpRequestNode",
    "prepare_node": "CodeNode",
    "npc_node": "LLMNode",
    "parse_node": "CodeNode",
    "if_forget_node": "IfElseNode",
    "forget_node": "HttpRequestNode",
    "if_remember_node": "IfElseNode",
    "remember_node": "HttpRequestNode",
    "answer_node": "AnswerNode",
}


def _graphon_available() -> bool:
    """Return whether graphon.dsl can be imported, without importing it."""
    try:
        return importlib.util.find_spec("graphon.dsl") is not None
    except ModuleNotFoundError:
        return False


def _install_stub() -> None:
    import graphon.dsl.node_factory as node_factory

    class _StubSlimLLM:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def __repr__(self) -> str:
            return "<StubSlimLLM>"

    node_factory.SlimLLM = _StubSlimLLM  # type: ignore[attr-defined]


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Dify DSL with Dify's importer")
    parser.add_argument("path", nargs="?", default=str(DEFAULT_DSL))
    args = parser.parse_args()
    path = Path(args.path)

    if not _graphon_available():
        print("graphon is not installed. Run: python -m pip install 'graphon==0.8.0'", file=sys.stderr)
        return 2

    _install_stub()
    import graphon.dsl as dsl_module

    text = path.read_text(encoding="utf-8")
    print(f"document: {path}")
    try:
        engine = dsl_module.loads(text)
    except Exception as error:  # noqa: BLE001 - any rejection is the result
        print(f"\nREJECTED: {type(error).__name__}: {error}", file=sys.stderr)
        traceback.print_exc()
        return 1

    graph = engine.graph
    nodes = graph.nodes or {}
    edges = graph.edges or {}
    print(f"accepted by Dify importer: {len(nodes)} nodes, {len(edges)} edges")

    problems: list[str] = []
    for node_id, expected in EXPECTED_NODE_TYPES.items():
        node = nodes.get(node_id)
        if node is None:
            problems.append(f"missing node {node_id}")
            continue
        actual = type(node).__name__
        if actual != expected:
            problems.append(f"{node_id} built as {actual}, expected {expected}")
        else:
            print(f"  {node_id:<18} {actual}")

    unexpected = set(nodes) - set(EXPECTED_NODE_TYPES)
    if unexpected:
        problems.append(f"unexpected nodes: {sorted(unexpected)}")

    if problems:
        print("\nproblems:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("\nOK: every node passed its Dify schema model")
    return 0


if __name__ == "__main__":
    sys.exit(main())
