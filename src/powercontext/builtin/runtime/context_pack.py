# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Self-contained canonical Context Pack and prefix-delta wire protocol."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass

_PACK_SCHEMA = "powercontext.context-pack.v1"
_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"


class ContextPackError(ValueError):
    """Malformed or inconsistent context pack data."""


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _items(items: tuple[bytes, ...]) -> tuple[bytes, ...]:
    if not isinstance(items, tuple) or any(not isinstance(item, bytes) for item in items):
        raise ContextPackError("chunks must be a tuple of bytes")  # noqa: TRY003
    try:
        for item in items:
            item.decode("utf-8")
    except UnicodeError as error:
        raise ContextPackError("chunks must contain valid UTF-8") from error  # noqa: TRY003
    return items


def _encoded(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decoded(value: object) -> tuple[bytes, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ContextPackError("encoded chunks must be an array of strings")  # noqa: TRY003
    try:
        items = tuple(base64.b64decode(item, validate=True) for item in value)
    except (ValueError, binascii.Error) as error:
        raise ContextPackError("invalid Base64 chunk") from error  # noqa: TRY003
    if _encoded(items) != value:
        raise ContextPackError("Base64 chunks must use canonical encoding")  # noqa: TRY003
    return _items(items)


def _digest(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ContextPackError("invalid SHA-256 digest")  # noqa: TRY003
    return value


def _wire(data: bytes, expected: set[str], schema: str) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ContextPackError("duplicate JSON field")  # noqa: TRY003
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    except (ValueError, UnicodeError, AttributeError) as error:
        raise ContextPackError("invalid context wire JSON") from error  # noqa: TRY003
    if not isinstance(value, dict) or set(value) != expected or value.get("schema") != schema:
        raise ContextPackError("invalid context wire schema")  # noqa: TRY003
    return value


@dataclass(frozen=True, slots=True)
class ContextPack:
    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> ContextPack:
        _items(items)
        unsigned = {"schema": _PACK_SCHEMA, "items": _encoded(items)}
        digest = "sha256:" + hashlib.sha256(_json(unsigned)).hexdigest()
        count = 0
        while True:
            size = len(_json({**unsigned, "digest": digest, "byte_count": count}))
            if count == size:
                return cls(items=items, digest=digest, byte_count=size)
            count = size

    @classmethod
    def from_content(cls, content: str | None, *, chunk_bytes: int = 512) -> ContextPack:
        if type(chunk_bytes) is not int or chunk_bytes <= 0:
            raise ContextPackError("chunk_bytes must be a positive integer")  # noqa: TRY003
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise ContextPackError("content must be text")  # noqa: TRY003
        try:
            raw = content.encode("utf-8")
        except UnicodeError as error:
            raise ContextPackError("content must be valid UTF-8") from error  # noqa: TRY003
        chunks: list[bytes] = []
        start = 0
        while start < len(raw):
            end = min(start + chunk_bytes, len(raw))
            while end > start:
                try:
                    raw[start:end].decode("utf-8")
                    break
                except UnicodeDecodeError:
                    end -= 1
            if end == start:
                raise ContextPackError("chunk limit cannot fit a UTF-8 character")  # noqa: TRY003
            chunks.append(raw[start:end])
            start = end
        return cls.build(tuple(chunks))

    def to_bytes(self) -> bytes:
        return _json({
            "schema": _PACK_SCHEMA,
            "items": _encoded(self.items),
            "digest": self.digest,
            "byte_count": self.byte_count,
        })

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        payload = _wire(data, {"schema", "items", "digest", "byte_count"}, _PACK_SCHEMA)
        result = cls.build(_decoded(payload["items"]))
        if _digest(payload["digest"]) != result.digest:
            raise ContextPackError("pack digest mismatch")  # noqa: TRY003
        if type(payload["byte_count"]) is not int or payload["byte_count"] != result.byte_count:
            raise ContextPackError("pack byte count mismatch")  # noqa: TRY003
        return result


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        base = ContextPack.from_bytes(base.to_bytes())
        target = ContextPack.from_bytes(target.to_bytes())
        prefix = 0
        for left, right in zip(base.items, target.items, strict=False):
            if left != right:
                break
            prefix += 1
        return cls(base.digest, target.digest, prefix, target.items[prefix:])

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        _digest(self.base_digest)
        _digest(self.target_digest)
        if type(self.prefix_items) is not int or self.prefix_items < 0:
            raise ContextPackError("prefix_items must be a nonnegative integer")  # noqa: TRY003
        _items(self.appended)
        return _json({
            "schema": _DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _encoded(self.appended),
        })

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        payload = _wire(data, {"schema", "base_digest", "target_digest", "prefix_items", "appended"}, _DELTA_SCHEMA)
        prefix = payload["prefix_items"]
        if type(prefix) is not int or prefix < 0:
            raise ContextPackError("prefix_items must be a nonnegative integer")  # noqa: TRY003
        return cls(
            _digest(payload["base_digest"]), _digest(payload["target_digest"]), prefix, _decoded(payload["appended"])
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    base = ContextPack.from_bytes(base.to_bytes())
    delta = ContextPackDelta.from_bytes(delta.to_bytes())
    if delta.base_digest != base.digest or delta.prefix_items > len(base.items):
        raise ContextPackError("delta does not refer to this base pack")  # noqa: TRY003
    result = ContextPack.build(base.items[: delta.prefix_items] + delta.appended)
    if result.digest != delta.target_digest:
        raise ContextPackError("delta target digest mismatch")  # noqa: TRY003
    return result


__all__ = ["ContextPack", "ContextPackDelta", "ContextPackError", "apply_context_pack_delta"]
