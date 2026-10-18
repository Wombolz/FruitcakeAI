#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.coding_runtime.pi_rpc import PiRpcClient, PiRpcConfig
from app.coding_runtime.pi_profile import PiModelProfile


def process_tree_rss_kib(root_pid: int | None) -> int | None:
    if root_pid is None:
        return None
    completed = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,rss="],
        check=True,
        capture_output=True,
        text=True,
    )
    rows: dict[int, tuple[int, int]] = {}
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3:
            pid, parent_pid, rss = (int(part) for part in parts)
            rows[pid] = (parent_pid, rss)
    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (parent_pid, _) in rows.items():
            if parent_pid in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    return sum(rows[pid][1] for pid in descendants if pid in rows)


async def probe(args: argparse.Namespace) -> dict[str, object]:
    command = (
        (str(args.executable),)
        if args.executable
        else ("npx", "--yes", f"@earendil-works/pi-coding-agent@{args.version}")
    )
    fixture = tempfile.TemporaryDirectory(prefix="fruitcake-coding-fixture-") if args.exercise_read else None
    workspace = Path(fixture.name) if fixture else args.workspace
    if fixture:
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        (workspace / "fixture.txt").write_text("FRUITCAKE_CODING_READ_OK\n", encoding="utf-8")
    profile = PiModelProfile("fruitcake-local", args.model, args.base_url) if args.model else None
    config = PiRpcConfig(
        workspace=workspace,
        command=command,
        model_profile=profile,
        rpc_arguments=("--mode", "rpc", "--no-session", "--thinking", "off"),
        environment={"PI_OFFLINE": "1"},
        command_timeout_seconds=args.timeout,
    )
    client = PiRpcClient(config)
    version_started = time.perf_counter()
    version = await client.probe_version()
    version_seconds = time.perf_counter() - version_started

    clients = [client, *(PiRpcClient(config) for _ in range(args.instances - 1))]

    async def start_and_read_state(instance: PiRpcClient) -> tuple[dict[str, object], float]:
        started = time.perf_counter()
        await instance.start()
        state = await instance.get_state()
        return state, time.perf_counter() - started

    exercise = {}
    try:
        states_and_timings = await asyncio.gather(*(start_and_read_state(instance) for instance in clients))
        states = [item[0] for item in states_and_timings]
        startup_seconds = [round(item[1], 4) for item in states_and_timings]
        rss_kib = [process_tree_rss_kib(instance.pid) for instance in clients]
        if args.exercise_read:
            async def consume():
                counts = {}
                operations = []
                answer = ""
                async for event in client.events():
                    counts[event.kind] = counts.get(event.kind, 0) + 1
                    if event.kind == "assistant.message":
                        answer = event.payload["text"]
                    if event.kind == "operation.completed":
                        operations.append({
                            "tool": event.payload["tool"],
                            "is_error": event.payload["is_error"],
                            "fixture_seen": "FRUITCAKE_CODING_READ_OK" in event.payload["text"],
                        })
                    if event.kind in {"run.completed", "run.failed", "session.failed"}:
                        break
                return {"event_counts": counts, "operations": operations, "answer": answer}

            consumer = asyncio.create_task(consume())
            started = time.perf_counter()
            run = asyncio.create_task(client.prompt_and_wait(
                "Use the read tool to read fixture.txt. Reply only with the token in that file.",
                timeout=args.prompt_timeout,
            ))
            peak_rss = 0
            try:
                while not run.done():
                    peak_rss = max(peak_rss, await asyncio.to_thread(process_tree_rss_kib, client.pid) or 0)
                    await asyncio.sleep(0.1)
                result = await run
                exercise = await asyncio.wait_for(consumer, 2)
                exercise.update({
                    "elapsed_seconds": round(time.perf_counter() - started, 4),
                    "peak_pi_process_tree_rss_kib": peak_rss,
                    "settled": result.settled, "aborted": result.aborted, "error": result.error,
                })
            finally:
                for task in (run, consumer):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(run, consumer, return_exceptions=True)
    finally:
        shutdown_started = time.perf_counter()
        await asyncio.gather(*(instance.close() for instance in clients))
        shutdown_seconds = time.perf_counter() - shutdown_started
        if fixture:
            fixture.cleanup()
    return {
        "package_version": ".".join(str(part) for part in version),
        "instances": args.instances,
        "version_probe_seconds": round(version_seconds, 4),
        "startup_to_state_seconds": startup_seconds,
        "idle_process_tree_rss_kib": rss_kib,
        "idle_process_tree_total_rss_kib": sum(value or 0 for value in rss_kib),
        "shutdown_seconds": round(shutdown_seconds, 4),
        "session_id_present": [bool(state.get("sessionId")) for state in states],
        "is_streaming": [state.get("isStreaming") for state in states],
        "model_provider": [(state.get("model") or {}).get("provider") for state in states],
        "stderr": client.stderr,
        "read_exercise": exercise,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure the bounded Pi RPC Phase 0 startup path.")
    parser.add_argument("--version", default="1.3.0")
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--instances", type=int, default=1)
    parser.add_argument("--model")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    parser.add_argument("--exercise-read", action="store_true", help="Read a disposable fixture using the selected model")
    parser.add_argument("--prompt-timeout", type=float, default=60.0)
    args = parser.parse_args()
    if args.instances < 1:
        parser.error("--instances must be at least 1")
    if args.exercise_read and (not args.model or args.instances != 1):
        parser.error("--exercise-read requires --model and --instances 1")
    print(json.dumps(asyncio.run(probe(args)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
