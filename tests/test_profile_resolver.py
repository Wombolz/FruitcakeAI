from app.autonomy.profiles import normalize_task_profile, resolve_task_profile_by_name


def test_normalize_task_profile_prefers_rss_newspaper():
    assert normalize_task_profile("rss_newspaper") == "rss_newspaper"
    assert normalize_task_profile("news_magazine") == "rss_newspaper"
    assert normalize_task_profile("morning_briefing") == "morning_briefing"
    assert normalize_task_profile("topic_watcher") == "topic_watcher"


def test_resolve_task_profile_aliases_to_rss_newspaper():
    assert resolve_task_profile_by_name("rss_newspaper").name == "rss_newspaper"
    assert resolve_task_profile_by_name("news_magazine").name == "rss_newspaper"
    assert resolve_task_profile_by_name("morning_briefing").name == "morning_briefing"
    assert resolve_task_profile_by_name("topic_watcher").name == "topic_watcher"
