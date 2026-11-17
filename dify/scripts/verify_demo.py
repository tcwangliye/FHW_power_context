"""Run the five-step verification scenario and write reproducible evidence.

Two modes:

``--mode dify``   drives the real Dify Chatflow through its service API, which
                  exercises Dify orchestration, the bridge and PowerContext.
``--mode bridge`` drives the bridge directly. Use it to prove the PowerContext
                  integration without a Dify deployment.

The assertions are on memory state, which is deterministic. Keyword detection in
the NPC's free-text answer is recorded as an observation, not an assertion.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RECALL_KEYWORDS = ("小林", "香菜", "温")
FORGET_KEYWORDS = ("打翻", "酒杯", "手滑")
SPILL_MARKERS = ("打翻", "酒杯")


def request_json(method: str, url: str, payload: dict | None = None, token: str | None = None) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {url} -> HTTP {error.code}: {detail[:500]}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"{method} {url} -> {error.reason}") from error


@dataclass
class Player:
    """One player identity, addressed consistently by Dify and the bridge."""

    bridge_url: str
    player_id: str
    dify_url: str | None = None
    dify_key: str | None = None
    conversation_id: str = ""

    def new_conversation(self) -> None:
        self.conversation_id = ""

    def say(self, utterance: str) -> dict[str, Any]:
        raise NotImplementedError

    def memories(self) -> list[dict[str, str]]:
        body = request_json("GET", f"{self.bridge_url}/players/{self.player_id}/memories")
        return body.get("memories", [])

    def reset(self) -> dict[str, Any]:
        return request_json("POST", f"{self.bridge_url}/players/{self.player_id}/reset", {})


@dataclass
class DifyPlayer(Player):
    def say(self, utterance: str) -> dict[str, Any]:
        assert self.dify_url and self.dify_key
        body = request_json(
            "POST",
            f"{self.dify_url}/chat-messages",
            {
                "inputs": {},
                "query": utterance,
                "response_mode": "blocking",
                "conversation_id": self.conversation_id,
                "user": self.player_id,
            },
            token=self.dify_key,
        )
        self.conversation_id = str(body.get("conversation_id") or "")
        return {"answer": body.get("answer", ""), "conversation_id": self.conversation_id}


@dataclass
class BridgePlayer(Player):
    """Bridge-only mode: the NPC line comes from PowerContext recall, not a model."""

    def say(self, utterance: str) -> dict[str, Any]:
        context = request_json(
            "POST",
            f"{self.bridge_url}/turn/context",
            {"player_id": self.player_id, "utterance": utterance},
        )
        return {"answer": context.get("memory_block", ""), "conversation_id": "bridge"}

    def remember(self, items: list[dict[str, str]]) -> dict[str, Any]:
        return request_json(
            "POST",
            f"{self.bridge_url}/turn/remember",
            {"player_id": self.player_id, "memories": items},
        )

    def forget(self, targets: list[str]) -> dict[str, Any]:
        return request_json(
            "POST",
            f"{self.bridge_url}/turn/forget",
            {"player_id": self.player_id, "targets": targets},
        )


@dataclass
class Evidence:
    directory: Path
    steps: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def record(self, name: str, request: dict[str, Any], response: dict[str, Any], **extra: Any) -> None:
        entry = {
            "step": len(self.steps) + 1,
            "name": name,
            "request": request,
            "response": response,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            **extra,
        }
        self.steps.append(entry)
        print(f"[{entry['step']}] {name}")
        answer = str(response.get("answer") or "").strip()
        if answer:
            print(f"    阿忘：{answer}")

    def check(self, condition: bool, message: str) -> None:
        if condition:
            print(f"    PASS  {message}")
        else:
            print(f"    FAIL  {message}")
            self.failures.append(message)

    def flush(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "scenario.json").write_text(
            json.dumps({"steps": self.steps, "failures": self.failures}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _spill_entries(memories: list[dict[str, str]]) -> list[dict[str, str]]:
    return [memory for memory in memories if any(marker in memory.get("text", "") for marker in SPILL_MARKERS)]


def run(player: Player, evidence: Evidence) -> int:
    bridge_seed = isinstance(player, BridgePlayer)

    print("步骤 0：清理演示玩家")
    evidence.record("reset", {"player": player.player_id}, player.reset())

    if bridge_seed:
        # Bridge-only mode has no model, so the scenario supplies the writes that
        # the NPC would normally extract from conversation.
        player.remember(
            [
                {"kind": "identity", "text": "小林，是常来的客人"},
                {"kind": "preference", "text": "小林不吃香菜"},
                {"kind": "preference", "text": "小林喜欢温过的酒"},
            ]
        )

    print("\n步骤 1：初次见面，自报家门（会话 A）")
    player.new_conversation() if isinstance(player, DifyPlayer) else None
    first = player.say("我叫小林，不吃香菜，酒要温的。")
    memories = player.memories()
    evidence.record(
        "step1-introduce",
        {"conversation": "A", "utterance": "我叫小林，不吃香菜，酒要温的。"},
        first,
        memories=memories,
    )
    evidence.check(len(memories) > 0, "对话后至少写入一条记忆")
    evidence.check(any("香菜" in m["text"] for m in memories), "记住了『不吃香菜』")

    print("\n步骤 2：恶作剧（会话 A 继续）")
    second = player.say("我把你的酒杯打翻了，哈哈。")
    if bridge_seed:
        # Bridge-only mode has no model to extract the fact, so it is supplied
        # here. The Dify mode relies on the NPC node emitting it instead.
        player.remember([{"kind": "event", "text": "小林把酒杯打翻了还笑着说手滑"}])
    memories = player.memories()
    evidence.record(
        "step2-mischief",
        {"conversation": "A", "utterance": "我把你的酒杯打翻了，哈哈。"},
        second,
        memories=memories,
    )
    evidence.check(bool(_spill_entries(memories)), "记住了『打翻酒杯』这件事")

    print("\n步骤 3：全新会话 B，验证跨会话记忆")
    if isinstance(player, DifyPlayer):
        player.new_conversation()
    third = player.say("你还记得我吗？我上次干过什么？")
    evidence.record(
        "step3-new-session-recall",
        {"conversation": "B", "utterance": "你还记得我吗？我上次干过什么？"},
        third,
    )
    answer = str(third.get("answer") or "")
    if isinstance(player, DifyPlayer):
        hits = [word for word in RECALL_KEYWORDS + FORGET_KEYWORDS if word in answer]
        print(f"    观察：新会话回答命中关键词 {hits}")
        evidence.check(bool(hits), "新会话里阿忘提到了旧事（观察项，非记忆状态断言）")

    print("\n步骤 4：要求遗忘（会话 B 继续）")
    if isinstance(player, DifyPlayer):
        fourth = player.say("打翻你酒杯那件事，忘了吧。")
        evidence.record(
            "step4-forget",
            {"conversation": "B", "utterance": "打翻你酒杯那件事，忘了吧。"},
            fourth,
        )
    else:
        forgotten = player.forget(["打翻酒杯"])
        evidence.record(
            "step4-forget",
            {"conversation": "B", "targets": ["打翻酒杯"]},
            {"answer": "", **forgotten},
        )
        evidence.check(forgotten.get("retired_count", 0) == 1, "停用了 1 条记忆")
    memories = player.memories()
    evidence.check(not _spill_entries(memories), "『打翻酒杯』已不在活跃记忆中")

    print("\n步骤 5：全新会话 C，确认真的忘了")
    if isinstance(player, DifyPlayer):
        player.new_conversation()
    fifth = player.say("我上次在你店里干过什么坏事吗？")
    evidence.record(
        "step5-new-session-after-forget",
        {"conversation": "C", "utterance": "我上次在你店里干过什么坏事吗？"},
        fifth,
    )
    answer = str(fifth.get("answer") or "")
    if isinstance(player, DifyPlayer):
        leaked = [word for word in FORGET_KEYWORDS if word in answer]
        print(f"    观察：遗忘后回答中的残留关键词 {leaked}")
        evidence.check(not leaked, "遗忘后不再提起那件事（观察项）")
    memories = player.memories()
    evidence.check(any("香菜" in m["text"] for m in memories), "其余记忆仍然保留")
    evidence.record("final-memory-state", {}, {"memories": memories})
    return 1 if evidence.failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the Lethe Tavern scenario")
    parser.add_argument("--mode", choices=("dify", "bridge"), default="dify")
    parser.add_argument("--player", default=os.environ.get("LETHE_PLAYER", "demo-lin"))
    parser.add_argument("--bridge", default=os.environ.get("BRIDGE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--dify-url", default=os.environ.get("DIFY_BASE_URL", "http://localhost/v1"))
    parser.add_argument("--dify-key", default=os.environ.get("DIFY_APP_KEY", ""))
    parser.add_argument("--evidence-dir", default="")
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    directory = Path(args.evidence_dir or Path(__file__).resolve().parents[1] / "evidence" / f"run-{stamp}")
    evidence = Evidence(directory)

    if args.mode == "dify":
        if not args.dify_key:
            print("DIFY_APP_KEY is required for --mode dify; use --mode bridge instead.", file=sys.stderr)
            return 2
        player: Player = DifyPlayer(
            bridge_url=args.bridge, player_id=args.player, dify_url=args.dify_url, dify_key=args.dify_key
        )
    else:
        player = BridgePlayer(bridge_url=args.bridge, player_id=args.player)

    started = time.monotonic()
    try:
        status = run(player, evidence)
    finally:
        evidence.flush()
        print(f"\n证据已写入 {directory}")
        print(f"耗时 {time.monotonic() - started:.1f}s")
    return status


if __name__ == "__main__":
    sys.exit(main())
