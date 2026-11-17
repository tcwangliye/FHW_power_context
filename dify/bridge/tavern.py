"""Tavern domain logic: turn a player utterance into memory reads and writes.

The service owns the rule that one player is one PowerContext Scope. Recall is
therefore an inventory read over that Scope, optionally narrowed by a lexical
search for the current utterance. Forgetting maps a natural-language target to
stored entries and retires them, which keeps the history auditable.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from powercontext import MemoryEntry, PowerContextClient, PowerContextError

MAX_REMEMBER_ITEMS = 6
MAX_TEXT_BYTES = 4096
KIND_LABELS = {
    "preference": "偏好",
    "event": "往事",
    "identity": "身份",
    "promise": "约定",
    "note": "印象",
}
FORGET_SIMILARITY_FLOOR = 0.34


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _bigrams(value: str) -> set[str]:
    cleaned = "".join(character for character in _normalize(value) if character.isalnum())
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[index : index + 2] for index in range(len(cleaned) - 1)}


def similarity(left: str, right: str) -> float:
    """Character-bigram containment of ``left`` inside ``right``."""
    left_grams = _bigrams(left)
    if not left_grams:
        return 0.0
    return len(left_grams & _bigrams(right)) / len(left_grams)


def _label(kind: str) -> str:
    return KIND_LABELS.get(kind, KIND_LABELS["note"])


def render_memory_block(entries: Iterable[MemoryEntry], *, max_chars: int) -> str:
    """Render bounded, citation-free lines for the role-play prompt."""
    lines: list[str] = []
    used = 0
    for entry in entries:
        line = f"- （{_label(entry.kind)}）{entry.text.strip()}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


@dataclass(frozen=True)
class TurnContext:
    scope_id: str
    memory_count: int
    memories: tuple[MemoryEntry, ...]
    retrieved: tuple[MemoryEntry, ...]
    memory_block: str

    def public(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "has_memory": self.memory_count > 0,
            "memory_count": self.memory_count,
            "memory_block": self.memory_block,
            "memories": [entry.public() for entry in self.memories],
            "retrieved": [entry.public() for entry in self.retrieved],
        }


class TavernService:
    """Coordinates PowerContext reads and writes for one tavern deployment."""

    def __init__(
        self,
        client: PowerContextClient,
        *,
        memory_limit: int = 24,
        memory_chars: int = 4000,
        scope_prefix: str = "lethe-tavern",
    ) -> None:
        self._client = client
        self._memory_limit = max(1, memory_limit)
        self._memory_chars = max(200, memory_chars)
        self._scope_prefix = scope_prefix

    @property
    def client(self) -> PowerContextClient:
        return self._client

    def scope_for(self, player_id: str) -> str:
        return self._client.ensure_scope(player_id, prefix=self._scope_prefix)

    def _active(self, player_id: str) -> tuple[str, list[MemoryEntry]]:
        scope_id = self.scope_for(player_id)
        return scope_id, self._client.list_entries(scope_id)

    def _recall(self, scope_id: str, utterance: str) -> list[MemoryEntry]:
        query = utterance.strip()
        if not query:
            return []
        try:
            return self._client.search(scope_id, query, limit=5)
        except PowerContextError:
            # Recall is an enhancement for the current turn; the inventory still
            # carries the player's history when the lexical search is unavailable.
            return []

    def build_context(self, player_id: str, utterance: str) -> TurnContext:
        scope_id, inventory = self._active(player_id)
        recent_first = list(reversed(inventory))[: self._memory_limit]
        retrieved = self._recall(scope_id, utterance)
        retrieved_ids = {entry.entry_id for entry in retrieved}
        ordered = [entry for entry in recent_first if entry.entry_id in retrieved_ids]
        ordered += [entry for entry in recent_first if entry.entry_id not in retrieved_ids]
        return TurnContext(
            scope_id=scope_id,
            memory_count=len(inventory),
            memories=tuple(ordered),
            retrieved=tuple(retrieved),
            memory_block=render_memory_block(ordered, max_chars=self._memory_chars),
        )

    def remember(self, player_id: str, items: Iterable[dict[str, Any]]) -> dict[str, Any]:
        scope_id = self.scope_for(player_id)
        saved: list[dict[str, str]] = []
        rejected: list[dict[str, str]] = []
        for item in list(items)[:MAX_REMEMBER_ITEMS]:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
                rejected.append({"text": text[:80], "reason": "text_too_long"})
                continue
            kind = str(item.get("kind") or "note").strip() or "note"
            entry = self._client.remember(
                scope_id,
                kind=kind,
                text=text,
                reason=str(item.get("reason") or "玩家在对话中透露"),
            )
            saved.append(entry.public())
        return {"scope_id": scope_id, "saved": saved, "saved_count": len(saved), "rejected": rejected}

    def _resolve_targets(self, scope_id: str, inventory: list[MemoryEntry], target: str) -> list[MemoryEntry]:
        """Find the stored entries one forget request refers to.

        PowerContext's own search is the primary matcher. The similarity fallback
        only covers targets phrased differently from the stored wording.
        """
        try:
            hits = self._client.search(scope_id, target, limit=5)
        except PowerContextError:
            hits = []
        if hits:
            return hits[:1]
        scored = [(similarity(target, entry.text), entry) for entry in inventory]
        scored = [item for item in scored if item[0] >= FORGET_SIMILARITY_FLOOR]
        if not scored:
            return []
        scored.sort(key=lambda item: item[0], reverse=True)
        return [scored[0][1]]

    def forget(self, player_id: str, targets: Iterable[str]) -> dict[str, Any]:
        scope_id = self.scope_for(player_id)
        retired: list[dict[str, str]] = []
        unmatched: list[str] = []
        for raw_target in list(targets)[:MAX_REMEMBER_ITEMS]:
            target = str(raw_target or "").strip()
            if not target:
                continue
            # Every write advances the Memory revision, which invalidates the
            # citations captured before it. Re-read per target.
            inventory = self._client.list_entries(scope_id)
            matches = self._resolve_targets(scope_id, inventory, target)
            if not matches:
                unmatched.append(target)
                continue
            for entry in matches:
                retired_entry = self._client.retire(
                    scope_id, citation=entry.citation, reason=f"玩家要求遗忘：{target}"[:512]
                )
                retired.append({"target": target, **retired_entry.public()})
        return {
            "scope_id": scope_id,
            "retired": retired,
            "retired_count": len(retired),
            "unmatched": unmatched,
        }

    def inventory(self, player_id: str, *, include_inactive: bool = False) -> dict[str, Any]:
        scope_id = self.scope_for(player_id)
        entries = self._client.list_entries(scope_id, include_inactive=include_inactive)
        return {
            "scope_id": scope_id,
            "count": len(entries),
            "memories": [entry.public() for entry in entries],
        }

    def reset(self, player_id: str) -> dict[str, Any]:
        """Retire every active entry so the demo scenario can start clean.

        Each retire advances the Memory revision, so the next citation has to be
        read from the refreshed head rather than reused from one snapshot.
        """
        scope_id = self.scope_for(player_id)
        retired: list[str] = []
        while True:
            inventory = self._client.list_entries(scope_id)
            if not inventory:
                break
            entry = inventory[0]
            self._client.retire(scope_id, citation=entry.citation, reason="演示重置")
            retired.append(entry.entry_id)
            if len(retired) > 1000:
                raise RuntimeError("reset did not converge")
        return {"scope_id": scope_id, "retired_count": len(retired), "retired": retired}


__all__ = [
    "KIND_LABELS",
    "TavernService",
    "TurnContext",
    "render_memory_block",
    "similarity",
]
