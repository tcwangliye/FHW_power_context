"""Minimal standard-library client for the PowerContext HTTP API.

Only the endpoints the tavern needs are wrapped: scope resolution, memory
listing, memory search, explicit writes and retirement.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class PowerContextError(RuntimeError):
    """Raised when PowerContext returns a non-success response."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"PowerContext {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class MemoryEntry:
    """One durable Memory entry with the citation needed to revise or retire it."""

    entry_id: str
    kind: str
    text: str
    citation: dict[str, Any]
    state: str = "active"

    def public(self) -> dict[str, str]:
        return {"entry_id": self.entry_id, "kind": self.kind, "text": self.text, "state": self.state}


def _scope_key(prefix: str, player_id: str) -> str:
    digest = hashlib.sha256(player_id.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}:{digest}"


def _parse_entry(raw: dict[str, Any]) -> MemoryEntry:
    return MemoryEntry(
        entry_id=str(raw["citation"]["entry_id"]),
        kind=str(raw.get("kind") or "note"),
        text=str(raw.get("text") or ""),
        citation=raw["citation"],
        state=str(raw.get("state") or "active"),
    )


class PowerContextClient:
    """Thin client over one PowerContext Server."""

    def __init__(self, base_url: str, token: str | None = None, timeout: float = 15.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        return self._base_url

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(f"{self._base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            code = "http_error"
            message = detail
            try:
                parsed = json.loads(detail)
                code = str(parsed.get("error", {}).get("code") or code)
                message = str(parsed.get("error", {}).get("message") or detail)
            except (ValueError, AttributeError):
                pass
            raise PowerContextError(error.code, code, message) from error
        except urllib.error.URLError as error:
            raise PowerContextError(503, "unreachable", str(error.reason)) from error
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def health(self) -> str:
        """Return the Server readiness status string."""
        try:
            body = self._request("GET", "/health/ready")
        except PowerContextError as error:
            return f"unavailable:{error.status}"
        return str(body.get("status") or "unknown")

    def ensure_scope(self, player_id: str, *, prefix: str = "lethe-tavern") -> str:
        """Return the durable Scope for a player, creating it on first use.

        Scope creation is idempotent on ``idempotency_key``, so repeated turns
        resolve the same identifier.
        """
        body = self._request(
            "POST",
            "/v1/scopes",
            {
                "title": f"Lethe Tavern player {player_id}"[:256],
                "summary": "Durable memory partition for one Lethe Tavern player.",
                "idempotency_key": _scope_key(prefix, player_id),
            },
        )
        return str(body["scope_id"])

    def list_entries(self, scope_id: str, *, include_inactive: bool = False) -> list[MemoryEntry]:
        body = self._request(
            "POST",
            "/v1/memory/entries/list",
            {"scope_id": scope_id, "include_inactive": include_inactive},
        )
        return [_parse_entry(raw) for raw in body.get("entries", [])]

    def search(self, scope_id: str, query: str, *, limit: int = 5) -> list[MemoryEntry]:
        body = self._request(
            "POST",
            "/v1/memory/search",
            {"scope_id": scope_id, "query": query, "limit": max(1, min(limit, 50))},
        )
        results: list[MemoryEntry] = []
        for hit in body.get("hits", []):
            results.append(
                MemoryEntry(
                    entry_id=str(hit["citation"]["entry_id"]),
                    kind="recalled",
                    text=str(hit.get("text") or ""),
                    citation=hit["citation"],
                    state="active",
                )
            )
        return results

    def remember(self, scope_id: str, *, kind: str, text: str, reason: str | None = None) -> MemoryEntry:
        payload: dict[str, Any] = {"scope_id": scope_id, "kind": kind[:128], "text": text}
        if reason:
            payload["reason"] = reason[:512]
        body = self._request("POST", "/v1/memory/remember", payload)
        return _parse_entry(body["entry"])

    def retire(self, scope_id: str, *, citation: dict[str, Any], reason: str | None = None) -> MemoryEntry:
        payload: dict[str, Any] = {"scope_id": scope_id, "citation": citation}
        if reason:
            payload["reason"] = reason[:512]
        body = self._request("POST", "/v1/memory/entries/retire", payload)
        return _parse_entry(body["entry"])


__all__ = ["MemoryEntry", "PowerContextClient", "PowerContextError"]
