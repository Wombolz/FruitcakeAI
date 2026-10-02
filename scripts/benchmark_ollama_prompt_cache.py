#!/usr/bin/env python3
"""Measure Ollama prompt-prefix reuse without involving Fruitcake chat state."""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from typing import Any


def _request(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=300) as response:
        result = json.loads(response.read().decode("utf-8"))
    result["client_duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result


def _metrics(label: str, result: dict[str, Any]) -> dict[str, Any]:
    prompt_tokens = int(result.get("prompt_eval_count", 0) or 0)
    cached_tokens = int(result.get("prompt_eval_cached_count", 0) or 0)
    return {
        "scenario": label,
        "prompt_tokens": prompt_tokens,
        "cached_prompt_tokens": cached_tokens,
        "cache_percent": round((cached_tokens / prompt_tokens) * 100, 2) if prompt_tokens else 0.0,
        "load_ms": round(int(result.get("load_duration", 0) or 0) / 1_000_000, 2),
        "prompt_eval_ms": round(int(result.get("prompt_eval_duration", 0) or 0) / 1_000_000, 2),
        "eval_ms": round(int(result.get("eval_duration", 0) or 0) / 1_000_000, 2),
        "client_ms": result["client_duration_ms"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, help="Ollama model name, without the LiteLLM prefix")
    parser.add_argument("--base-url", default="http://localhost:11434")
    parser.add_argument("--keep-alive", default="15m")
    args = parser.parse_args()

    stable_system = (
        "You are a concise local assistant. Follow the user request and return one short sentence. "
        "This intentionally stable prefix is repeated to measure Ollama prompt-cache reuse."
    )
    payloads = [
        ("warm_prefix", [{"role": "system", "content": stable_system}, {"role": "user", "content": "Say cache test one."}]),
        ("repeat_prefix", [{"role": "system", "content": stable_system}, {"role": "user", "content": "Say cache test two."}]),
        ("volatile_system", [{"role": "system", "content": f"Timestamp {time.time()}. {stable_system}"}, {"role": "user", "content": "Say cache test three."}]),
    ]
    url = f"{args.base_url.rstrip('/')}/api/chat"
    rows = []
    for label, messages in payloads:
        result = _request(
            url,
            {
                "model": args.model,
                "messages": messages,
                "stream": False,
                "keep_alive": args.keep_alive,
                "options": {"num_predict": 8},
            },
        )
        rows.append(_metrics(label, result))
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
