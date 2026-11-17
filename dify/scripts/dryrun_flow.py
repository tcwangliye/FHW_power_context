"""Dry-run the Chatflow graph using the DSL's own code nodes.

The only node not executed is the LLM, which needs a deployed Dify plus a model
endpoint. Everything else runs for real against a live bridge and PowerContext:

    input_node -> context_node (HTTP) -> prepare_node -> [scripted NPC text]
    -> parse_node -> if_forget -> forget_node (HTTP) -> if_remember
    -> remember_node (HTTP)

This proves the data flow between nodes, including the request bodies the graph
builds and the branch conditions it evaluates. It is not a substitute for one
real Dify run; it removes the risk that the graph is wired wrong.

Usage:
    python scripts/dryrun_flow.py --player demo-lin

Requires a reachable bridge (see README) and `graphon` is not needed.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

DSL_PATH = Path(__file__).resolve().parents[1] / "dsl" / "lethe-tavern.chatflow.yml"
LOCAL_HOST = "lethe-bridge:8080"


@dataclass
class NodeResult:
    node_id: str
    node_type: str
    outputs: dict[str, Any] = field(default_factory=dict)


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _nodes(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    graph = document["workflow"]["graph"]
    return {node["id"]: node["data"] for node in graph["nodes"]}


def _code_function(data: dict[str, Any], node_id: str):
    namespace: dict[str, Any] = {}
    exec(compile(data["code"], f"<{node_id}>", "exec"), namespace)  # noqa: S102
    return namespace["main"]


def _http_post(url: str, body: str, timeout: float = 30.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {"status_code": response.status, "body": response.read().decode("utf-8")}
    except urllib.error.HTTPError as error:
        return {
            "status_code": error.code,
            "body": error.read().decode("utf-8", "replace"),
        }


def _run_http_node(data: dict[str, Any], body_value: str, bridge_url: str) -> dict[str, Any]:
    url = str(data["url"]).replace(f"http://{LOCAL_HOST}", bridge_url.rstrip("/"))
    if not url.startswith(bridge_url.rstrip("/")):
        raise SystemExit(f"refusing to call external URL during dry run: {url}")
    return _http_post(url, body_value)


def dry_run(
    document: dict[str, Any],
    *,
    player_id: str,
    bridge_url: str,
    scripted_npc: list[str],
    evidence_dir: Path,
) -> int:
    nodes = _nodes(document)
    steps: list[dict[str, Any]] = []
    failures: list[str] = []

    def record(name: str, **payload: Any) -> None:
        entry = {"step": len(steps) + 1, "name": name, "at": datetime.now(UTC).isoformat(timespec="seconds"), **payload}
        steps.append(entry)
        print(f"[{entry['step']}] {name}")
        for line in payload.get("notes", []) or []:
            print(f"    {line}")

    def check(condition: bool, message: str) -> None:
        print(f"    {'PASS' if condition else 'FAIL'}  {message}")
        if not condition:
            failures.append(message)

    bridge_url = bridge_url.rstrip("/")
    urls = {node_id: str(data.get("url")) for node_id, data in nodes.items() if data.get("type") == "http-request"}
    record("preflight", notes=[f"bridge={bridge_url}", f"graph http nodes={sorted(urls)}"])

    # 1. input_node: build the context request body from sys.user_id / sys.query.
    naive_query = "我叫小林，不吃香菜，酒要温的。"
    input_main = _code_function(nodes["input_node"], "input_node")
    input_out = input_main(player_id, naive_query)
    record(
        "input_node",
        request={"player_id": player_id, "utterance": naive_query},
        outputs=input_out,
        notes=[f"context_body={input_out['context_body']}"],
    )
    check("context_body" in input_out, "input_node produced context_body")

    # 2. context_node: real HTTP to the bridge.
    context_out = _run_http_node(nodes["context_node"], input_out["context_body"], bridge_url)
    record("context_node", outputs=context_out, notes=[f"status={context_out['status_code']}"])
    check(context_out["status_code"] == 200, "context_node returned 200")

    # 3. prepare_node: parse the bridge body into a prompt block.
    prepare_main = _code_function(nodes["prepare_node"], "prepare_node")
    prepare_out = prepare_main(context_out["body"])
    record("prepare_node", outputs=prepare_out)
    check(isinstance(prepare_out.get("memory_block"), str), "prepare_node produced memory_block")

    # 4. NPC node is the only one not executed: a model endpoint is required.
    npc_text = scripted_npc[0] if scripted_npc else ""
    record(
        "npc_node",
        outputs={"text": npc_text},
        notes=["scripted: a real run would call the configured LLM here"],
    )

    # 5. parse_node: split the answer from the machine-readable memory directive.
    parse_main = _code_function(nodes["parse_node"], "parse_node")
    parse_out = parse_main(npc_text, player_id)
    record(
        "parse_node",
        outputs={k: v for k, v in parse_out.items() if not k.endswith("_body")},
        notes=[
            f"reply={parse_out['reply']}",
            f"remember_body={parse_out['remember_body']}",
            f"forget_body={parse_out['forget_body']}",
        ],
    )
    check(
        parse_out["remember_count"] >= 1 or parse_out["forget_count"] >= 1,
        "parse_node extracted at least one directive",
    )
    check("<<<MEMORY>>>" not in parse_out["reply"], "reply no longer contains the separator")

    # 6. if-else nodes: evaluate the same comparison the graph declares.
    def eval_if(data: dict[str, Any]) -> bool:
        case = data["cases"][0]
        condition = case["conditions"][0]
        node_id, key = condition["variable_selector"]
        if node_id == "parse_node":
            left = parse_out[key]
        else:
            raise SystemExit(f"unsupported condition source in dry run: {node_id}")
        return left > int(condition["value"])

    forget_branch = eval_if(nodes["if_forget_node"])
    record("if_forget_node", outputs={"result": forget_branch})
    if forget_branch:
        forget_out = _run_http_node(nodes["forget_node"], parse_out["forget_body"], bridge_url)
        record("forget_node", outputs=forget_out)
        check(forget_out["status_code"] == 200, "forget_node returned 200")

    remember_branch = eval_if(nodes["if_remember_node"])
    record("if_remember_node", outputs={"result": remember_branch})
    if remember_branch:
        remember_out = _run_http_node(nodes["remember_node"], parse_out["remember_body"], bridge_url)
        record("remember_node", outputs=remember_out)
        check(remember_out["status_code"] == 200, "remember_node returned 200")
        saved = json.loads(remember_out["body"]).get("saved_count", 0)
        check(saved >= 1, "remember_node persisted at least one memory")

    # 7. answer_node is a template; verify the selector resolves.
    answer_template = str(nodes["answer_node"].get("answer") or "")
    record("answer_node", outputs={"answer": answer_template})
    check("parse_node.reply" in answer_template, "answer_node selects parse_node.reply")

    # 8. Confirm the write/retire actually took effect in PowerContext.
    memories = json.loads(
        urllib.request.urlopen(
            urllib.request.Request(f"{bridge_url}/players/{player_id}/memories?include_inactive=true"),
            timeout=30,
        ).read()
    )["memories"]
    active = [m["text"] for m in memories if m.get("state") == "active"]
    inactive = [m["text"] for m in memories if m.get("state") != "active"]
    record("memory-state", outputs={"active": active, "inactive": inactive})
    for target in json.loads(parse_out["forget_body"]).get("targets", []):
        retired_now = [text for text in inactive if any(word in text for word in target)]
        check(bool(retired_now), f"forget target {target!r} is now inactive")

    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "dryrun.json").write_text(
        json.dumps({"steps": steps, "failures": failures, "scripted_npc": scripted_npc}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nevidence: {evidence_dir / 'dryrun.json'}")
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        return 1
    print(f"OK: {len(steps)} nodes walked, no failures")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run the Chatflow graph against a live bridge")
    parser.add_argument("--player", default="dryrun-lin")
    parser.add_argument("--bridge", default="http://127.0.0.1:8080")
    parser.add_argument("--dsl", default=str(DSL_PATH))
    parser.add_argument("--evidence-dir", default="")
    parser.add_argument("--npc-text-file", default="")
    parser.add_argument(
        "--keep-state",
        action="store_true",
        help="skip the preflight reset, so a scenario can be seeded first",
    )
    parser.add_argument(
        "--npc-text",
        default=(
            "又是你，小林。上回你把杯子打翻，我还没忘。\n<<<MEMORY>>>\n"
            '{"remember":[{"kind":"preference","text":"小林不吃香菜"},'
            '{"kind":"preference","text":"小林要温酒"}],"forget":[]}'
        ),
    )
    args = parser.parse_args()

    npc_text = args.npc_text
    if args.npc_text_file:
        npc_text = Path(args.npc_text_file).read_text(encoding="utf-8")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    directory = Path(args.evidence_dir or Path(__file__).resolve().parents[1] / "evidence" / f"dryrun-{stamp}")

    # Start clean so the run is repeatable, unless the caller seeded a scenario.
    if not args.keep_state:
        urllib.request.urlopen(
            urllib.request.Request(f"{args.bridge.rstrip('/')}/players/{args.player}/reset", data=b"{}", method="POST"),
            timeout=30,
        ).read()

    document = _load(Path(args.dsl))
    return dry_run(
        document,
        player_id=args.player,
        bridge_url=args.bridge,
        scripted_npc=[npc_text],
        evidence_dir=directory,
    )


if __name__ == "__main__":
    sys.exit(main())
