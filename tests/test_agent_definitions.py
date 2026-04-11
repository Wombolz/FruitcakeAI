from app.agent.definition_loader import (
    agent_preset_exists,
    get_agent_category,
    get_agent_preset,
    list_agent_categories,
    list_agent_presets,
)


def test_agent_registry_loads_expected_categories_and_presets():
    categories = list_agent_categories()
    presets = list_agent_presets()
    assert "explore" in categories
    assert "verify" in categories
    assert "monitor" in categories
    assert "roadmap_verifier" in presets
    assert "runtime_inspector" in presets
    assert "document_sync_manager" in presets
    assert "repo_map_manager" in presets
    assert "recent_run_analyzer" in presets


def test_agent_preset_inherits_category_behavior():
    category = get_agent_category("verify")
    preset = get_agent_preset("runtime_inspector")
    assert category is not None
    assert preset is not None
    joined = "\n".join(preset.behavior_instructions).lower()
    assert "separate direct observation from inference" in joined
    assert any("evidence" in item.lower() for item in category.behavior_instructions)


def test_agent_preset_exposes_expected_metadata():
    preset = get_agent_preset("document_sync_manager")
    assert preset is not None
    assert preset.category_id == "monitor"
    assert preset.execution_mode == "service"
    assert preset.background is True
    assert "Docs/_internal/MASTER_ROADMAP.md" in preset.required_context_sources
    assert "failed" in preset.output_contract
    assert agent_preset_exists("document_sync_manager") is True


def test_runtime_inspector_defaults_to_non_self_latest_behavior():
    preset = get_agent_preset("runtime_inspector")
    assert preset is not None
    joined = "\n".join(preset.behavior_instructions).lower()
    assert "most recent non-self task or run" in joined
    assert "fall back to the most recent prior relevant item" in joined
    assert "prefer run-level inspection surfaces" in joined
    assert "do not end with optional next-step menus" in joined
