from app.autonomy.profiles import normalize_task_profile, resolve_task_profile_by_name


def test_normalize_task_profile_prefers_rss_newspaper():
    assert normalize_task_profile("rss_newspaper") == "rss_newspaper"
    assert normalize_task_profile("news_magazine") == "rss_newspaper"


def test_resolve_task_profile_aliases_to_rss_newspaper():
    assert resolve_task_profile_by_name("rss_newspaper").name == "rss_newspaper"
    assert resolve_task_profile_by_name("news_magazine").name == "rss_newspaper"
