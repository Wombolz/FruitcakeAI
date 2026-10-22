"""Bundle preview/install and scope-checked document reads."""
from __future__ import annotations

import json
import posixpath

from sqlalchemy import and_, or_, select

from app.config import settings
from app.db.models import Skill
from app.skills.bundles import bundle_hash, validate_files
from app.skills.service import SkillValidationError, get_skill_service


async def preview_bundle(files, *, user_context, scope="shared", pinned=False):
    if scope not in {"shared", "personal"}:
        raise SkillValidationError("scope must be shared or personal")
    try:
        files = validate_files(files)
    except ValueError as exc:
        raise SkillValidationError(str(exc)) from exc
    service = get_skill_service()
    preview = service.parse_markdown(files["SKILL.md"], personal_user_id=user_context.id)
    if len(preview.name) > 255 or len(preview.slug) > 100 or len(preview.description) > 4000:
        raise SkillValidationError("Skill name, slug or description is too long")
    preview.scope = scope
    preview.personal_user_id = user_context.id if scope == "personal" else None
    preview.is_pinned = pinned
    preview.bundle_files = files
    preview.preview_hash = bundle_hash(files, scope, preview.personal_user_id, pinned)
    missing = sorted(set(preview.allowed_tool_additions) - await service._known_tool_names(user_context))
    candidate = Skill(id=2**63 - 1, name=preview.name, slug=preview.slug,
                      system_prompt_addition=preview.system_prompt_addition,
                      bundle_files_json=json.dumps(files))
    estimated = service._estimate_tokens(service.render_skill_block(candidate))
    warnings = list(preview.validation_warnings)
    if estimated > min(settings.skills_bundle_max_tokens, settings.skills_bundle_total_max_tokens):
        warnings.append("Main instructions exceed the configured bundle prompt budget; shorten SKILL.md before importing")
    return preview, missing, estimated, warnings


async def read_skill_file(db, user_context, *, skill_id: int, path: str, offset: int = 0):
    if type(skill_id) is not int or skill_id not in user_context.active_skill_ids:
        raise PermissionError("Only bundles selected for this turn can be read")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a nonnegative character offset")
    if not isinstance(path, str) or path.startswith("/") or "\\" in path or ":" in path:
        raise ValueError("Use a relative document path inside the selected bundle")
    path = posixpath.normpath(path.split("#", 1)[0])
    if path == ".." or path.startswith("../"):
        raise ValueError("Document path escapes the selected bundle")
    skill = (await db.execute(select(Skill).where(
        Skill.id == skill_id, Skill.is_active.is_(True),
        or_(Skill.scope == "shared", and_(Skill.scope == "personal", Skill.personal_user_id == user_context.user_id)),
    ))).scalar_one_or_none()
    if skill is None or not skill.bundle_files_json:
        raise LookupError("Selected skill bundle is no longer available")
    files = json.loads(skill.bundle_files_json)
    if path not in files:
        raise LookupError("Document is not in this installed bundle")
    content = files[path]
    if offset > len(content):
        raise ValueError("offset exceeds the document length")
    # Plain-text tool rendering reserves a small header without JSON escaping
    # doubling quotes/newlines and pushing a page past ordinary history limits.
    page_chars = min(3000, max(1, max(400, settings.agent_tool_result_max_chars) - 256))
    end = min(len(content), offset + page_chars)
    return {
        "skill_id": skill.id, "path": path, "content": content[offset:end],
        "offset": offset, "next_offset": end if end < len(content) else None,
        "total_characters": len(content),
    }
