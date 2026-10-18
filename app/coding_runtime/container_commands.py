"""Host-owned, offline Docker command execution; never a host shell."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
from uuid import UUID

from app.coding_runtime.contracts import CodingRuntimeError

PROTOCOL = "fruitcake.container_command.v1"
TOOL = "fruitcake_run"
TITLE = "Approve container command?"
OUTPUT_BYTES = 64 * 1024
PROPOSAL_BYTES = 64 * 1024
MAX_SECONDS = 300
LABEL = "fruitcake.coding-command=true"


def validate_input(proposal: dict) -> dict:
    if not isinstance(proposal, dict) or set(proposal) != {"protocol", "operation_id", "command", "working_directory", "timeout_seconds"}:
        raise CodingRuntimeError("command_invalid", "A complete container command proposal is required")
    command, directory, seconds = (proposal.get(key) for key in ("command", "working_directory", "timeout_seconds"))
    if proposal["protocol"] != PROTOCOL or not isinstance(proposal["operation_id"], str) or not 0 < len(proposal["operation_id"]) <= 200:
        raise CodingRuntimeError("command_invalid", "Invalid container command identity")
    if not isinstance(command, str) or not command.strip() or len(command.encode("utf-8")) > 8192 or "\0" in command:
        raise CodingRuntimeError("command_invalid", "Commands must contain 1-8192 UTF-8 bytes without NUL")
    if not isinstance(directory, str) or len(directory) > 512 or any(ord(c) < 32 for c in directory):
        raise CodingRuntimeError("command_invalid", "Invalid checkout-relative working directory")
    if directory != "." and any(part in {"", ".", ".."} or part.casefold() == ".git" for part in directory.split("/")):
        raise CodingRuntimeError("command_invalid", "Use a checkout-relative directory outside Git metadata")
    if type(seconds) is not int or not 1 <= seconds <= MAX_SECONDS:
        raise CodingRuntimeError("command_invalid", "Command timeout must be 1-300 seconds")
    return {"command": command, "working_directory": directory, "timeout_seconds": seconds}


def working_directory(root: Path, directory: str) -> Path:
    target = root.joinpath(directory)
    if target.resolve(strict=True) != target or not target.is_dir():
        raise CodingRuntimeError("command_invalid", "Command directory must exist without symbolic links")
    return target


def command_hash(command: dict) -> str:
    fields = {key: command[key] for key in ("id", "command", "working_directory", "timeout_seconds", "image", "network", "filesystem")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class DockerCommandRunner:
    def __init__(self, image: str = "fruitcake/mcp-shell", executable: str = "docker"):
        self.image = image
        self.executable = shutil.which(executable) or executable
        self._capacity = asyncio.Semaphore(2)

    def environment(self):
        return {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "TMPDIR") if key in os.environ}

    async def _control(self, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(self.executable, *args, env=self.environment(),
                                                      stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            async with asyncio.timeout(10):
                out, _ = await process.communicate()
        except BaseException:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise CodingRuntimeError("container_unavailable", "Docker command failed. Check Docker and the installed command image; no host fallback is allowed.")
        return out.decode("utf-8").strip()

    async def image_id(self) -> str:
        try:
            image = await self._control("image", "inspect", "--format", "{{.Id}}", self.image)
        except OSError as exc:
            raise CodingRuntimeError("container_unavailable", "Docker is unavailable; no host fallback is allowed") from exc
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise CodingRuntimeError("container_unavailable", "Docker returned an invalid image identity")
        return image

    def name(self, identity: str) -> str:
        return "fruitcake-coding-" + str(UUID(identity))

    def create_arguments(self, root: Path, command: dict) -> tuple[str, ...]:
        working_directory(root, command["working_directory"])
        if root.resolve(strict=True) != root or not (root / ".git").is_file() or (root / ".git").is_symlink() or "," in str(root):
            raise CodingRuntimeError("command_invalid", "A canonical managed checkout is required")
        return (
            "create", "--name", self.name(command["id"]), "--label", LABEL, "--pull", "never",
            "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "128", "--memory", "512m", "--cpus", "1", "--init", "--log-driver", "none",
            "--user", f"{os.getuid()}:{os.getgid()}", "--tmpfs", "/tmp:rw,nosuid,nodev,size=268435456,mode=1777",
            "--mount", f"type=bind,src={root},dst=/workspace",
            "--mount", f"type=bind,src={root / '.git'},dst=/workspace/.git,readonly",
            "--workdir", "/workspace" + ("" if command["working_directory"] == "." else "/" + command["working_directory"]),
            "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint", "/usr/bin/timeout",
            command["image"], "--kill-after=2", str(command["timeout_seconds"]), "/bin/sh", "-c", command["command"],
        )

    async def remove(self, identity: str) -> None:
        name = self.name(identity)
        # Names are generated by Fruitcake, not supplied by an agent or shell.
        try:
            await self._control("rm", "--force", name)
        except Exception:
            # Missing is success; a present container after failure is not.
            try:
                remaining = await self._control("ps", "-aq", "--filter", f"name=^/{name}$", "--filter", f"label={LABEL}")
            except Exception as exc:
                raise CodingRuntimeError("container_cleanup_failed", "Container cleanup could not be verified; inspect Docker") from exc
            if remaining:
                raise CodingRuntimeError("container_cleanup_failed", "Command container could not be removed; inspect Docker")

    async def recover(self) -> None:
        ids = (await self._control("ps", "-aq", "--filter", f"label={LABEL}")).splitlines()
        if any(not re.fullmatch(r"[a-f0-9]{12,64}", identity) for identity in ids):
            raise CodingRuntimeError("container_cleanup_failed", "Docker returned invalid recovery identities")
        if ids:
            await self._control("rm", "--force", *ids)

    async def execute(self, root: Path, command: dict, on_output) -> dict:
        async with self._capacity:
            return await self._execute(root, command, on_output)

    async def _execute(self, root: Path, command: dict, on_output) -> dict:
        output = {"stdout": bytearray(), "stderr": bytearray()}
        totals = {"stdout": 0, "stderr": 0}
        process = None
        readers = []
        last_update = 0.0
        started = time.monotonic()
        result = {"status": "failed", "exit_code": None}

        def snapshot():
            return {**result, **{key: data.decode("utf-8", errors="replace") for key, data in output.items()},
                    "output_truncated": any(totals[key] > OUTPUT_BYTES for key in totals),
                    "duration_ms": round((time.monotonic() - started) * 1000)}

        async def read(stream, key):
            nonlocal last_update
            while chunk := await stream.read(8192):
                totals[key] += len(chunk)
                output[key].extend(chunk[:max(0, OUTPUT_BYTES - len(output[key]))])
                if time.monotonic() - last_update >= 0.5:
                    last_update = time.monotonic()
                    await on_output(snapshot())

        try:
            await self._control(*self.create_arguments(root, command))
            process = await asyncio.create_subprocess_exec(self.executable, "start", "--attach", self.name(command["id"]),
                                                          env=self.environment(), stdout=asyncio.subprocess.PIPE,
                                                          stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL)
            readers = [asyncio.create_task(read(process.stdout, "stdout")), asyncio.create_task(read(process.stderr, "stderr"))]
            try:
                async with asyncio.timeout(command["timeout_seconds"] + 5):
                    await process.wait()
                    await asyncio.gather(*readers)
                state = json.loads(await self._control("inspect", "--format", "{{json .State}}", self.name(command["id"])))
                status = "completed" if state["ExitCode"] == 0 else "timed_out" if state["ExitCode"] in {124, 137} else "failed"
                result = {"status": "failed" if state["OOMKilled"] else status,
                          "exit_code": state["ExitCode"], "oom_killed": state["OOMKilled"]}
            except TimeoutError:
                result = {"status": "timed_out", "exit_code": 124}
        finally:
            try:
                await asyncio.shield(self.remove(command["id"]))
            finally:
                if process is not None:
                    if process.returncode is None:
                        with suppress(ProcessLookupError):
                            process.kill()
                    await process.wait()
                for task in readers:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
        return snapshot()
