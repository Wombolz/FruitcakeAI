from __future__ import annotations

from pathlib import Path

import pytest

from app.host_root_access import build_host_root_scan_summary, validate_host_root_candidate


def test_validate_host_root_candidate_rejects_broad_roots():
    with pytest.raises(ValueError, match="too broad"):
        validate_host_root_candidate("/")

    with pytest.raises(ValueError, match="too broad"):
        validate_host_root_candidate(str(Path.home()))


def test_build_host_root_scan_summary_skips_symlink_escape(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret", encoding="utf-8")
    (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
    (repo / "safe.txt").write_text("safe", encoding="utf-8")
    (repo / "escape").symlink_to(outside, target_is_directory=True)

    summary = build_host_root_scan_summary(repo)

    tree_lines = summary["tree_lines"]
    assert any(line.endswith("README.md") for line in tree_lines)
    assert all("escape" not in line for line in tree_lines)


def test_build_host_root_scan_summary_collects_navigation_data(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "app" / "agent").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "config").mkdir()
    (repo / "Docs").mkdir()
    (repo / "README.md").write_text("# FruitcakeAI\n", encoding="utf-8")
    (repo / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (repo / "app" / "agent" / "core.py").write_text("# TODO: improve repo map\n", encoding="utf-8")
    (repo / "tests" / "test_task_steps.py").write_text("assert True\n", encoding="utf-8")
    (repo / "config" / "agents.yaml").write_text("agents: []\n", encoding="utf-8")
    (repo / ".claude").mkdir()

    summary = build_host_root_scan_summary(repo)

    assert any(item["path"] == "app/" for item in summary["key_roots"])
    assert any(item["source_class"] == "observed_top_level" for item in summary["key_roots"])
    assert any(item["subsystem"] == "agent_runtime" for item in summary["entrypoints"])
    assert any(hit["marker"] == "TODO" for hit in summary["marker_hits"])
    assert any(item["path"] == ".claude/" for item in summary["secondary_context"])
    assert any(item["path"] == ".claude" for item in summary["noise_areas"])


def test_build_host_root_scan_summary_downranks_noise_roots_in_tree_preview(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".claude" / "worktrees" / "branch1").mkdir(parents=True)
    (repo / ".claude" / "worktrees" / "branch1" / "app").mkdir()
    (repo / "app").mkdir()
    (repo / "README.md").write_text("# Repo\n", encoding="utf-8")

    summary = build_host_root_scan_summary(repo)

    assert any(line == ".claude/" for line in summary["tree_lines"])
    assert all(".claude/worktrees/branch1/app/" not in line for line in summary["tree_lines"])


def test_build_host_root_scan_summary_respects_ignored_paths_for_markers(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "Docs").mkdir()
    (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
    (repo / "app" / "main.py").write_text("# TODO: visible marker\n", encoding="utf-8")
    (repo / "Docs" / "draft.md").write_text("FIXME: hidden marker\n", encoding="utf-8")

    summary = build_host_root_scan_summary(repo, ignored_paths=["Docs"])

    assert any(hit["path"] == "app/main.py" for hit in summary["marker_hits"])
    assert all(hit["path"] != "Docs/draft.md" for hit in summary["marker_hits"])
