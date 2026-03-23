from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Memory, MemoryEntity, MemoryObservation, MemoryRelation
from app.memory.graph_service import GraphMemoryService


@dataclass
class GraphEntitySeed:
    name: str
    entity_type: str
    aliases: set[str] = field(default_factory=set)
    confidence: float = 0.8


@dataclass
class GraphObservationSeed:
    entity_name: str
    content: str | None
    source_memory_id: int
    confidence: float = 0.75
    observed_at: datetime | None = None


@dataclass
class GraphRelationSeed:
    from_entity_name: str
    to_entity_name: str
    relation_type: str
    source_memory_id: int
    confidence: float = 0.8


@dataclass
class GraphImportPlan:
    user_id: int
    entities: dict[str, GraphEntitySeed] = field(default_factory=dict)
    observations: list[GraphObservationSeed] = field(default_factory=list)
    relations: list[GraphRelationSeed] = field(default_factory=list)
    skipped_memories: list[int] = field(default_factory=list)


def _normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _is_pronoun(value: str) -> bool:
    return _normalize_name(value).lower() in {"he", "she", "they", "him", "her", "them"}


def _parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.strptime(text.strip(), "%B %d, %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _add_entity(plan: GraphImportPlan, name: str, entity_type: str, aliases: list[str] | None = None, confidence: float = 0.8) -> None:
    canonical = _normalize_name(name)
    if not canonical:
        return
    existing = plan.entities.get(canonical)
    if existing is None:
        plan.entities[canonical] = GraphEntitySeed(
            name=canonical,
            entity_type=entity_type,
            aliases={
                _normalize_name(alias)
                for alias in (aliases or [])
                if _normalize_name(alias) and not _is_pronoun(alias)
            },
            confidence=confidence,
        )
        return
    existing.confidence = max(existing.confidence, confidence)
    if entity_type != "unknown" and existing.entity_type == "unknown":
        existing.entity_type = entity_type
    for alias in aliases or []:
        alias_norm = _normalize_name(alias)
        if alias_norm and not _is_pronoun(alias_norm):
            existing.aliases.add(alias_norm)


def _add_observation(
    plan: GraphImportPlan,
    entity_name: str,
    content: str | None,
    source_memory_id: int,
    confidence: float = 0.75,
    observed_at: datetime | None = None,
) -> None:
    plan.observations.append(
        GraphObservationSeed(
            entity_name=_normalize_name(entity_name),
            content=(content or "").strip() or None,
            source_memory_id=source_memory_id,
            confidence=confidence,
            observed_at=observed_at,
        )
    )


def _add_relation(
    plan: GraphImportPlan,
    from_entity_name: str,
    to_entity_name: str,
    relation_type: str,
    source_memory_id: int,
    confidence: float = 0.8,
) -> None:
    plan.relations.append(
        GraphRelationSeed(
            from_entity_name=_normalize_name(from_entity_name),
            to_entity_name=_normalize_name(to_entity_name),
            relation_type=relation_type,
            source_memory_id=source_memory_id,
            confidence=confidence,
        )
    )


def build_graph_import_plan(memory_rows: list[dict[str, Any]], user_id: int) -> GraphImportPlan:
    plan = GraphImportPlan(user_id=user_id)
    alias_map: dict[str, str] = {}

    active_semantic = [
        row for row in memory_rows
        if row.get("is_active") is True and row.get("memory_type") == "semantic"
    ]
    active_semantic.sort(key=lambda row: (row.get("created_at") or "", int(row.get("id", 0))))

    for row in active_semantic:
        memory_id = int(row["id"])
        content = str(row.get("content", "")).strip()
        matched = False
        last_person_subject: str | None = None

        full_name = re.search(
            r"The user's full name is (?P<full>[A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+)+) and he goes by (?P<alias>[A-Z][A-Za-z]+)\.",
            content,
        )
        if full_name:
            canonical = _normalize_name(full_name.group("full"))
            alias = _normalize_name(full_name.group("alias"))
            _add_entity(plan, canonical, "person", aliases=[alias], confidence=0.98)
            alias_map[alias] = canonical
            _add_observation(plan, canonical, f"Goes by {alias}.", memory_id, confidence=0.98)
            matched = True

        birth = re.search(
            r"(?P<name>[A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){0,3}) was born on (?P<date>[A-Z][a-z]+ \d{1,2}, \d{4})\.",
            content,
        )
        if birth:
            person = _normalize_name(birth.group("name"))
            canonical = alias_map.get(person, person)
            _add_entity(plan, canonical, "person", aliases=[person] if canonical != person else [], confidence=0.96)
            last_person_subject = canonical
            _add_observation(
                plan,
                canonical,
                f"Born on {birth.group('date')}.",
                memory_id,
                confidence=0.96,
                observed_at=_parse_date(birth.group("date")),
            )
            matched = True

        partner = re.search(
            r"(?P<left>[A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){0,2})'s partner is (?P<right>[A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){1,3})\.",
            content,
        )
        if partner:
            left_raw = _normalize_name(partner.group("left"))
            left = alias_map.get(left_raw, left_raw)
            right = _normalize_name(partner.group("right"))
            _add_entity(plan, left, "person", aliases=[left_raw] if left != left_raw else [], confidence=0.95)
            _add_entity(plan, right, "person", confidence=0.95)
            _add_relation(plan, left, right, "partner_of", memory_id, confidence=0.95)
            matched = True

        daughters = re.search(
            r"(?P<parent>[A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){0,3}) has two daughters named (?P<c1>[A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){1,3}) and (?P<c2>[A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){1,3}), both born on (?P<date>[A-Z][a-z]+ \d{1,2}, \d{4})\.",
            content,
        )
        if daughters:
            parent_raw = _normalize_name(daughters.group("parent"))
            parent = last_person_subject if parent_raw in {"He", "She"} and last_person_subject else alias_map.get(parent_raw, parent_raw)
            child_names = [_normalize_name(daughters.group("c1")), _normalize_name(daughters.group("c2"))]
            aliases = [parent_raw] if parent != parent_raw and not _is_pronoun(parent_raw) else []
            _add_entity(plan, parent, "person", aliases=aliases, confidence=0.94)
            for child in child_names:
                _add_entity(plan, child, "person", confidence=0.94)
                _add_relation(plan, parent, child, "parent_of", memory_id, confidence=0.94)
                _add_observation(
                    plan,
                    child,
                    f"Born on {daughters.group('date')}.",
                    memory_id,
                    confidence=0.94,
                    observed_at=_parse_date(daughters.group("date")),
                )
            matched = True

        children = re.search(
            r"(?P<parent>[A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){1,3}) was born on (?P<pdate>[A-Z][a-z]+ \d{1,2}, \d{4}) and has two children: (?P<c1>[A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){1,3}) \(born (?P<d1>[A-Z][a-z]+ \d{1,2}, \d{4})\) and (?P<c2>[A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){1,3}) \(born (?P<d2>[A-Z][a-z]+ \d{1,2}, \d{4})\)\.",
            content,
        )
        if children:
            parent = _normalize_name(children.group("parent"))
            _add_entity(plan, parent, "person", confidence=0.95)
            _add_observation(
                plan,
                parent,
                f"Born on {children.group('pdate')}.",
                memory_id,
                confidence=0.95,
                observed_at=_parse_date(children.group("pdate")),
            )
            for child_key, date_key in [("c1", "d1"), ("c2", "d2")]:
                child = _normalize_name(children.group(child_key))
                _add_entity(plan, child, "person", confidence=0.93)
                _add_relation(plan, parent, child, "parent_of", memory_id, confidence=0.93)
                _add_observation(
                    plan,
                    child,
                    f"Born on {children.group(date_key)}.",
                    memory_id,
                    confidence=0.93,
                    observed_at=_parse_date(children.group(date_key)),
                )
            matched = True

        both_born = re.search(
            r"(?P<c1>[A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){1,3}) and (?P<c2>[A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){1,3}) were both born on (?P<date>[A-Z][a-z]+ \d{1,2}, \d{4})\.",
            content,
        )
        if both_born:
            for child_key in ("c1", "c2"):
                child = _normalize_name(both_born.group(child_key))
                _add_entity(plan, child, "person", confidence=0.9)
                _add_observation(
                    plan,
                    child,
                    f"Born on {both_born.group('date')}.",
                    memory_id,
                    confidence=0.9,
                    observed_at=_parse_date(both_born.group("date")),
                )
            matched = True

        lives_at = re.search(
            r"The family lives at (?P<address>.+)\.",
            content,
        )
        if lives_at:
            address = _normalize_name(lives_at.group("address"))
            _add_entity(plan, address, "place", confidence=0.85)
            _add_observation(plan, address, "Family residence.", memory_id, confidence=0.85)
            primary = alias_map.get("Jeremiah") or next(
                (seed.name for seed in plan.entities.values() if seed.entity_type == "person" and "Jeremiah" in seed.name),
                None,
            )
            if primary:
                _add_relation(plan, primary, address, "lives_at", memory_id, confidence=0.8)
            matched = True

        handbook = re.search(
            r"(?P<doc>Bulloch County Schools Student Handbook \(2025\)) outlines (?P<rest>.+)$",
            content,
        )
        if handbook:
            doc = _normalize_name(handbook.group("doc"))
            _add_entity(plan, doc, "document", confidence=0.75)
            _add_observation(plan, doc, content, memory_id, confidence=0.75)
            matched = True

        if not matched:
            plan.skipped_memories.append(memory_id)

    return plan


async def apply_graph_import_plan(db: AsyncSession, plan: GraphImportPlan) -> dict[str, int]:
    svc = GraphMemoryService()
    entity_ids: dict[str, int] = {}

    for seed in plan.entities.values():
        entity, _ = await svc.find_or_create_entity(
            db=db,
            user_id=plan.user_id,
            name=seed.name,
            entity_type=seed.entity_type,
            aliases=sorted(seed.aliases),
            confidence=seed.confidence,
        )
        entity_ids[seed.name] = entity.id

    created_observations = 0
    for seed in plan.observations:
        if seed.entity_name not in entity_ids:
            continue
        existing = await db.execute(
            select(MemoryObservation).where(
                and_(
                    MemoryObservation.user_id == plan.user_id,
                    MemoryObservation.entity_id == entity_ids[seed.entity_name],
                    MemoryObservation.source_memory_id == seed.source_memory_id,
                    MemoryObservation.is_active == True,
                )
            )
        )
        if existing.scalar_one_or_none() is not None:
            continue
        await svc.add_observation(
            db=db,
            user_id=plan.user_id,
            entity_id=entity_ids[seed.entity_name],
            content=seed.content,
            observed_at=seed.observed_at,
            confidence=seed.confidence,
            source_memory_id=seed.source_memory_id,
        )
        created_observations += 1

    created_relations = 0
    for seed in plan.relations:
        if seed.from_entity_name not in entity_ids or seed.to_entity_name not in entity_ids:
            continue
        existing = await db.execute(
            select(MemoryRelation).where(
                and_(
                    MemoryRelation.user_id == plan.user_id,
                    MemoryRelation.from_entity_id == entity_ids[seed.from_entity_name],
                    MemoryRelation.to_entity_id == entity_ids[seed.to_entity_name],
                    MemoryRelation.relation_type == seed.relation_type,
                    MemoryRelation.source_memory_id == seed.source_memory_id,
                )
            )
        )
        if existing.scalar_one_or_none() is not None:
            continue
        await svc.create_relation(
            db=db,
            user_id=plan.user_id,
            from_entity_id=entity_ids[seed.from_entity_name],
            to_entity_id=entity_ids[seed.to_entity_name],
            relation_type=seed.relation_type,
            confidence=seed.confidence,
            source_memory_id=seed.source_memory_id,
        )
        created_relations += 1

    return {
        "entities": len(entity_ids),
        "observations": created_observations,
        "relations": created_relations,
        "skipped_memories": len(plan.skipped_memories),
    }


async def build_plan_from_export(db: AsyncSession, export_path: str | Path, include_inactive: bool = False) -> GraphImportPlan:
    rows = json.loads(Path(export_path).read_text())
    if not isinstance(rows, list):
        raise ValueError("Export file must be a JSON list.")
    memory_ids = [int(row["id"]) for row in rows if row.get("id") is not None]
    if not memory_ids:
        raise ValueError("Export file contains no memory ids.")
    result = await db.execute(select(Memory).where(Memory.id.in_(memory_ids)))
    memories = list(result.scalars().all())
    if not memories:
        raise ValueError("No matching memories found in the current database.")
    user_ids = {memory.user_id for memory in memories}
    if len(user_ids) != 1:
        raise ValueError("Export memories belong to multiple users; import one user at a time.")
    if not include_inactive:
        rows = [row for row in rows if row.get("is_active") is True]
    return build_graph_import_plan(rows, user_id=int(next(iter(user_ids))))
