# Sprint 7.3 — Graph Memory Foundation

**Status**: Planned  
**Phase**: 7 — Trusted Local Capability Expansion  
**Depends on**: existing flat memory system, current Postgres DB, current persona/tool filtering

---

## Summary

Add a Fruitcake-native graph memory layer for durable relationship structure without replacing the current flat memory system.

This sprint does **not** redesign the existing `memories` table or `MemoryService`. The current flat memory system remains the primary retrieval path for prompt context.

The graph layer exists to answer a different question:
- flat memory: what facts matter right now?
- graph memory: how do the things we know relate to each other?

First sprint goal:
- durable graph tables
- graph service layer
- additive tools/API
- provenance and guardrails

Not a goal for this sprint:
- automatic graph enrichment in every chat turn
- replacing memory retrieval tiers
- aggressive entity merging

---

## Locked Decisions

- Keep flat memory as the primary recall path.
- Add graph memory as a separate relational layer in the same Postgres DB.
- Keep graph memory user-scoped and auditable.
- Do not treat graph observations as a second free-floating fact store.
- When graph facts are derived from existing flat memories, reference the source memory instead of duplicating the fact content by default.
- Keep runtime graph enrichment out of baseline chat memory injection in this sprint.

---

## Current State

The current memory system is already real and should remain intact:
- `memories` is a flat per-user table with:
  - `semantic`
  - `procedural`
  - `episodic`
- `MemoryService.retrieve_for_context()` already provides:
  - standing memory recall
  - recent high-importance episodic recall
  - vector-similar episodic recall
- existing memory APIs support:
  - create
  - list
  - recall
  - importance/tags update
  - deactivate

This sprint is additive on top of that system.

---

## Data Model

### `memory_entities`
- `id`
- `user_id`
- `name`
- `entity_type`
- `aliases`
- `confidence`
- `is_active`
- `created_at`
- `updated_at`

### `memory_relations`
- `id`
- `user_id`
- `from_entity_id`
- `to_entity_id`
- `relation_type`
- `confidence`
- `source_memory_id`
- `source_session_id`
- `source_task_id`
- `created_at`

### `memory_observations`
- `id`
- `user_id`
- `entity_id`
- `content`
- `observed_at`
- `confidence`
- `source_memory_id`
- `source_session_id`
- `source_task_id`
- `created_at`

### Data model rules
- Every graph row is user-scoped.
- Relation endpoints must only connect entities owned by the same user.
- `source_memory_id` is used whenever the graph fact comes from an existing flat memory.
- Free-text observation content is allowed, but should be reserved for graph-native observations rather than becoming the default mirror of flat memory content.
- Conflicting observations may coexist; do not silently overwrite them.

---

## Service Design

Add a dedicated graph service under `app/memory/` rather than extending `MemoryService` directly.

Initial service surface:
- `find_entity(...)`
- `create_entity(...)`
- `find_or_create_entity(...)`
- `create_relation(...)`
- `add_observation(...)`
- `search_entities(...)`
- `open_entity_graph(...)`

### Normalization rules
- case-insensitive entity matching
- alias-aware search
- conservative matching only
- no aggressive auto-merge of ambiguous names in this sprint

### Boundary rule
- `MemoryService` continues to own flat memory retrieval and storage
- graph service owns entities, relations, and observations
- graph service may reference flat memories, but must not replace them

---

## Tool / API Direction

Additive graph interfaces:
- `create_memory_entities`
- `create_memory_relations`
- `add_memory_observations`
- `search_memory_graph`
- `open_memory_graph_nodes`

API direction:
- create entity
- create relation
- add observation
- search entities
- open entity neighborhood

Rules:
- graph tools stay explicit and persona-filterable
- graph tools are not auto-invoked by default chat behavior in this sprint
- current flat memory API remains unchanged

---

## Runtime Integration

Do **not** add graph context into baseline prompt assembly in this sprint.

The later intended integration model is:
1. flat memory retrieval runs first
2. graph enrichment optionally traverses entities mentioned in recalled memories
3. graph context is added only if useful and within budget

That later step stays deferred until:
- the graph is populated enough to matter
- token budget interaction is modeled
- graph behavior is validated in soak

---

## Guardrails

- no cross-user graph joins
- no silent conflict resolution
- no unbounded traversal
- no unlimited enrichment depth
- no mutation of current flat memory behavior

Initial traversal limit for later enrichment work:
- max 2 hops

---

## Acceptance Criteria

1. Graph tables exist and are migrated cleanly.
2. Graph service supports entity creation, relation creation, observation creation, search, and open-node inspection.
3. Graph rows are user-scoped and provenance-carrying.
4. Existing flat memory retrieval remains unchanged.
5. Graph tools/API are additive and explicit.
6. No automatic graph injection into chat memory context occurs in this sprint.

---

## Test Plan

### Service / model
- create entity with conservative normalization
- create relation between same-user entities
- reject relation across ownership boundaries
- add observation with `source_memory_id`
- search entities by name and alias
- open entity neighborhood returns entity + relations + observations

### Guardrails
- conflicting observations coexist
- graph rows do not overwrite one another
- cross-user access fails cleanly

### Regression
- existing `MemoryService.retrieve_for_context()` behavior is unchanged
- existing `/memories` CRUD/recall behavior is unchanged
- existing prompt memory injection behavior is unchanged

---

## Follow-on Work

Likely follow-ups after the foundation lands:
- graph admin inspector / diagnostics
- optional graph enrichment in context assembly
- confidence decay
- conflict review tooling
- dream-cycle graph-aware consolidation

Do not treat this sprint as permission to fold graph logic directly into every memory path before the base layer proves itself.
