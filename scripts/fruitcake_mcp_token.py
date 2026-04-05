#!/usr/bin/env python3
"""
Print a Fruitcake access token for Codex MCP use.

Usage examples:
  python3 scripts/fruitcake_mcp_token.py --username alice --password secret --export
  FRUITCAKE_MCP_USERNAME=alice FRUITCAKE_MCP_PASSWORD=secret python3 scripts/fruitcake_mcp_token.py --export
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch a Fruitcake JWT access token for MCP use.")
    parser.add_argument("--base-url", default=os.environ.get("FRUITCAKE_MCP_BASE_URL", "http://localhost:30417"))
    parser.add_argument("--username", default=os.environ.get("FRUITCAKE_MCP_USERNAME"))
    parser.add_argument("--password", default=os.environ.get("FRUITCAKE_MCP_PASSWORD"))
    parser.add_argument(
        "--env-var",
        default=os.environ.get("FRUITCAKE_MCP_ENV_VAR", "FRUITCAKE_MCP_TOKEN"),
        help="Environment variable name to print when using --export.",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="Print a shell export command instead of the raw token.",
    )
    return parser.parse_args()


def _login(base_url: str, username: str, password: str) -> str:
    payload = json.dumps({"username": username, "password": password}).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/auth/login",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise SystemExit(f"Login failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach Fruitcake at {base_url}: {exc.reason}") from exc

    token = str(body.get("access_token") or "").strip()
    if not token:
        raise SystemExit("Fruitcake login response did not include an access_token.")
    return token


def main() -> int:
    args = _parse_args()
    if not args.username or not args.password:
        print("username and password are required (flags or FRUITCAKE_MCP_USERNAME / FRUITCAKE_MCP_PASSWORD).", file=sys.stderr)
        return 2

    token = _login(args.base_url, args.username, args.password)
    if args.export:
        print(f'export {args.env_var}="{token}"')
    else:
        print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
