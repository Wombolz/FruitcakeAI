from __future__ import annotations

from app.memory.graph_import import build_graph_import_plan


def test_build_graph_import_plan_extracts_family_entities_relations_and_observations():
    plan = build_graph_import_plan(
        [
            {
                "id": 1,
                "memory_type": "semantic",
                "content": "The user's full name is John Jeremiah Womble and he goes by Jeremiah.",
                "is_active": True,
            },
            {
                "id": 2,
                "memory_type": "semantic",
                "content": "Jeremiah's partner is Amy Jean Goshorn.",
                "is_active": True,
            },
            {
                "id": 3,
                "memory_type": "semantic",
                "content": "John Jeremiah Womble was born on March 1, 1981. He has two daughters named Auri Louise Womble and Evie Ann Womble, both born on April 20, 2017.",
                "is_active": True,
            },
        ],
        user_id=1,
    )

    assert "John Jeremiah Womble" in plan.entities
    assert "Amy Jean Goshorn" in plan.entities
    assert "Auri Louise Womble" in plan.entities
    assert "Evie Ann Womble" in plan.entities
    assert "Jeremiah" in plan.entities["John Jeremiah Womble"].aliases

    relation_types = {(item.from_entity_name, item.to_entity_name, item.relation_type) for item in plan.relations}
    assert ("John Jeremiah Womble", "Amy Jean Goshorn", "partner_of") in relation_types
    assert ("John Jeremiah Womble", "Auri Louise Womble", "parent_of") in relation_types
    assert ("John Jeremiah Womble", "Evie Ann Womble", "parent_of") in relation_types

    observation_targets = {(item.entity_name, item.source_memory_id) for item in plan.observations}
    assert ("John Jeremiah Womble", 1) in observation_targets
    assert ("Auri Louise Womble", 3) in observation_targets
    assert ("Evie Ann Womble", 3) in observation_targets


def test_build_graph_import_plan_skips_nonsemantic_and_inactive_memories():
    plan = build_graph_import_plan(
        [
            {
                "id": 10,
                "memory_type": "procedural",
                "content": "Respond in English.",
                "is_active": True,
            },
            {
                "id": 11,
                "memory_type": "semantic",
                "content": "Jeremiah asked for a haiku about recent headlines.",
                "is_active": False,
            },
            {
                "id": 12,
                "memory_type": "semantic",
                "content": "Unstructured note with no supported pattern.",
                "is_active": True,
            },
        ],
        user_id=1,
    )

    assert plan.entities == {}
    assert plan.relations == []
    assert plan.observations == []
    assert plan.skipped_memories == [12]
