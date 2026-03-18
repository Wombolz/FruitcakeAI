from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx
import structlog
import yaml
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Skill, User

log = structlog.get_logger(__name__)

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_WORD_RE = re.compile(r"[a-z0-9]+")


class SkillValidationError(ValueError):
    pass


class SkillConflictError(ValueError):
    pass


class SkillNotFoundError(ValueError):
    pass


@dataclass(slots=True)
class SkillPreview:
    slug: str
    name: str
    description: str
    system_prompt_addition: str
    allowed_tool_additions: list[str]
    scope: str
    personal_user_id: int | None
    source_url: str | None
    is_pinned: bool
    validation_warnings: list[str]
    preview_hash: str


@dataclass(slots=True)
class SkillInjectionDecision:
    skill_id: int
    slug: str
    name: str
    score: float
    included: bool
    reason: str
    rendered_text: str = ""
    estimated_tokens: int = 0
    allowed_tool_additions: list[str] | None = None
    selection_mode: str = "embedding"


async def _embed(text: str) -> list[float] | None:
    if not text.strip():
        return None
    try:
        from app.rag.service import get_rag_service

        svc = get_rag_service()
        embed_model = svc._index._embed_model if getattr(svc, "_loaded", False) else None
        if embed_model is None:
            return None
        return await embed_model.aget_text_embedding(text)
    except Exception:
        log.warning("skills.embed_failed", exc_info=True)
        return None


class SkillService:
    def _build_preview(
        self,
        *,
        name: str,
        slug: str | None,
        description: str,
        system_prompt_addition: str,
        allowed_tool_additions: Any,
        scope: str | None,
        personal_user_id: int | None,
        source_url: str | None,
        is_pinned: bool,
        shared_personal_user_id_policy: str = "clear",
    ) -> SkillPreview:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise SkillValidationError("name is required")

        normalized_slug = str(slug or self.slugify(normalized_name)).strip()
        if not _SLUG_RE.fullmatch(normalized_slug):
            raise SkillValidationError("slug must match [a-z0-9-]+")

        normalized_description = str(description or "").strip()
        if len(normalized_description) < 20:
            raise SkillValidationError("description must be at least 20 characters")

        normalized_body = str(system_prompt_addition or "").strip()
        if not normalized_body:
            raise SkillValidationError("system prompt body must not be empty")

        if allowed_tool_additions is None:
            allowed_tool_additions = []
        if not isinstance(allowed_tool_additions, list):
            raise SkillValidationError("required_tools must be a list")
        tool_names = [str(name).strip() for name in allowed_tool_additions if str(name).strip()]

        normalized_scope = str(scope or "shared").strip().lower()
        if normalized_scope not in {"shared", "personal"}:
            raise SkillValidationError("scope must be 'shared' or 'personal'")

        if normalized_scope == "personal":
            if personal_user_id is None:
                raise SkillValidationError("personal scope requires a personal_user_id")
        elif personal_user_id is not None:
            if shared_personal_user_id_policy == "reject":
                raise SkillValidationError("personal_user_id must be null for shared scope")
            personal_user_id = None

        warnings: list[str] = []
        if len(normalized_description) < 40:
            warnings.append("description is short; relevance matching may be weak")

        preview_core = {
            "slug": normalized_slug,
            "name": normalized_name,
            "description": normalized_description,
            "system_prompt_addition": normalized_body,
            "allowed_tool_additions": tool_names,
            "scope": normalized_scope,
            "personal_user_id": personal_user_id,
            "source_url": source_url,
            "is_pinned": bool(is_pinned),
        }
        preview_hash = self.build_preview_hash(preview_core)
        return SkillPreview(
            slug=normalized_slug,
            name=normalized_name,
            description=normalized_description,
            system_prompt_addition=normalized_body,
            allowed_tool_additions=tool_names,
            scope=normalized_scope,
            personal_user_id=personal_user_id,
            source_url=source_url,
            is_pinned=bool(is_pinned),
            validation_warnings=warnings,
            preview_hash=preview_hash,
        )

    async def fetch_preview_content(self, source_url: str) -> str:
        parsed = urlparse(source_url)
        if parsed.scheme != "https":
            raise SkillValidationError("source_url must use https")
        allowed = {d.lower() for d in (settings.skills_preview_allowed_domains or []) if d}
        host = (parsed.hostname or "").lower()
        if allowed and host not in allowed:
            raise SkillValidationError(f"source_url domain '{host}' is not in the allowlist")

        async with httpx.AsyncClient(timeout=settings.skills_preview_fetch_timeout_seconds) as client:
            response = await client.get(source_url, follow_redirects=True)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "text" not in content_type and "markdown" not in content_type and "yaml" not in content_type:
                raise SkillValidationError("source_url must return text content")
            body = response.text
            if len(body.encode("utf-8")) > settings.skills_preview_fetch_max_bytes:
                raise SkillValidationError("source_url content exceeds max preview size")
            return body

    def build_preview_hash(self, preview_data: dict[str, Any]) -> str:
        canonical = json.dumps(preview_data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def parse_markdown(
        self,
        content: str,
        *,
        source_url: str | None = None,
        personal_user_id: int | None = None,
    ) -> SkillPreview:
        match = _FRONTMATTER_RE.match(content.strip())
        if not match:
            raise SkillValidationError("SKILL.md must include YAML frontmatter delimited by ---")

        raw_meta, body = match.groups()
        meta = yaml.safe_load(raw_meta) or {}
        if not isinstance(meta, dict):
            raise SkillValidationError("frontmatter must parse to a mapping")

        allowed_tool_additions = meta.get("required_tools")
        if allowed_tool_additions is None:
            allowed_tool_additions = meta.get("allowed_tool_additions", [])
        return self._build_preview(
            name=meta.get("name") or "",
            slug=meta.get("slug"),
            description=meta.get("description") or "",
            system_prompt_addition=body,
            allowed_tool_additions=allowed_tool_additions,
            scope=meta.get("scope"),
            personal_user_id=personal_user_id,
            source_url=source_url,
            is_pinned=bool(meta.get("pinned") or meta.get("global_safe") or meta.get("empty_query_safe")),
            shared_personal_user_id_policy="clear",
        )

    async def validate_tool_names(self, tool_names: Iterable[str]) -> None:
        available = await self._known_tool_names()
        unknown = sorted({str(name) for name in tool_names if str(name)} - available)
        if unknown:
            raise SkillValidationError(f"unknown tool names: {', '.join(unknown)}")

    async def preview_from_request(
        self,
        *,
        content: str | None,
        source_url: str | None,
        personal_user_id: int | None,
    ) -> SkillPreview:
        if bool(content) == bool(source_url):
            raise SkillValidationError("provide exactly one of content or source_url")
        raw = content or await self.fetch_preview_content(str(source_url))
        preview = self.parse_markdown(raw, source_url=source_url, personal_user_id=personal_user_id)
        await self.validate_tool_names(preview.allowed_tool_additions)
        return preview

    async def install_preview(
        self,
        db: AsyncSession,
        *,
        preview: SkillPreview,
        installed_by: int,
    ) -> Skill:
        existing = (
            await db.execute(
                select(Skill).where(
                    Skill.slug == preview.slug,
                    Skill.personal_user_id == preview.personal_user_id,
                    Skill.is_active == True,
                ).order_by(Skill.installed_at.desc())
            )
        ).scalars().all()
        superseded = existing[0] if existing else None
        description_embedding = await _embed(preview.description)
        for row in existing:
            row.is_active = False
        skill = Skill(
            slug=preview.slug,
            name=preview.name,
            description=preview.description,
            system_prompt_addition=preview.system_prompt_addition,
            scope=preview.scope,
            personal_user_id=preview.personal_user_id,
            installed_by=installed_by,
            source_url=preview.source_url,
            content_hash=hashlib.sha256(preview.system_prompt_addition.encode("utf-8")).hexdigest(),
            is_active=True,
            is_pinned=preview.is_pinned,
            supersedes_skill_id=superseded.id if superseded is not None else None,
        )
        skill.allowed_tool_additions = preview.allowed_tool_additions
        skill.description_embedding_vector = description_embedding
        db.add(skill)
        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            raise SkillConflictError("active skill slug already exists for this scope") from exc
        await db.refresh(skill)
        return skill

    async def delete_skill(self, db: AsyncSession, *, skill_id: int) -> None:
        skill = await db.get(Skill, skill_id)
        if skill is None:
            raise SkillNotFoundError(f"skill {skill_id} not found")
        await db.delete(skill)
        await db.flush()

    async def preview_from_payload(self, payload: dict[str, Any]) -> SkillPreview:
        preview = self._build_preview(
            name=payload.get("name") or "",
            slug=payload.get("slug"),
            description=payload.get("description") or "",
            system_prompt_addition=payload.get("system_prompt_addition") or "",
            allowed_tool_additions=payload.get("allowed_tool_additions", []),
            scope=payload.get("scope"),
            personal_user_id=payload.get("personal_user_id"),
            source_url=payload.get("source_url"),
            is_pinned=bool(payload.get("is_pinned", False)),
            shared_personal_user_id_policy="reject",
        )
        await self.validate_tool_names(preview.allowed_tool_additions)
        return preview

    async def list_skills(self, db: AsyncSession) -> list[Skill]:
        result = await db.execute(select(Skill).order_by(Skill.slug, Skill.installed_at.desc()))
        return list(result.scalars().all())

    async def update_skill(
        self,
        db: AsyncSession,
        *,
        skill_id: int,
        is_active: bool | None = None,
        is_pinned: bool | None = None,
        scope: str | None = None,
        personal_user_id: int | None = None,
    ) -> Skill:
        skill = await db.get(Skill, skill_id)
        if skill is None:
            raise SkillNotFoundError(f"skill {skill_id} not found")
        if is_active is not None:
            skill.is_active = is_active
        if is_pinned is not None:
            skill.is_pinned = is_pinned
        if scope is not None:
            if scope not in {"shared", "personal"}:
                raise SkillValidationError("scope must be 'shared' or 'personal'")
            skill.scope = scope
            if scope == "shared":
                skill.personal_user_id = None
            else:
                if personal_user_id is None and skill.personal_user_id is None:
                    raise SkillValidationError("personal scope requires personal_user_id")
                if personal_user_id is not None:
                    skill.personal_user_id = personal_user_id
        elif personal_user_id is not None:
            if skill.scope != "personal":
                raise SkillValidationError("personal_user_id can only be set for personal skills")
            skill.personal_user_id = personal_user_id
        await db.flush()
        await db.refresh(skill)
        return skill

    async def get_active_for_context(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        query: str,
    ) -> tuple[list[SkillInjectionDecision], list[str], set[str]]:
        decisions = await self.explain_injection(db, user_id=user_id, query=query)
        selected = [d for d in decisions if d.included]
        prompt_blocks = [d.rendered_text for d in selected if d.rendered_text]
        tool_grants: set[str] = set()
        for d in selected:
            tool_grants.update(d.allowed_tool_additions or [])
        return selected, prompt_blocks, tool_grants

    async def explain_injection(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        query: str,
        skill_id: int | None = None,
    ) -> list[SkillInjectionDecision]:
        stmt = select(Skill).where(
            or_(Skill.scope == "shared", and_(Skill.scope == "personal", Skill.personal_user_id == user_id))
        ).order_by(Skill.is_pinned.desc(), Skill.slug.asc(), Skill.installed_at.desc())
        if skill_id is not None:
            stmt = stmt.where(Skill.id == skill_id)
        rows = await db.execute(stmt)
        skills = list(rows.scalars().all())
        if not skills:
            return []

        stripped_query = (query or "").strip()
        query_embedding = await _embed(stripped_query) if stripped_query else None
        selection_mode = "embedding" if query_embedding is not None else "pinned_only"
        ranked: list[SkillInjectionDecision] = []
        for skill in skills:
            score = await self._score_skill(skill, stripped_query, query_embedding)
            include = True
            reason = "relevant"
            if not skill.is_active:
                include = False
                reason = "inactive"
            elif not stripped_query:
                if skill.is_pinned:
                    score = max(score, 1.0)
                    reason = "pinned_empty_query"
                else:
                    include = False
                    reason = "empty_query_not_pinned"
            elif query_embedding is None:
                if skill.is_pinned:
                    score = max(score, 1.0)
                    reason = "pinned_only_fallback"
                else:
                    include = False
                    reason = "embedding_unavailable_not_pinned"
            elif score < settings.skills_similarity_threshold:
                include = False
                reason = "below_similarity_threshold"

            rendered = self.render_skill_block(skill) if include else ""
            ranked.append(
                SkillInjectionDecision(
                    skill_id=skill.id,
                    slug=skill.slug,
                    name=skill.name,
                    score=score,
                    included=include,
                    reason=reason,
                    rendered_text=rendered,
                    estimated_tokens=self._estimate_tokens(rendered),
                    allowed_tool_additions=skill.allowed_tool_additions,
                    selection_mode=selection_mode,
                )
            )

        ranked.sort(key=lambda item: (item.included, item.score, item.slug), reverse=True)
        return self._apply_budget(ranked)

    def render_skill_block(self, skill: Skill) -> str:
        header = f"Skill: {skill.name} ({skill.slug})"
        body = skill.system_prompt_addition.strip()
        return f"{header}\n{body}".strip()

    async def _score_skill(self, skill: Skill, query: str, query_embedding: list[float] | None) -> float:
        if not query:
            return 0.0
        skill_embedding = skill.description_embedding_vector
        if query_embedding is not None and skill_embedding:
            return self._cosine_similarity(query_embedding, skill_embedding)
        return 0.0

    def relevance_mode(self) -> str:
        try:
            from app.rag.service import get_rag_service

            svc = get_rag_service()
            embed_model = svc._index._embed_model if getattr(svc, "_loaded", False) else None
            return "embedding" if embed_model is not None else "pinned_only"
        except Exception:
            return "pinned_only"

    def _apply_budget(self, decisions: list[SkillInjectionDecision]) -> list[SkillInjectionDecision]:
        total_tokens = 0
        included = 0
        final: list[SkillInjectionDecision] = []
        for decision in decisions:
            if not decision.included:
                final.append(decision)
                continue
            if included >= settings.skills_max_injected:
                decision.included = False
                decision.reason = "max_skills_budget"
                decision.rendered_text = ""
                decision.estimated_tokens = 0
                final.append(decision)
                continue
            rendered = self._truncate_to_token_budget(decision.rendered_text, settings.skills_max_tokens_per_skill)
            estimated = self._estimate_tokens(rendered)
            if total_tokens + estimated > settings.skills_total_max_tokens:
                decision.included = False
                decision.reason = "prompt_budget_exceeded"
                decision.rendered_text = ""
                decision.estimated_tokens = 0
                final.append(decision)
                continue
            decision.rendered_text = rendered
            decision.estimated_tokens = estimated
            total_tokens += estimated
            included += 1
            final.append(decision)
        return final

    def _truncate_to_token_budget(self, text: str, max_tokens: int) -> str:
        if self._estimate_tokens(text) <= max_tokens:
            return text
        chars = max(32, max_tokens * 4)
        trimmed = text[:chars].rstrip()
        return trimmed + "\n[Skill content truncated for prompt budget]"

    def _estimate_tokens(self, text: str) -> int:
        return max(1, math.ceil(len(text) / 4)) if text else 0

    def slugify(self, value: str) -> str:
        words = _WORD_RE.findall((value or "").lower())
        return "-".join(words)

    async def _known_tool_names(self) -> set[str]:
        from app.agent.tools import TOOL_SCHEMAS
        from app.mcp.registry import get_mcp_registry

        names = {tool["function"]["name"] for tool in TOOL_SCHEMAS}
        registry = get_mcp_registry()
        if registry._is_ready:
            names.update(tool["function"]["name"] for tool in registry.get_tools_for_agent())
        return names

    def _lexical_score(self, query: str, haystack: str) -> float:
        q_terms = set(_WORD_RE.findall(query.lower()))
        h_terms = set(_WORD_RE.findall(haystack.lower()))
        if not q_terms or not h_terms:
            return 0.0
        overlap = len(q_terms & h_terms)
        if overlap == 0:
            return 0.0
        return overlap / max(len(q_terms), 1)

    def _cosine_similarity(self, left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right))
        left_mag = math.sqrt(sum(a * a for a in left))
        right_mag = math.sqrt(sum(b * b for b in right))
        if left_mag == 0 or right_mag == 0:
            return 0.0
        return dot / (left_mag * right_mag)


@lru_cache(maxsize=1)
def get_skill_service() -> SkillService:
    return SkillService()


async def hydrate_user_context(
    db: AsyncSession,
    user_context,
    *,
    query: str,
    allowed_tool_cap: set[str] | None = None,
):
    service = get_skill_service()
    decisions, prompt_blocks, tool_grants = await service.get_active_for_context(
        db,
        user_id=user_context.user_id,
        query=query,
    )
    effective_cap = set(allowed_tool_cap or user_context.allowed_tool_cap or [])
    effective_grants = set(tool_grants) - set(user_context.blocked_tools or [])
    if effective_cap:
        effective_grants &= effective_cap
    user_context.skill_prompt_additions = prompt_blocks
    user_context.skill_granted_tools = sorted(effective_grants)
    user_context.active_skill_slugs = [decision.slug for decision in decisions if decision.included]
    user_context.skill_selection_mode = decisions[0].selection_mode if decisions else get_skill_service().relevance_mode()
    user_context.skill_injection_details = [
        {
            "skill_id": decision.skill_id,
            "slug": decision.slug,
            "score": decision.score,
            "included": decision.included,
            "reason": decision.reason,
            "selection_mode": decision.selection_mode,
        }
        for decision in decisions
    ]
    if allowed_tool_cap is not None:
        user_context.allowed_tool_cap = sorted(effective_cap)
    return user_context
