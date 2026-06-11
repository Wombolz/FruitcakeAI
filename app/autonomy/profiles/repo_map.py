from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.agent.definition_loader import get_agent_preset
from app.autonomy.profiles.base import TaskExecutionProfile
from app.host_root_access import (
    build_host_root_scan_summary,
    ensure_host_root_access,
    is_workspace_path_for_user,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


class RepoMapExecutionProfile(TaskExecutionProfile):
    name = "repo_map"

    async def prepare_run_context(
        self,
        *,
        db,
        user_id: int,
        task_id: int,
        task_run_id: Optional[int],
    ) -> Dict[str, Any]:
        del user_id, task_run_id
        from app.db.models import Task

        task = await db.get(Task, task_id)
        recipe = task.task_recipe if isinstance(task.task_recipe, dict) else {}
        params = recipe.get("params") if isinstance(recipe.get("params"), dict) else {}
        contract = _build_repo_map_contract(params=params)
        contract["root_inspections"] = await _build_repo_map_root_inspections(
            db=db,
            user_id=task.user_id,
            included_roots=contract.get("included_roots") or [],
            ignored_paths=contract.get("ignored_paths") or [],
        )
        return {
            "repo_map_contract": contract,
            "agent_role": "repo_map_manager",
        }

    def allow_skill_injection(self, *, run_context: Dict[str, Any]) -> bool:
        return False

    def effective_allowed_tools(self, *, run_context: Dict[str, Any]) -> Optional[set[str]]:
        del run_context
        return {"write_file", "append_file"}

    def augment_prompt(
        self,
        *,
        prompt_parts: list[str],
        run_context: Dict[str, Any],
        is_final_step: bool,
    ) -> None:
        del is_final_step
        contract = run_context.get("repo_map_contract")
        if isinstance(contract, dict):
            prompt_parts.append(_format_repo_map_prompt_contract(contract))

    def augment_run_debug(
        self,
        *,
        run_debug: Dict[str, Any],
        run_context: Dict[str, Any],
    ) -> None:
        contract = run_context.get("repo_map_contract")
        if isinstance(contract, dict):
            run_debug["repo_map_contract"] = contract
        tool_records = run_context.get("last_tool_records")
        if isinstance(tool_records, list) and tool_records:
            run_debug["last_tool_records"] = tool_records

    def validate_finalize(
        self,
        *,
        result: str,
        prior_full_outputs: List[str],
        run_context: Dict[str, Any],
        is_final_step: bool,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        del prior_full_outputs
        if not is_final_step:
            return result, None
        report = _validate_repo_map_output(
            result=result,
            repo_map_contract=run_context.get("repo_map_contract")
            if isinstance(run_context.get("repo_map_contract"), dict)
            else None,
            tool_records=run_context.get("last_tool_records")
            if isinstance(run_context.get("last_tool_records"), list)
            else None,
        )
        return result, report


def build_repo_map_behavior_contract() -> str:
    preset = get_agent_preset("repo_map_manager")
    if not preset or not preset.output_contract:
        return ""
    contract = ", ".join(preset.output_contract)
    return (
        f"Agent output contract: produce this agent's assigned output and include: {contract}. "
        "Do not return a task draft, saved-task confirmation, RSS/news briefing, unrelated completion, "
        "or a request for the operator to manually save/paste the report."
    )


def _build_repo_map_contract(*, params: dict[str, Any]) -> dict[str, Any]:
    included_roots = [
        str(item).strip()
        for item in (params.get("included_roots") or [])
        if str(item).strip()
    ]
    ignored_paths = [
        str(item).strip()
        for item in (params.get("ignored_paths") or [])
        if str(item).strip()
    ]
    output_path = str(params.get("output_path") or "").strip()
    return {
        "included_roots": included_roots,
        "ignored_paths": ignored_paths,
        "output_path": output_path,
        "root_inspections": [],
    }


async def _build_repo_map_root_inspections(
    *,
    db,
    user_id: int,
    included_roots: list[str],
    ignored_paths: list[str],
) -> list[dict[str, Any]]:
    inspections: list[dict[str, Any]] = []
    for root in included_roots:
        inspections.append(
            await _inspect_repo_map_root(
                db=db,
                user_id=user_id,
                root=root,
                ignored_paths=ignored_paths,
            )
        )
    return inspections


async def _inspect_repo_map_root(
    *,
    db,
    user_id: int,
    root: str,
    ignored_paths: list[str],
) -> dict[str, Any]:
    path = Path(root).expanduser()
    if not path.is_absolute():
        path = (_REPO_ROOT / path).resolve(strict=False)
    else:
        path = path.resolve(strict=False)
    workspace_path = is_workspace_path_for_user(user_id, str(path))
    if path.exists() and path.is_dir() and not workspace_path:
        await ensure_host_root_access(
            db,
            user_id=user_id,
            raw_path=str(path),
            requester="repo_map_manager",
            reason=(
                "Repo map manager needs read-only access to the configured host root "
                "to inspect directory structure and summarize workspace orientation."
            ),
        )
    exists = path.exists()
    is_dir = path.is_dir()
    children: list[str] = []
    if is_dir:
        try:
            children = sorted(item.name for item in path.iterdir())[:200]
        except Exception:
            children = []
    notable_children = [
        name
        for name in ("app", "tests", "config", "scripts", "Docs", "README.md", "requirements.txt")
        if name in children
    ]
    scan_summary = build_host_root_scan_summary(path, ignored_paths=ignored_paths) if is_dir else {}
    return {
        "configured_root": root,
        "resolved_root": str(path),
        "exists": exists,
        "is_dir": is_dir,
        "notable_children": notable_children,
        "workspace_path": workspace_path,
        "tree_lines": scan_summary.get("tree_lines") or [],
        "preview_file": scan_summary.get("preview_file"),
        "preview_text": scan_summary.get("preview_text"),
    }


def _format_repo_map_prompt_contract(contract: dict[str, Any]) -> str:
    inspections = contract.get("root_inspections") or []
    lines = [
        build_repo_map_behavior_contract(),
        "Repo-map run configuration (authoritative):",
        f"- included_roots: {json.dumps(contract.get('included_roots') or [], ensure_ascii=True)}",
        f"- ignored_paths: {json.dumps(contract.get('ignored_paths') or [], ensure_ascii=True)}",
        f"- output_path: {str(contract.get('output_path') or '').strip() or 'MISSING'}",
        "- Use the configured included_roots above as the scan base. Do not substitute documentation paths as the primary workspace truth.",
        "- Backend root preflight below is authoritative for configured host roots. Use it as your scan evidence instead of claiming the repo is inaccessible.",
        "- Do not call generic filesystem listing or read tools against configured host-root paths outside the workspace sandbox.",
        "- If you write a workspace file, write only to the configured output_path above.",
        "- Do not invent timestamped artifact paths unless they are explicitly configured as output_path.",
        "- Final output must state the roots checked and artifacts updated using the exact configured values.",
        "- A healthy completion must actually write the configured output_path or clearly report that the artifact was updated there.",
        "- Do not say the report was not written, do not ask the operator to paste/save it manually, and do not describe workspace access limitations for an approved host root.",
        "- Structure the final report as a navigation artifact with sections such as: Repo Overview, Key Roots, Entrypoints by Subsystem, Tests and Validation, Open Markers, and Noise / Ignore Areas.",
        "- Separate directly observed repo facts from secondary context and suggested follow-up. Prefer labels like: Observed, Secondary context, Suggested follow-up.",
        "- Treat .claude and nested worktrees as secondary context unless the configured root explicitly points into them.",
        "- Do not present worktree-local paths as canonical top-level repo structure without labeling them as worktree-local or secondary context.",
    ]
    if inspections:
        lines.append("Root preflight from backend:")
        for item in inspections:
            if not isinstance(item, dict):
                continue
            lines.append(
                "- "
                    f"{item.get('configured_root')}: exists={item.get('exists')}, "
                    f"is_dir={item.get('is_dir')}, "
                    f"notable_children={json.dumps(item.get('notable_children') or [], ensure_ascii=True)}"
                )
            tree_lines = item.get("tree_lines") or []
            if tree_lines:
                lines.append("  Tree preview:")
                for tree_line in tree_lines[:40]:
                    lines.append(f"    {tree_line}")
            key_roots = item.get("key_roots") or []
            if key_roots:
                lines.append("  Key roots:")
                for root_item in key_roots[:8]:
                    if not isinstance(root_item, dict):
                        continue
                    path = str(root_item.get("path") or "").strip()
                    source_class = str(root_item.get("source_class") or "").strip()
                    highlights = root_item.get("highlights") or []
                    label = f"{path} [{source_class}]" if source_class else path
                    lines.append(f"    - {label}")
                    for highlight in highlights[:4]:
                        lines.append(f"      - {highlight}")
            entrypoints = item.get("entrypoints") or []
            if entrypoints:
                lines.append("  Entrypoints by subsystem:")
                for entry in entrypoints[:8]:
                    if not isinstance(entry, dict):
                        continue
                    subsystem = str(entry.get("subsystem") or "").strip() or "general"
                    path = str(entry.get("path") or "").strip()
                    source_class = str(entry.get("source_class") or "").strip()
                    if path:
                        detail = f"{subsystem}: {path}"
                        if source_class:
                            detail += f" [{source_class}]"
                        lines.append(f"    - {detail}")
            secondary_context = item.get("secondary_context") or []
            if secondary_context:
                lines.append("  Secondary context:")
                for entry in secondary_context[:8]:
                    if not isinstance(entry, dict):
                        continue
                    path = str(entry.get("path") or "").strip()
                    note = str(entry.get("note") or "").strip()
                    source_class = str(entry.get("source_class") or "").strip()
                    detail = path
                    if source_class:
                        detail += f" [{source_class}]"
                    if note:
                        detail += f" — {note}"
                    lines.append(f"    - {detail}")
            marker_hits = item.get("marker_hits") or []
            if marker_hits:
                lines.append("  Open markers:")
                for marker in marker_hits[:8]:
                    if not isinstance(marker, dict):
                        continue
                    marker_name = str(marker.get("marker") or "").strip()
                    path = str(marker.get("path") or "").strip()
                    line = marker.get("line")
                    snippet = str(marker.get("snippet") or "").strip()
                    lines.append(f"    - {marker_name} {path}:{line} — {snippet[:120]}")
            noise_areas = item.get("noise_areas") or []
            if noise_areas:
                lines.append("  Noise / ignore areas:")
                for entry in noise_areas[:8]:
                    if isinstance(entry, dict):
                        path = str(entry.get("path") or "").strip()
                        note = str(entry.get("note") or "").strip()
                        source_class = str(entry.get("source_class") or "").strip()
                        detail = path
                        if source_class:
                            detail += f" [{source_class}]"
                        if note:
                            detail += f" — {note}"
                        lines.append(f"    - {detail}")
                    else:
                        lines.append(f"    - {entry}")
            preview_file = str(item.get("preview_file") or "").strip()
            preview_text = str(item.get("preview_text") or "").strip()
            if preview_file and preview_text:
                lines.append(f"  Preview from {preview_file}:")
                lines.append(preview_text[:1200])
    return "\n".join(lines)


def _normalize_path_for_match(value: str) -> str:
    return re.sub(r"/+", "/", str(value or "").strip().replace("\\", "/")).rstrip("/")


def _path_parts(value: str) -> tuple[str, ...]:
    normalized = _normalize_path_for_match(value)
    if not normalized:
        return ()
    return tuple(part for part in normalized.split("/") if part)


def _path_matches_configured_output_path(candidate: str, configured_output_path: str) -> bool:
    candidate_parts = _path_parts(candidate)
    configured_parts = _path_parts(configured_output_path)
    if not candidate_parts or not configured_parts:
        return False
    if candidate_parts == configured_parts:
        return True
    if len(candidate_parts) < len(configured_parts):
        return False
    return candidate_parts[-len(configured_parts):] == configured_parts


def _text_mentions_configured_output_path(text: str, configured_output_path: str) -> bool:
    for token in re.findall(r"[A-Za-z0-9_./\\\\-]+", str(text or "")):
        if _path_matches_configured_output_path(token, configured_output_path):
            return True
    return False


def _text_mentions_path(text: str, path: str) -> bool:
    normalized_text = _normalize_path_for_match(text).lower()
    normalized_path = _normalize_path_for_match(path).lower()
    if not normalized_path:
        return False
    return normalized_path in normalized_text


def _repo_map_written_paths(tool_records: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    for record in tool_records:
        if not isinstance(record, dict):
            continue
        if str(record.get("tool") or "") not in {"write_file", "append_file"}:
            continue
        args = record.get("arguments") if isinstance(record.get("arguments"), dict) else {}
        path = str(args.get("path") or "").strip()
        if path:
            paths.append(path)
    return paths


def _repo_map_failed_host_root_probe(
    tool_records: list[dict[str, Any]],
    contract: dict[str, Any] | None,
) -> bool:
    if not contract:
        return False
    inspected_roots = {
        _normalize_path_for_match(str(item.get("resolved_root") or item.get("configured_root") or ""))
        for item in (contract.get("root_inspections") or [])
        if isinstance(item, dict) and (item.get("tree_lines") or item.get("preview_text") or item.get("notable_children"))
    }
    inspected_roots.discard("")
    if not inspected_roots:
        return False

    for record in tool_records:
        if not isinstance(record, dict):
            continue
        tool_name = str(record.get("tool") or "").strip()
        if tool_name not in {"list_directory", "read_file"}:
            continue
        args = record.get("arguments") if isinstance(record.get("arguments"), dict) else {}
        path = _normalize_path_for_match(str(args.get("path") or ""))
        if not path or path not in inspected_roots:
            continue
        result_summary = str(record.get("result_summary") or "").lower()
        if "within the user's workspace" in result_summary or "must stay within the user's workspace" in result_summary:
            return True
    return False


def _repo_map_fallback_markers(text: str, configured_roots: list[str], configured_output_path: str) -> list[str]:
    lowered = text.lower()
    markers: list[str] = []

    generic_patterns = {
        "workspace_access_limitation": r"workspace path access limitation|workspace access restriction|path access limitation",
        "sandbox_limitation": r"sandbox(?:ed)? file tools require paths inside the current assistant workspace|outside that sandbox",
        "did_not_write_report": r"did not write (?:the )?report file to disk|did not update/write",
        "manual_paste_request": r"paste (?:the )?full report here|tell me how you want me to save the report|you can save it",
        "grant_workspace_access": r"grant or map .+ into this workspace|enable access to .+ from this workspace",
    }
    for name, pattern in generic_patterns.items():
        if re.search(pattern, lowered):
            markers.append(name)

    for root in configured_roots:
        normalized_root = _normalize_path_for_match(root).lower()
        if normalized_root and normalized_root in lowered and re.search(r"could not access|can't access|inaccessible|outside the sandbox", lowered):
            markers.append("configured_root_claimed_inaccessible")
            break

    if configured_output_path and _text_mentions_configured_output_path(text, configured_output_path):
        if re.search(r"did not write|not write|not updated|paste it here|save it elsewhere", lowered):
            markers.append("configured_output_not_written_fallback")

    return sorted(set(markers))


def _repo_map_evidence_markers(text: str) -> list[str]:
    lowered = text.lower()
    markers: list[str] = []
    if re.search(r"(^|\n)\s*observed(?: facts)?\s*:", lowered):
        markers.append("observed")
    if re.search(r"(^|\n)\s*secondary context\s*:", lowered):
        markers.append("secondary_context")
    if re.search(r"(^|\n)\s*suggested follow-up\s*:", lowered) or re.search(r"(^|\n)\s*suggested next step(?:s)?\s*:", lowered):
        markers.append("suggested_follow_up")
    return sorted(set(markers))


def _repo_map_worktree_mislabeling(text: str) -> dict[str, bool]:
    lowered = text.lower()
    lines = [line.strip().lower() for line in text.splitlines() if line.strip()]
    canonical_section_prefixes = (
        "repo overview:",
        "observed:",
        "observed facts:",
        "key roots:",
        "entrypoints by subsystem:",
        "tests and validation:",
    )
    presents_worktree_as_canonical = any(
        ".claude/worktrees" in line
        and line.startswith(canonical_section_prefixes)
        and "worktree-local" not in line
        and "secondary" not in line
        for line in lines
    )
    top_level_signals = sum(
        1 for token in ("app/", "tests/", "config/", "scripts/", "docs/") if token in lowered
    )
    worktree_mentions = len(re.findall(r"\.claude(?:/|\\)|worktree", lowered))
    return {
        "presents_worktree_as_canonical": presents_worktree_as_canonical,
        "over_indexes_worktree": bool(worktree_mentions >= 3 and top_level_signals < 2),
    }


def _contradicted_existing_repo_children(text: str, contract: dict[str, Any] | None) -> list[str]:
    if not contract:
        return []
    segments = [
        segment.strip().lower()
        for segment in re.split(r"[\n\r;]+|(?<=[.!?])\s+", text)
        if segment.strip()
    ]
    contradictions: list[str] = []
    missing_terms = ("missing", "not found", "absent", "does not exist", "unavailable")
    for inspection in contract.get("root_inspections") or []:
        if not isinstance(inspection, dict) or not inspection.get("exists"):
            continue
        root = str(inspection.get("configured_root") or "")
        for child in inspection.get("notable_children") or []:
            child_text = str(child or "").strip()
            if not child_text:
                continue
            patterns = (
                f"{child_text.lower()}/",
                f"`{child_text.lower()}`",
                f"{root.lower().rstrip('/')}/{child_text.lower()}",
            )
            if any(
                any(pattern in segment for pattern in patterns) and any(term in segment for term in missing_terms)
                for segment in segments
            ):
                contradictions.append(f"{root}/{child_text}")
    return sorted(set(contradictions))


def _validate_repo_map_output(
    *,
    result: str,
    repo_map_contract: dict[str, Any] | None = None,
    tool_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    text = str(result or "").strip()
    lowered = text.lower()
    tool_records = list(tool_records or [])
    configured_roots = list((repo_map_contract or {}).get("included_roots") or [])
    configured_output_path = str((repo_map_contract or {}).get("output_path") or "").strip()
    written_paths = _repo_map_written_paths(tool_records)
    failed_host_root_probe = _repo_map_failed_host_root_probe(tool_records, repo_map_contract)
    fallback_markers = _repo_map_fallback_markers(text, configured_roots, configured_output_path)
    evidence_markers = _repo_map_evidence_markers(text)
    worktree_flags = _repo_map_worktree_mislabeling(text)
    missing_configured_roots = [
        root for root in configured_roots if not _text_mentions_path(text, root)
    ]
    output_path_mismatch = False
    if configured_output_path:
        output_path_mismatch = not _text_mentions_configured_output_path(text, configured_output_path)
        if written_paths:
            output_path_mismatch = output_path_mismatch or any(
                not _path_matches_configured_output_path(path, configured_output_path)
                for path in written_paths
            )
        if not written_paths:
            output_path_mismatch = True
    synthetic_artifact_path_claimed = bool(
        re.search(r"\bartifacts/primary_repo_map_\d{4}-\d{2}-\d{2}\.md\b", lowered)
    )
    contradicted_existing_roots = _contradicted_existing_repo_children(text, repo_map_contract)
    contract_markers = {
        "repo overview": "repo overview" in lowered or "overview" in lowered,
        "observed": "observed" in lowered,
        "secondary context": "secondary context" in lowered,
        "suggested follow-up": "suggested follow-up" in lowered or "suggested next step" in lowered or "suggested next steps" in lowered,
        "key roots": "key roots" in lowered,
        "entrypoints by subsystem": "entrypoints by subsystem" in lowered,
        "tests and validation": "tests and validation" in lowered,
        "open markers": "open markers" in lowered,
        "noise / ignore areas": "noise / ignore areas" in lowered or "noise and ignore areas" in lowered,
        "roots checked": "roots checked" in lowered,
        "artifacts updated": "artifacts updated" in lowered,
        "notable changes": "notable changes" in lowered,
        "workspace orientation": "workspace orientation" in lowered,
        "repo map": "repo map" in lowered or "repository map" in lowered,
        "entrypoints": "entrypoint" in lowered or "entrypoints" in lowered,
        "repo paths": bool(re.search(r"\b(app|config|docs|tests|scripts)/[a-z0-9_./-]+", lowered)),
    }
    incompatible_markers = [
        marker
        for marker in (
            "draft ready",
            "saved maintenance task",
            "refresh rss cache",
            "task draft",
            "news briefing",
            "rss briefing",
            "headline roundup",
        )
        if marker in lowered
    ]
    contract_hits = [name for name, matched in contract_markers.items() if matched]
    navigation_hits = [
        name
        for name in (
            "repo overview",
            "observed",
            "secondary context",
            "suggested follow-up",
            "key roots",
            "entrypoints by subsystem",
            "tests and validation",
            "open markers",
            "noise / ignore areas",
        )
        if contract_markers.get(name)
    ]
    fatal_reason = ""
    if not text:
        fatal_reason = "Repo map manager returned empty output."
    elif incompatible_markers:
        fatal_reason = (
            "Repo map manager returned incompatible task/RSS/news content instead of repo-map output."
        )
    elif fallback_markers:
        fatal_reason = (
            "Repo map manager fell back to workspace/sandbox limitation narration instead of using the approved repo-map contract."
        )
    elif failed_host_root_probe:
        fatal_reason = (
            "Repo map manager attempted generic workspace filesystem tools against an approved host root."
        )
    elif missing_configured_roots:
        fatal_reason = "Repo map manager output did not report the configured included_roots."
    elif output_path_mismatch:
        fatal_reason = "Repo map manager output did not honor the configured output_path."
    elif synthetic_artifact_path_claimed:
        fatal_reason = "Repo map manager output claimed an unconfigured synthetic artifact path."
    elif contradicted_existing_roots:
        fatal_reason = "Repo map manager output contradicted actual configured root contents."
    elif worktree_flags["presents_worktree_as_canonical"]:
        fatal_reason = "Repo map manager output presented worktree-local paths as canonical repo structure without labeling."
    elif worktree_flags["over_indexes_worktree"]:
        fatal_reason = "Repo map manager output over-indexed worktree context instead of the configured top-level repo."
    elif repo_map_contract and any(
        (item.get("key_roots") or item.get("entrypoints") or item.get("secondary_context") or item.get("marker_hits") or item.get("noise_areas"))
        for item in (repo_map_contract.get("root_inspections") or [])
        if isinstance(item, dict)
    ) and "observed" not in evidence_markers:
        fatal_reason = "Repo map manager output did not clearly separate observed facts from interpretation."
    elif repo_map_contract and any(
        (item.get("key_roots") or item.get("entrypoints") or item.get("secondary_context") or item.get("marker_hits") or item.get("noise_areas"))
        for item in (repo_map_contract.get("root_inspections") or [])
        if isinstance(item, dict)
    ) and not navigation_hits:
        fatal_reason = "Repo map manager output did not present the richer navigation-oriented repo-map sections."
    elif len(text) < 120 or len(contract_hits) < 3:
        fatal_reason = (
            "Repo map manager output did not satisfy the repo-map/workspace-orientation contract."
        )

    return {
        "fatal": bool(fatal_reason),
        "fatal_reason": fatal_reason,
        "validation_mode": "repo_map_manager_semantic_contract",
        "agent_role": "repo_map_manager",
        "contract_hits": contract_hits,
        "navigation_hits": navigation_hits,
        "incompatible_markers": incompatible_markers,
        "configured_roots": configured_roots,
        "missing_configured_roots": missing_configured_roots,
        "configured_output_path": configured_output_path,
        "written_paths": written_paths,
        "failed_host_root_probe": failed_host_root_probe,
        "fallback_markers": fallback_markers,
        "evidence_markers": evidence_markers,
        "worktree_flags": worktree_flags,
        "output_path_mismatch": output_path_mismatch,
        "synthetic_artifact_path_claimed": synthetic_artifact_path_claimed,
        "contradicted_existing_roots": contradicted_existing_roots,
        "output_length": len(text),
    }
