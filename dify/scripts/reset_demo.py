"""Reset one tavern player to the scenario's initial state.

The bridge retires every active Memory entry for the player instead of deleting
the Scope, so PowerContext keeps the audit history of what was forgotten.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def post(base_url: str, path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise SystemExit(f"reset failed with HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise SystemExit(f"cannot reach the bridge at {base_url}: {error.reason}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset a Lethe Tavern demo player")
    parser.add_argument("--player", default=os.environ.get("LETHE_PLAYER", "demo-lin"))
    parser.add_argument("--bridge", default=os.environ.get("BRIDGE_URL", "http://127.0.0.1:8080"))
    args = parser.parse_args()

    result = post(args.bridge, f"/players/{args.player}/reset")
    print(json.dumps({"player": args.player, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
