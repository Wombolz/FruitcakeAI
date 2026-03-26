from app.agent.chat_routing import classify_chat_complexity


def test_classify_simple_chat():
    d = classify_chat_complexity("What's the weather today?", threshold=3, routing_enabled=True)
    assert d.is_complex is False
    assert d.mode == "chat"
    assert d.score < 3


def test_classify_complex_chat_with_multi_signals():
    text = (
        "Please research the latest headlines, compare sources, and summarize in steps. "
        "Include citations and then suggest what to prioritize first."
    )
    d = classify_chat_complexity(text, threshold=3, routing_enabled=True)
    assert d.is_complex is True
    assert d.mode == "task"
    assert d.score >= 3
    assert "tool_heavy" in d.reasons or "multi_step_marker" in d.reasons


def test_routing_disabled_forces_simple_mode():
    text = (
        "I need a plan: research, compare options, summarize pros and cons, "
        "then produce a recommendation."
    )
    d = classify_chat_complexity(text, threshold=1, routing_enabled=False)
    assert d.is_complex is False
    assert d.mode == "chat"


def test_classify_location_lookup_request_as_complex():
    text = (
        "Add Zaxby's as well, then find the addresses for each location in "
        "Statesboro, GA and add the addresses as well."
    )
    d = classify_chat_complexity(text, threshold=3, routing_enabled=True)
    assert d.is_complex is True
    assert d.mode == "task"
    assert "location_lookup" in d.reasons
