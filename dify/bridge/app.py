"""HTTP bridge between Dify HTTP Request nodes and a PowerContext Server.

Standard library only, so the container needs no third-party package and the
same file runs unchanged on a developer host.

Endpoints
---------
GET  /healthz
POST /turn/context            {"player_id", "utterance"}
POST /turn/remember           {"player_id", "memories": [{"kind", "text", "reason"}]}
POST /turn/forget             {"player_id", "targets": [...]}
GET  /players/{id}/memories   ?include_inactive=true
POST /players/{id}/reset
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from pc_client import PowerContextClient, PowerContextError
from tavern import TavernService

MAX_BODY_BYTES = 256 * 1024
MAX_PLAYER_ID_CHARS = 128
LOGGER = logging.getLogger("lethe.bridge")


class BadRequest(ValueError):
    """Request shape violation that maps to HTTP 400."""


def _require_player(payload: dict[str, Any]) -> str:
    player_id = str(payload.get("player_id") or "").strip()
    if not player_id:
        raise BadRequest("player_id is required")
    if len(player_id) > MAX_PLAYER_ID_CHARS:
        raise BadRequest(f"player_id must be at most {MAX_PLAYER_ID_CHARS} characters")
    return player_id


def _require_text(payload: dict[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = payload.get(key)
    if value is None:
        if allow_empty:
            return ""
        raise BadRequest(f"{key} is required")
    text = str(value)
    if not text.strip() and not allow_empty:
        raise BadRequest(f"{key} must not be blank")
    return text


def _upstream_status(error: PowerContextError) -> int:
    """Preserve the client-error meaning of an upstream conflict."""
    if error.status in (409, 412, 428):
        return HTTPStatus.CONFLICT
    if error.status == 404:
        return HTTPStatus.NOT_FOUND
    return HTTPStatus.BAD_GATEWAY


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "LetheBridge/1.0"
    service: TavernService
    bridge_token: str | None = None

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - base signature
        LOGGER.info("%s - %s", self.address_string(), format % args)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str) -> None:
        self._send(status, {"error": {"code": code, "message": message}})

    def _authorized(self) -> bool:
        if not self.bridge_token:
            return True
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {self.bridge_token}"

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise BadRequest("request body is too large")
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BadRequest(f"request body must be UTF-8 JSON: {error}") from error
        if not isinstance(parsed, dict):
            raise BadRequest("request body must be a JSON object")
        return parsed

    def do_GET(self) -> None:
        started = time.monotonic()
        try:
            path, _, query = self.path.partition("?")
            if path == "/livez":
                self._send(HTTPStatus.OK, {"status": "ok"})
                return
            if path == "/healthz":
                status = self.service.client.health()
                self._send(
                    HTTPStatus.OK if status == "ready" else HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "status": "ok" if status == "ready" else "degraded",
                        "powercontext": status,
                        "powercontext_url": self.service.client.base_url,
                    },
                )
                return
            segments = [urllib.parse.unquote(part) for part in path.strip("/").split("/")]
            if len(segments) == 3 and segments[0] == "players" and segments[2] == "memories":
                if not self._authorized():
                    self._error(HTTPStatus.UNAUTHORIZED, "unauthorized", "bridge token required")
                    return
                include_inactive = "include_inactive=true" in query
                self._send(HTTPStatus.OK, self.service.inventory(segments[1], include_inactive=include_inactive))
                return
            self._error(HTTPStatus.NOT_FOUND, "not_found", f"unknown path {path}")
        except PowerContextError as error:
            self._error(_upstream_status(error), "powercontext_error", error.message)
        except Exception as error:
            LOGGER.exception("unhandled GET failure")
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", str(error))
        finally:
            LOGGER.info("GET %s %.0fms", self.path, (time.monotonic() - started) * 1000)

    def do_POST(self) -> None:
        started = time.monotonic()
        try:
            if not self._authorized():
                self._error(HTTPStatus.UNAUTHORIZED, "unauthorized", "bridge token required")
                return
            path = self.path.partition("?")[0]
            payload = self._read_json()
            self._dispatch_post(path, payload)
        except BadRequest as error:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request", str(error))
        except PowerContextError as error:
            self._error(_upstream_status(error), "powercontext_error", error.message)
        except Exception as error:
            LOGGER.exception("unhandled POST failure")
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", str(error))
        finally:
            LOGGER.info("POST %s %.0fms", self.path, (time.monotonic() - started) * 1000)

    def _dispatch_post(self, path: str, payload: dict[str, Any]) -> None:
        if path == "/turn/context":
            player_id = _require_player(payload)
            utterance = _require_text(payload, "utterance", allow_empty=True)
            context = self.service.build_context(player_id, utterance)
            self._send(HTTPStatus.OK, {"player_id": player_id, **context.public()})
            return

        if path == "/turn/remember":
            player_id = _require_player(payload)
            memories = payload.get("memories")
            if not isinstance(memories, list):
                raise BadRequest("memories must be a list")
            items = [item for item in memories if isinstance(item, dict)]
            self._send(HTTPStatus.OK, {"player_id": player_id, **self.service.remember(player_id, items)})
            return

        if path == "/turn/forget":
            player_id = _require_player(payload)
            targets = payload.get("targets")
            if isinstance(payload.get("target"), str):
                targets = [payload["target"]]
            if not isinstance(targets, list):
                raise BadRequest("targets must be a list of strings")
            self._send(
                HTTPStatus.OK,
                {"player_id": player_id, **self.service.forget(player_id, [str(item) for item in targets])},
            )
            return

        segments = [urllib.parse.unquote(part) for part in path.strip("/").split("/")]
        if len(segments) == 3 and segments[0] == "players" and segments[2] == "reset":
            self._send(HTTPStatus.OK, {"player_id": segments[1], **self.service.reset(segments[1])})
            return

        self._error(HTTPStatus.NOT_FOUND, "not_found", f"unknown path {path}")


def build_service() -> TavernService:
    client = PowerContextClient(
        os.environ.get("POWERCONTEXT_URL", "http://127.0.0.1:8000"),
        token=os.environ.get("POWERCONTEXT_TOKEN") or None,
        timeout=float(os.environ.get("POWERCONTEXT_TIMEOUT_SECONDS", "15")),
    )
    return TavernService(
        client,
        memory_limit=int(os.environ.get("LETHE_MEMORY_LIMIT", "24")),
        memory_chars=int(os.environ.get("LETHE_MEMORY_CHARS", "4000")),
        scope_prefix=os.environ.get("LETHE_SCOPE_PREFIX", "lethe-tavern"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Lethe Tavern bridge for Dify")
    parser.add_argument("--host", default=os.environ.get("LETHE_BRIDGE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("LETHE_BRIDGE_PORT", "8080")))
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("LETHE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    BridgeHandler.service = build_service()
    BridgeHandler.bridge_token = os.environ.get("LETHE_BRIDGE_TOKEN") or None
    LOGGER.info("bridge listening on %s:%s -> %s", args.host, args.port, BridgeHandler.service.client.base_url)
    ThreadingHTTPServer((args.host, args.port), BridgeHandler).serve_forever()


if __name__ == "__main__":
    main()
