from app.agent.chat_intents import (
    is_library_detail_or_excerpt_intent,
    is_library_lookup_intent,
)


def test_detects_library_lookup_intent():
    assert is_library_lookup_intent("Can you list the documents in my library?")
    assert is_library_lookup_intent("show uploaded files in the docs library")


def test_ignores_non_library_requests():
    assert not is_library_lookup_intent("check weather.com for statesboro weather")
    assert not is_library_lookup_intent("summarize the latest headlines with links")


def test_detects_library_detail_or_excerpt_intent():
    assert is_library_detail_or_excerpt_intent("show document details for my library file")
    assert is_library_detail_or_excerpt_intent("give excerpts from the uploaded document")


def test_ignores_non_library_detail_or_excerpt():
    assert not is_library_detail_or_excerpt_intent("what's the weather tonight")
