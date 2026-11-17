"""Scripted OpenAI-compatible chat endpoint for offline verification.

This is NOT a model. It is a deterministic stand-in used to exercise the Dify
orchestration when no model credentials are available. It speaks enough of the
OpenAI chat-completions protocol for Dify's OpenAI-API-compatible provider and
emits the same ``<<<MEMORY>>>`` directive the tavern prompt asks a model for, so
the graph's parse/remember/forget branches run for real.

Swap it for a real provider by pointing the Dify model at any OpenAI-compatible
endpoint; nothing else in the flow changes.
"""

from __future__ import annotations

import json
import os
import re
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL_NAME = os.environ.get("STUB_MODEL_NAME", "lethe-npc")
MARKER = "<<<MEMORY>>>"
FORGET_HINTS = ("忘", "别记", "抹掉", "不要再提")
SPILL_WORDS = ("打翻", "泼", "摔", "酒杯", "杯子")


def _last_user_message(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return ""


def _known_memories(system_prompt: str) -> list[str]:
    """Read the memory block the prompt embedded, for echo-style recall."""
    block = system_prompt.split("你记得的事：", 1)[-1]
    return [line.strip("- ").strip() for line in block.splitlines() if line.strip().startswith("-")]


def _reply(user_text: str, system_prompt: str) -> str:
    wants_forget = any(hint in user_text for hint in FORGET_HINTS)

    if wants_forget:
        targets: list[str] = []
        known = _known_memories(system_prompt)
        if any(any(word in line for word in SPILL_WORDS) for line in known):
            targets.append("打翻酒杯")
        if not targets and any(word in user_text for word in SPILL_WORDS):
            targets.append("打翻酒杯")
        line = "行，那就当我没听过。忘川的水一冲，什么都不剩。"
        payload = {"remember": [], "forget": targets[:2] or ["打翻酒杯"]}
        return f"{line}\n{MARKER}\n{json.dumps(payload, ensure_ascii=False)}"

    remember: list[dict[str, str]] = []
    if "香菜" in user_text:
        remember.append({"kind": "preference", "text": "小林不吃香菜"})
    if "温" in user_text:
        remember.append({"kind": "preference", "text": "小林喜欢温过的酒"})
    match = re.search(r"我(?:叫|是)\s*([\u4e00-\u9fa5A-Za-z]{1,8})", user_text)
    if match:
        remember.append({"kind": "identity", "text": f"常来的客人，自称{match.group(1)}"})
    if any(word in user_text for word in SPILL_WORDS):
        remember.append({"kind": "event", "text": "小林把酒杯打翻了还笑着说手滑"})

    known = _known_memories(system_prompt)
    if known:
        opening = f"又来了。我还记着：{known[0]}。"
    else:
        opening = "面生。第一次来吧，坐下说。"
    closing = "你说的我记下了，下次不用再讲一遍。" if remember else "有什么想说的，就说。"
    payload = {"remember": remember[:3], "forget": []}
    return f"{opening}{closing}\n{MARKER}\n{json.dumps(payload, ensure_ascii=False)}"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002 - base signature
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path.rstrip("/").endswith("/models"):
            self._json(HTTPStatus.OK, {"object": "list", "data": [{"id": MODEL_NAME, "object": "model"}]})
            return
        self._json(HTTPStatus.OK, {"status": "ok"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = {}

        messages = payload.get("messages") or []
        system_prompt = next((str(m.get("content") or "") for m in messages if m.get("role") == "system"), "")
        content = _reply(_last_user_message(messages), system_prompt)
        model = payload.get("model") or MODEL_NAME
        completion_id = f"chatcmpl-stub-{int(time.time() * 1000)}"

        # Dify opens chat models in streaming mode, so honour `stream`.
        if payload.get("stream"):
            self._stream(completion_id, model, content)
            return

        self._json(
            HTTPStatus.OK,
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def _stream(self, completion_id: str, model: str, content: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def frame(data: dict[str, Any] | str) -> None:
            body = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
            chunk = f"data: {body}\n\n".encode()
            self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n")
            self.wfile.flush()

        created = int(time.time())
        # Emit in small pieces so the client sees a normal token stream.
        step = 6
        for index in range(0, len(content), step):
            frame(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {"index": 0, "delta": {"content": content[index : index + step]}, "finish_reason": None}
                    ],
                }
            )
        frame(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )
        frame("[DONE]")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def main() -> None:
    port = int(os.environ.get("STUB_PORT", "8080"))
    print(f"scripted model listening on :{port} as {MODEL_NAME}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
