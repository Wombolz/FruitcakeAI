from app.agent.chat_orchestration import build_orchestrated_chat_history


def test_orchestration_overlay_inserts_before_latest_user_message():
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "research this topic and compare sources"},
    ]
    out = build_orchestrated_chat_history(history, enabled=True, max_steps=3)
    assert len(out) == len(history) + 1
    assert out[-2]["role"] == "system"
    assert "micro-plan" in out[-2]["content"].lower()
    assert out[-1]["role"] == "user"
    assert out[-1]["content"] == history[-1]["content"]


def test_orchestration_overlay_disabled_returns_copy_without_injection():
    history = [{"role": "user", "content": "simple question"}]
    out = build_orchestrated_chat_history(history, enabled=False, max_steps=3)
    assert out == history
    assert out is not history


def test_orchestration_step_count_is_clamped():
    history = [{"role": "user", "content": "complex ask"}]
    low = build_orchestrated_chat_history(history, enabled=True, max_steps=1)
    high = build_orchestrated_chat_history(history, enabled=True, max_steps=10)
    assert "2 steps maximum" in low[-2]["content"]
    assert "3 steps maximum" in high[-2]["content"]
