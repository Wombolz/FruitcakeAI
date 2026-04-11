from app.agent.definition_loader import agent_definition_exists, get_agent_definition, list_agent_definitions


def test_agent_definitions_load_expected_builtins():
    defs = list_agent_definitions()
    assert "roadmap_verifier" in defs
    assert "runtime_inspector" in defs
    assert "document_sync_manager" in defs


def test_agent_definitions_support_legacy_alias_resolution():
    from app.agent.definition_loader import get_agent_definition

    legacy = get_agent_definition("mcp_tester")
    current = get_agent_definition("runtime_inspector")
    assert legacy is not None
    assert current is not None
    assert legacy.agent_type == "runtime_inspector"
    assert legacy.display_name == current.display_name


def test_agent_definition_exposes_expected_metadata():
    definition = get_agent_definition("document_sync_manager")
    assert definition is not None
    assert definition.execution_mode == "service"
    assert definition.background is True
    assert "Docs/_internal/MASTER_ROADMAP.md" in definition.required_context_sources
    assert "failed" in definition.output_contract
    assert agent_definition_exists("document_sync_manager") is True


def test_runtime_inspector_defaults_to_non_self_latest_behavior():
    definition = get_agent_definition("runtime_inspector")
    assert definition is not None
    joined = "\n".join(definition.behavior_instructions).lower()
    assert "most recent non-self task or run" in joined
    assert "fall back to the most recent prior relevant item" in joined
    assert "prefer run-level inspection surfaces" in joined
    assert "do not end with optional next-step menus" in joined
