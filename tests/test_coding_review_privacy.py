from types import SimpleNamespace

import pytest

from app import library_sources


def test_library_cannot_link_or_scan_private_review_evidence(tmp_path, monkeypatch):
    capture = tmp_path / "storage/.coding_review_captures/2/id"
    capture.mkdir(parents=True)
    manifest = capture / "manifest.json"
    manifest.write_text('{"private":true}')
    alias = tmp_path / "public-alias"
    alias.symlink_to(capture, target_is_directory=True)
    monkeypatch.setattr(library_sources, "settings", SimpleNamespace(linked_source_allowed_roots=str(tmp_path)))
    for path in (capture, manifest, alias, alias / "manifest.json"):
        with pytest.raises(ValueError, match="Private coding review"):
            library_sources._ensure_path_within_allowed_roots(path)
        assert library_sources._is_sensitive_file(path)
        assert library_sources._should_skip_dir(path)
    extractor = SimpleNamespace(supports=lambda path: True)
    assert library_sources._iter_supported_files(tmp_path / "storage", extractor, []) == []
    assert library_sources._iter_supported_files(capture, extractor, []) == []
