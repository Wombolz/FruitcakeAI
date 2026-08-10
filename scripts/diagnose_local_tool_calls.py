"""Root-cause harness for qwen tool-calling failures over Ollama.

Isolates which layer produces malformed-tool-call failures
("failed to parse JSON" errors from the Ollama server) by running the same
post-tool conversation through three legs:

  A. native_correct       — POST /api/chat with a well-formed transcript:
                            assistant tool_calls preserved, tool_name on the
                            tool-result message. This is what Ollama expects.
  B. native_litellm_shape — POST /api/chat with the transcript exactly as
                            litellm 1.82.2's ollama_chat transform emits it:
                            assistant tool_calls stripped (empty assistant
                            message), tool result without tool_name.
  C. litellm_e2e          — litellm.acompletion(model="ollama_chat/...") with
                            the app's OpenAI-shaped history, i.e. the real
                            production path (goes through the buggy transform).

If A is clean while B and C degrade the same way, the root cause is the
litellm request translation, not the model or Ollama's parser.

Usage:
  .venv/bin/python scripts/diagnose_local_tool_calls.py --trials 5
  .venv/bin/python scripts/diagnose_local_tool_calls.py --legs native_correct litellm_e2e --scenarios followup_after_tools
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.agent.tools import TOOL_SCHEMAS  # noqa: E402
from app.agent.model_stream import (  # noqa: E402
    ModelTurnAccumulator,
    close_provider_stream,
    iter_model_stream_events,
)

OLLAMA_BASE = "http://localhost:11434"
DEFAULT_MODEL = "qwen3.6:35b"

SYSTEM_PROMPT = (
    "You are Fruitcake, a helpful household assistant. Use the provided tools "
    "when they are needed to answer. When you already have tool evidence in "
    "the conversation, answer directly from it."
)

# Realistic summarize_document result, shaped like the app's tool output.
SUMMARY_TOOL_RESULT = """**Summary of 'incident_report_q2.md' (48 total sections):**
_Note: This document has 48 sections. The summary covers 24 evenly-spaced samples from throughout._

### Major sections
- Executive overview of the Q2 reliability incidents
- Timeline of the April 14 ingestion outage
- Postmortem for the May 3 scheduler stall
- Remediation workstreams and ownership

### Key findings
- The April 14 outage began at 09:42 UTC when the RSS ingestion worker exhausted database connections.
- Recovery required a manual restart of the worker pool at 11:15 UTC; total user-facing degradation lasted 93 minutes.
- The May 3 scheduler stall was traced to a deadlock between task cleanup and the run-now path.
- Three remediation items were completed in June: connection pooling limits, scheduler lock ordering, and alerting on queue depth.
- One item remains open: automated failover for the ingestion worker, owned by the platform team.

### Caveats
- Sampling covered 24 of 48 sections; appendix-level detail may be missing.
- The report does not state a target date for the open failover item.
"""

SCENARIOS: dict[str, dict[str, Any]] = {
    # Turn right after a large tool result: the model should synthesize text.
    # This is the turn that historically threw "failed to parse JSON".
    "post_tool_synthesis": {
        "user_1": "Summarize the incident report from my library.",
        "followup": None,
    },
    # A follow-up question after synthesis, where reaching for another tool
    # call is plausible — exercises tool-call emission with history present.
    "followup_after_tools": {
        "user_1": "Summarize the incident report from my library.",
        "assistant_synthesis": (
            "Here's the short version: Q2 had two major incidents (April 14 "
            "ingestion outage, May 3 scheduler stall). Three of four "
            "remediation items are done; automated ingestion failover is "
            "still open with the platform team."
        ),
        "followup": "What exact times are mentioned for the April outage? Check the library again if you need to.",
    },
}

TOOL_CALL_ID = "call_diag_001"
TOOL_NAME = "summarize_document"
TOOL_ARGS = {"document_name": "incident_report_q2.md"}


def _openai_history(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    """History in the app's shape (OpenAI format), fed to litellm."""
    history: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": scenario["user_1"]},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": TOOL_CALL_ID,
                    "type": "function",
                    "function": {"name": TOOL_NAME, "arguments": json.dumps(TOOL_ARGS)},
                }
            ],
        },
        {"role": "tool", "tool_call_id": TOOL_CALL_ID, "content": SUMMARY_TOOL_RESULT},
    ]
    if scenario.get("assistant_synthesis"):
        history.append({"role": "assistant", "content": scenario["assistant_synthesis"]})
    if scenario.get("followup"):
        history.append({"role": "user", "content": scenario["followup"]})
    return history


def _native_history(scenario: dict[str, Any], *, correct: bool) -> list[dict[str, Any]]:
    """History in Ollama /api/chat shape.

    correct=True  → assistant tool_calls preserved + tool_name on result.
    correct=False → the shape litellm 1.82.2 actually sends (tool_calls
                    stripped, bare tool message) — reproduced faithfully.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": scenario["user_1"]},
    ]
    if correct:
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": TOOL_NAME, "arguments": TOOL_ARGS}}],
            }
        )
        messages.append({"role": "tool", "tool_name": TOOL_NAME, "content": SUMMARY_TOOL_RESULT})
    else:
        messages.append({"role": "assistant", "content": ""})
        messages.append({"role": "tool", "content": SUMMARY_TOOL_RESULT})
    if scenario.get("assistant_synthesis"):
        messages.append({"role": "assistant", "content": scenario["assistant_synthesis"]})
    if scenario.get("followup"):
        messages.append({"role": "user", "content": scenario["followup"]})
    return messages


def _classify_native(payload: dict[str, Any]) -> dict[str, Any]:
    message = payload.get("message") or {}
    tool_calls = message.get("tool_calls") or []
    content = str(message.get("content") or "")
    thinking = str(message.get("thinking") or "")
    base = {
        "content_chars": len(content),
        "thinking_chars": len(thinking),
        "done_reason": str(payload.get("done_reason") or ""),
        "content_preview": content[:200],
    }
    if tool_calls:
        names = []
        args_ok = True
        for call in tool_calls:
            fn = call.get("function") or {}
            names.append(str(fn.get("name") or ""))
            if not isinstance(fn.get("arguments"), (dict, str)):
                args_ok = False
        return {
            "outcome": "tool_call",
            "tool_names": names,
            "arguments_well_formed": args_ok,
            **base,
        }
    if not content.strip():
        return {"outcome": "empty_answer", **base}
    return {"outcome": "text_answer", **base}


def _classify_error(error_text: str) -> str:
    lowered = error_text.lower()
    if "failed to parse json" in lowered:
        return "error_parse_json"
    if "does not support tools" in lowered:
        return "error_tools_unsupported"
    if "must be at the beginning" in lowered:
        return "error_template"
    return "error_other"


async def _run_native(
    client: httpx.AsyncClient,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "stream": False,
        "options": {"num_predict": 2400},
    }
    started = time.perf_counter()
    try:
        resp = await client.post(f"{OLLAMA_BASE}/api/chat", json=body, timeout=300)
        elapsed = time.perf_counter() - started
        if resp.status_code != 200:
            return {
                "outcome": _classify_error(resp.text),
                "status_code": resp.status_code,
                "error_preview": resp.text[:400],
                "elapsed_s": round(elapsed, 1),
            }
        result = _classify_native(resp.json())
        result["elapsed_s"] = round(elapsed, 1)
        return result
    except Exception as exc:  # noqa: BLE001 — harness records everything
        return {
            "outcome": _classify_error(str(exc)),
            "error_preview": str(exc)[:400],
            "elapsed_s": round(time.perf_counter() - started, 1),
        }


async def _run_litellm(
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    import litellm

    started = time.perf_counter()
    try:
        resp = await litellm.acompletion(
            model=f"ollama_chat/{model}",
            messages=messages,
            tools=tools,
            api_base=OLLAMA_BASE,
            max_tokens=2400,
            timeout=300,
        )
        elapsed = time.perf_counter() - started
        message = resp.choices[0].message
        tool_calls = message.tool_calls or []
        if tool_calls:
            names = []
            args_ok = True
            for call in tool_calls:
                names.append(str(call.function.name or ""))
                try:
                    json.loads(call.function.arguments or "{}")
                except (TypeError, ValueError):
                    args_ok = False
            return {
                "outcome": "tool_call",
                "tool_names": names,
                "arguments_well_formed": args_ok,
                "content_preview": str(message.content or "")[:200],
                "elapsed_s": round(elapsed, 1),
            }
        content = str(message.content or "")
        return {
            "outcome": "text_answer" if content.strip() else "empty_answer",
            "content_chars": len(content),
            "content_preview": content[:200],
            "elapsed_s": round(elapsed, 1),
        }
    except Exception as exc:  # noqa: BLE001 — harness records everything
        return {
            "outcome": _classify_error(str(exc)),
            "error_preview": str(exc)[:400],
            "elapsed_s": round(time.perf_counter() - started, 1),
        }


async def _run_litellm_stream(
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    """Capture sanitized normalized stream shape without recording raw content."""
    import litellm

    started = time.perf_counter()
    provider_stream = None
    accumulator = ModelTurnAccumulator(request_id="diagnostic")
    event_counts: dict[str, int] = {}
    tool_shapes: list[dict[str, Any]] = []
    try:
        provider_stream = await litellm.acompletion(
            model=f"ollama_chat/{model}",
            messages=messages,
            tools=tools,
            stream=True,
            reasoning_effort="high",
            api_base=OLLAMA_BASE,
            max_tokens=2400,
            timeout=300,
        )
        try:
            async for event in iter_model_stream_events(provider_stream):
                accumulator.add(event)
                event_counts[event.kind] = event_counts.get(event.kind, 0) + 1
                if event.kind == "tool_call_delta":
                    tool_shapes.append(
                        {
                            "has_index": event.tool_call_index is not None,
                            "position": event.tool_call_position,
                            "has_id": bool(event.tool_call_id),
                            "name": event.tool_name or "",
                            "arguments_type": type(event.arguments_payload).__name__,
                            "complete": event.tool_call_complete,
                        }
                    )
        finally:
            await close_provider_stream(provider_stream)
        turn = accumulator.finish()
        return {
            "outcome": "tool_call" if turn.tool_calls else ("text_answer" if turn.content.strip() else "empty_answer"),
            "event_counts": event_counts,
            "tool_shapes": tool_shapes[:8],
            "tool_names": [call["function"]["name"] for call in turn.tool_calls],
            "content_chars": len(turn.content),
            "reasoning_chars": len(turn.reasoning_content),
            "finish_reason": turn.finish_reason or "",
            "usage_present": bool(turn.usage),
            "elapsed_s": round(time.perf_counter() - started, 1),
        }
    except Exception as exc:  # noqa: BLE001 — harness records bounded error previews
        return {
            "outcome": _classify_error(str(exc)),
            "error_preview": str(exc)[:400],
            "events_before_error": accumulator.event_count,
            "elapsed_s": round(time.perf_counter() - started, 1),
        }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument(
        "--legs",
        nargs="+",
        default=["native_correct", "native_litellm_shape", "litellm_e2e", "litellm_stream"],
        choices=["native_correct", "native_litellm_shape", "litellm_e2e", "litellm_stream"],
    )
    parser.add_argument("--scenarios", nargs="+", default=list(SCENARIOS.keys()), choices=list(SCENARIOS.keys()))
    parser.add_argument("--max-tools", type=int, default=0, help="cap the tool surface (0 = full app surface)")
    parser.add_argument("--out", default="scripts/diagnose_local_tool_calls_results.jsonl")
    args = parser.parse_args()

    tools = list(TOOL_SCHEMAS)
    if args.max_tools > 0:
        tools = tools[: args.max_tools]

    out_path = REPO_ROOT / args.out
    results: list[dict[str, Any]] = []

    async with httpx.AsyncClient() as client:
        for scenario_name in args.scenarios:
            scenario = SCENARIOS[scenario_name]
            for leg in args.legs:
                for trial in range(args.trials):
                    if leg == "native_correct":
                        outcome = await _run_native(
                            client,
                            model=args.model,
                            messages=_native_history(scenario, correct=True),
                            tools=tools,
                        )
                    elif leg == "native_litellm_shape":
                        outcome = await _run_native(
                            client,
                            model=args.model,
                            messages=_native_history(scenario, correct=False),
                            tools=tools,
                        )
                    elif leg == "litellm_e2e":
                        outcome = await _run_litellm(
                            model=args.model,
                            messages=_openai_history(scenario),
                            tools=tools,
                        )
                    else:
                        outcome = await _run_litellm_stream(
                            model=args.model,
                            messages=_openai_history(scenario),
                            tools=tools,
                        )
                    record = {
                        "scenario": scenario_name,
                        "leg": leg,
                        "trial": trial,
                        "model": args.model,
                        "tool_count": len(tools),
                        **outcome,
                    }
                    results.append(record)
                    print(json.dumps(record), flush=True)

    with out_path.open("w") as fh:
        for record in results:
            fh.write(json.dumps(record) + "\n")

    print("\n=== summary ===")
    tally: dict[tuple[str, str], dict[str, int]] = {}
    for record in results:
        key = (record["scenario"], record["leg"])
        tally.setdefault(key, {})
        tally[key][record["outcome"]] = tally[key].get(record["outcome"], 0) + 1
    for (scenario_name, leg), counts in tally.items():
        print(f"{scenario_name:24s} {leg:22s} {counts}")


if __name__ == "__main__":
    asyncio.run(main())
