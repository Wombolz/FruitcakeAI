from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time


if "--version" in sys.argv:
    print(os.environ.get("FAKE_PI_VERSION", "1.3.0"))
    raise SystemExit(0)


def emit(payload: dict) -> None:
    sys.stdout.buffer.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
    sys.stdout.buffer.flush()


if "--child" in sys.argv:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path("child.pid").write_text(str(child.pid))


for raw_line in sys.stdin.buffer:
    command = json.loads(raw_line)
    request_id = command.get("id")
    command_type = command["type"]
    if command_type == "get_state":
        emit(
            {
                "id": request_id,
                "type": "response",
                "command": command_type,
                "success": True,
                "data": {"sessionId": "fake-session", "isStreaming": False},
            }
        )
    elif command_type == "prompt":
        message = command["message"]
        if message == "handled":
            emit(
                {
                    "id": request_id,
                    "type": "response",
                    "command": command_type,
                    "success": True,
                    "data": {"disposition": "handled"},
                }
            )
            continue
        if message == "malformed":
            sys.stdout.buffer.write(b'{"type":"runtime_note","text":"left\xe2\x80\xa8right"}\n')
            sys.stdout.buffer.write(b"not-json\n")
            sys.stdout.buffer.flush()
        if message == "invalid_shape":
            emit({"type": "message_end", "message": ["not", "an", "object"]})
        if message == "split_utf8":
            record = {"type": "message_update", "assistantMessageEvent": {
                "type": "text_delta", "delta": "before\u2028after", "contentIndex": 0,
            }}
            encoded = json.dumps(record, ensure_ascii=False).encode() + b"\r\n"
            for byte in encoded:
                sys.stdout.buffer.write(bytes([byte]))
                sys.stdout.buffer.flush()
                time.sleep(0.0001)
        if message == "stderr":
            sys.stderr.write("x" * 256 + "tail-marker")
            sys.stderr.flush()
        if message == "exit":
            sys.stderr.write("simulated crash")
            sys.stderr.flush()
            raise SystemExit(17)
        emit(
            {
                "id": request_id,
                "type": "response",
                "command": command_type,
                "success": True,
                "data": {"disposition": "started"},
            }
        )
        emit({"type": "agent_start"})
        emit({"type": "turn_start"})
        if message in {"provider_error", "recovered_error"}:
            emit({"type": "message_end", "message": {
                "role": "assistant", "stopReason": "error", "errorMessage": "provider unavailable",
            }})
            if message == "recovered_error":
                emit({"type": "message_end", "message": {
                    "role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "Recovered"}],
                }})
        emit(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "hello\u2028world"},
            }
        )
        emit({"type": "agent_end", "messages": [], "willRetry": False})
        if message == "wait_for_abort":
            continue
        if message == "delay":
            time.sleep(0.2)
        emit({"type": "agent_settled", "aborted": False})
    elif command_type in {"steer", "follow_up"}:
        emit(
            {
                "id": request_id,
                "type": "response",
                "command": command_type,
                "success": True,
                "data": {"disposition": "queued"},
            }
        )
    elif command_type == "abort":
        emit({"id": request_id, "type": "response", "command": command_type, "success": True})
        emit({"type": "agent_settled", "aborted": True})
    elif command_type == "clear_queue":
        emit({"id": request_id, "type": "response", "command": command_type, "success": True,
              "data": {"steering": [], "followUp": []}})
    elif command_type == "duplicate":
        response = {"id": request_id, "type": "response", "command": command_type, "success": True}
        emit(response)
        emit(response)
    elif command_type == "fail":
        emit(
            {
                "id": request_id,
                "type": "response",
                "command": command_type,
                "success": False,
                "error": "simulated failure",
            }
        )
    elif command_type == "hang":
        continue
    else:
        emit({"id": request_id, "type": "response", "command": "wrong", "success": True})

if "--stubborn" in sys.argv:
    time.sleep(60)
