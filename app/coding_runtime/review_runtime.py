"""Independent Pi reviewer adapter; builder settings are not changed."""
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import json

from app.coding_runtime.contracts import CodingModel, CodingPrompt, CodingRuntimeCapabilities, CodingRuntimeError
from app.coding_runtime.pi_profile import PiModelProfile
from app.coding_runtime.pi_rpc import PiRpcClient, PiRpcConfig
from app.coding_runtime.review_output import RUBRIC
from app.coding_runtime.pi_runtime import PiCodingRuntime

EFFORTS = {"none": "off", "off": "off", "minimal": "minimal", "low": "low", "medium": "medium", "high": "high"}


def effort_choices(profile):
    return ["default", *[value for value in profile.reasoning_efforts if profile.supports_thinking and value in EFFORTS]]


def resolve_effort(profile, requested):
    if requested not in effort_choices(profile):
        raise CodingRuntimeError("unsupported_effort", "Reasoning effort is not declared and supported for this reviewer")
    selected = profile.default_reasoning_effort if requested == "default" else requested
    if selected is not None and selected not in effort_choices(profile):
        raise CodingRuntimeError("unsupported_effort", "The reviewer's default effort has no supported mapping")
    return EFFORTS.get(selected, "off")


@dataclass(frozen=True)
class ReviewRuntimeStart:
    workspace: Path
    model: CodingModel
    reasoning_supported: bool
    resolved_effort: str


class PiReviewRuntime(PiCodingRuntime):
    def capabilities(self):
        return CodingRuntimeCapabilities(confirmations=True)

    async def start(self, request: ReviewRuntimeStart):
        executable = Path(self._executable).expanduser()
        if not executable.is_absolute() or not executable.is_file():
            raise CodingRuntimeError("runtime_unavailable", "Configure the installed reviewer runtime executable")
        if not request.model.is_local or request.model.provider_family not in {"ollama", "local", "openai_compat"}:
            raise CodingRuntimeError("model_unsupported", "Reviewer currently requires an authorized local model profile")
        self._workspace = request.workspace
        model_id = request.model.model_id
        if model_id.startswith(("ollama_chat/", "ollama/", "openai/")):
            model_id = model_id.split("/", 1)[1]
        if request.model.provider_family == "ollama":
            self._ollama_model_id = f"ollama_chat/{model_id}"
        self._client = PiRpcClient(PiRpcConfig(workspace=request.workspace, command=(str(executable),),
            rpc_arguments=("--mode", "rpc", "--no-session", "--system-prompt", RUBRIC),
            model_profile=PiModelProfile("fruitcake-review", model_id, self._base_url, self._api_key,
                request.model.context_window, request.model.max_tokens, reasoning=request.reasoning_supported),
            tool_allowlist=("fruitcake_review_read",), review_mode=True))
        await self._client.start()
        levels = (await self._client.send_command("get_available_thinking_levels"))["data"]["levels"]
        if request.resolved_effort not in levels:
            raise CodingRuntimeError("unsupported_effort", "Installed runtime does not support the selected effort")
        await self._client.send_command("set_thinking_level", level=request.resolved_effort)
        readback = (await self._client.get_state()).get("thinkingLevel")
        if readback != request.resolved_effort:
            raise CodingRuntimeError("reasoning_mismatch", "Runtime did not retain the requested effort; no model prompt was sent")
        self.provenance = {"sent": request.resolved_effort, "rpc_acknowledged": True,
            "runtime_readback": readback, "evidence_source": "pi_rpc_get_state",
            "observed_at": datetime.now(timezone.utc).isoformat(), "provider_confirmation": None,
            "runtime_version": ".".join(map(str, self._client.version))}

    async def run(self, prompt: CodingPrompt):
        if prompt.history:
            raise CodingRuntimeError("runtime_denied", "Reviewer history must be empty")
        async for event in super().run(prompt):
            yield event

    async def answer_read(self, request_id, result):
        await self._active().respond_to_interaction(request_id, value=json.dumps(result, ensure_ascii=False))

    async def steer(self, message):
        raise CodingRuntimeError("runtime_denied", "Reviewer steering is unavailable")

    async def follow_up(self, message):
        raise CodingRuntimeError("runtime_denied", "Reviewer follow-ups require a fresh explicit review")
