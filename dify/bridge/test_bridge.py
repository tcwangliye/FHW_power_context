"""Behaviour tests for the Lethe Tavern bridge.

``InMemoryClient`` reproduces the PowerContext surface the tavern depends on so
the tests stay fast. ``PowerContextIntegrationTest`` reuses the same assertions
against a real Server when ``POWERCONTEXT_URL`` is set.
"""

from __future__ import annotations

import json
import os
import unittest
import urllib.error
import urllib.request
from typing import Any

from powercontext import MemoryEntry, PowerContextClient, PowerContextError
from tavern import TavernService, render_memory_block, similarity


class InMemoryClient:
    """Minimal stand-in for :class:`powercontext.PowerContextClient`.

    It models the Memory revision: every write advances it, and a citation taken
    from an older revision is rejected. That makes the staleness handling in
    :class:`tavern.TavernService` testable.
    """

    def __init__(self) -> None:
        self.scopes: dict[str, str] = {}
        self.entries: dict[str, list[MemoryEntry]] = {}
        self.revisions: dict[str, int] = {}
        self._counter = 0
        self.base_url = "memory://"

    def health(self) -> str:
        return "ready"

    def ensure_scope(self, player_id: str, *, prefix: str = "lethe") -> str:
        key = f"{prefix}:{player_id}"
        self.scopes.setdefault(key, f"scp_{len(self.scopes)}")
        scope_id = self.scopes[key]
        self.entries.setdefault(scope_id, [])
        self.revisions.setdefault(scope_id, 1)
        return scope_id

    def _cite(self, scope_id: str, entry: MemoryEntry) -> MemoryEntry:
        return MemoryEntry(
            entry_id=entry.entry_id,
            kind=entry.kind,
            text=entry.text,
            citation={**entry.citation, "memory_ref": {"revision": self.revisions[scope_id]}},
        )

    def list_entries(self, scope_id: str, *, include_inactive: bool = False) -> list[MemoryEntry]:
        return [self._cite(scope_id, entry) for entry in self.entries.get(scope_id, [])]

    def search(self, scope_id: str, query: str, *, limit: int = 5) -> list[MemoryEntry]:
        scored = [
            (similarity(query, entry.text), entry)
            for entry in self.entries.get(scope_id, [])
            if similarity(query, entry.text) > 0.3
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [self._cite(scope_id, entry) for _score, entry in scored[:limit]]

    def remember(self, scope_id: str, *, kind: str, text: str, reason: str | None = None) -> MemoryEntry:
        self._counter += 1
        self.revisions[scope_id] += 1
        entry = MemoryEntry(
            entry_id=f"ent_{self._counter}",
            kind=kind,
            text=text,
            citation={"entry_id": f"ent_{self._counter}", "entry_version_id": f"ver_{self._counter}"},
        )
        self.entries[scope_id].append(entry)
        return self._cite(scope_id, entry)

    def retire(self, scope_id: str, *, citation: dict[str, Any], reason: str | None = None) -> MemoryEntry:
        if citation.get("memory_ref", {}).get("revision") != self.revisions[scope_id]:
            raise PowerContextError(409, "conflict", "The Memory Revision is stale.")
        entry_id = citation["entry_id"]
        remaining = []
        retired = None
        for entry in self.entries[scope_id]:
            if entry.entry_id == entry_id:
                retired = entry
            else:
                remaining.append(entry)
        if retired is None:
            raise AssertionError(f"unknown entry {entry_id}")
        self.entries[scope_id] = remaining
        self.revisions[scope_id] += 1
        return self._cite(scope_id, retired)


class TavernBehaviorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = InMemoryClient()
        self.service = TavernService(self.client)

    def test_memory_survives_a_new_session(self) -> None:
        self.service.remember(
            "lin",
            [
                {"kind": "preference", "text": "小林不吃香菜"},
                {"kind": "preference", "text": "小林喜欢温过的酒"},
            ],
        )

        # A later turn is a fresh request, exactly like a new Dify conversation.
        context = self.service.build_context("lin", "你还记得我吗？")

        self.assertTrue(context.memory_count)
        self.assertIn("小林不吃香菜", context.memory_block)
        self.assertIn("小林喜欢温过的酒", context.memory_block)

    def test_players_do_not_share_memory(self) -> None:
        self.service.remember("lin", [{"kind": "event", "text": "小林打翻了酒杯"}])
        self.service.remember("zhou", [{"kind": "event", "text": "周姑娘唱了一首歌"}])

        lin_context = self.service.build_context("lin", "")
        zhou_context = self.service.build_context("zhou", "")

        self.assertIn("小林打翻了酒杯", lin_context.memory_block)
        self.assertNotIn("周姑娘唱了一首歌", lin_context.memory_block)
        self.assertIn("周姑娘唱了一首歌", zhou_context.memory_block)

    def test_forgotten_memory_stops_surfacing(self) -> None:
        self.service.remember(
            "lin",
            [
                {"kind": "event", "text": "小林上次把酒杯打翻了还笑着说手滑"},
                {"kind": "preference", "text": "小林不吃香菜"},
            ],
        )

        result = self.service.forget("lin", ["打翻酒杯"])
        self.assertEqual(result["retired_count"], 1)

        context = self.service.build_context("lin", "我上次干过什么坏事吗？")
        self.assertNotIn("打翻酒杯", context.memory_block)
        self.assertIn("小林不吃香菜", context.memory_block)

    def test_forget_matches_a_paraphrased_request(self) -> None:
        self.service.remember("lin", [{"kind": "event", "text": "小林把酒泼在掌柜的新袍子上"}])
        self.client.search = lambda *args, **kwargs: []  # force the similarity fallback

        result = self.service.forget("lin", ["把酒泼到袍子上的事"])

        self.assertEqual(result["retired_count"], 1)
        self.assertEqual(result["unmatched"], [])

    def test_unmatched_forget_is_reported_not_silently_dropped(self) -> None:
        self.service.remember("lin", [{"kind": "preference", "text": "小林不吃香菜"}])

        result = self.service.forget("lin", ["我欠你三块灵石"])

        self.assertEqual(result["retired_count"], 0)
        self.assertEqual(result["unmatched"], ["我欠你三块灵石"])

    def test_unknown_player_gets_an_empty_but_valid_context(self) -> None:
        context = self.service.build_context("nobody", "我们见过吗？")
        self.assertEqual(context.memory_count, 0)
        self.assertFalse(context.public()["has_memory"])
        self.assertEqual(context.memory_block, "")

    def test_reset_clears_every_entry_despite_advancing_revisions(self) -> None:
        self.service.remember(
            "lin",
            [
                {"kind": "note", "text": "随手记一笔"},
                {"kind": "note", "text": "再记一笔"},
                {"kind": "note", "text": "还有一笔"},
            ],
        )
        result = self.service.reset("lin")
        self.assertEqual(result["retired_count"], 3)
        self.assertEqual(self.service.build_context("lin", "").memory_count, 0)

    def test_forget_two_targets_in_one_request(self) -> None:
        self.service.remember(
            "lin",
            [
                {"kind": "event", "text": "小林把酒杯打翻了"},
                {"kind": "event", "text": "小林赊了三壶酒"},
            ],
        )
        result = self.service.forget("lin", ["打翻酒杯", "三壶酒"])
        self.assertEqual(result["retired_count"], 2)
        self.assertEqual(result["unmatched"], [])


class RenderingTest(unittest.TestCase):
    def test_block_is_bounded_and_labelled(self) -> None:
        entries = [
            MemoryEntry("a", "preference", "不吃香菜", {}),
            MemoryEntry("b", "event", "打翻酒杯", {}),
        ]
        block = render_memory_block(entries, max_chars=1000)
        self.assertIn("（偏好）不吃香菜", block)
        self.assertIn("（往事）打翻酒杯", block)

        for budget in (5, 10, 20, 40):
            rendered = render_memory_block(entries, max_chars=budget)
            self.assertLessEqual(len(rendered), budget)

    def test_similarity_handles_short_and_empty_input(self) -> None:
        self.assertEqual(similarity("", "任何内容"), 0.0)
        self.assertGreater(similarity("打翻酒杯", "小林把酒杯打翻了"), 0.3)


class BridgeHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        import threading
        from http.server import ThreadingHTTPServer

        from app import BridgeHandler

        BridgeHandler.service = TavernService(InMemoryClient())
        BridgeHandler.bridge_token = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), BridgeHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _post(self, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_full_turn_round_trip(self) -> None:
        status, health = self._get_health()
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "ok")

        status, saved = self._post(
            "/turn/remember", {"player_id": "lin", "memories": [{"kind": "preference", "text": "不吃香菜"}]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved["saved_count"], 1)

        status, context = self._post("/turn/context", {"player_id": "lin", "utterance": "你还记得我吗"})
        self.assertEqual(status, 200)
        self.assertIn("不吃香菜", context["memory_block"])

        status, forgotten = self._post("/turn/forget", {"player_id": "lin", "targets": ["不吃香菜"]})
        self.assertEqual(status, 200)
        self.assertEqual(forgotten["retired_count"], 1)

        status, context = self._post("/turn/context", {"player_id": "lin", "utterance": ""})
        self.assertEqual(context["memory_count"], 0)

    def test_missing_player_id_is_a_client_error(self) -> None:
        status, payload = self._post("/turn/context", {"utterance": "hi"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_unknown_route_is_reported(self) -> None:
        status, payload = self._post("/nope", {})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def _get_health(self) -> tuple[int, dict[str, Any]]:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=10) as response:
            return response.status, json.load(response)


@unittest.skipUnless(os.environ.get("POWERCONTEXT_URL"), "POWERCONTEXT_URL not set")
class PowerContextIntegrationTest(unittest.TestCase):
    """Run the same recall/forget contract against a real Server."""

    def test_remember_then_forget_against_real_server(self) -> None:
        client = PowerContextClient(os.environ["POWERCONTEXT_URL"])
        service = TavernService(client, scope_prefix=f"lethe-test-{os.getpid()}")
        player = "integration-player"

        service.reset(player)
        service.remember(player, [{"kind": "event", "text": "小林把酒杯打翻了还笑着说手滑"}])
        block = service.build_context(player, "你还记得我吗").memory_block
        self.assertIn("打翻", block)
        self.assertIn("酒杯", block)

        result = service.forget(player, ["打翻酒杯"])
        self.assertEqual(result["retired_count"], 1)
        self.assertNotIn("酒杯", service.build_context(player, "我干过什么坏事").memory_block)
        service.reset(player)


if __name__ == "__main__":
    unittest.main(verbosity=2)
