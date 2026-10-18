"""Guard image inputs and CI runtime pins independently of Docker availability."""

from pathlib import Path
import re

import pytest
import yaml


@pytest.mark.parametrize("filename", ["containers/postgres/Dockerfile", "mcp_shell_server/Dockerfile"])
def test_all_container_base_images_are_digest_pinned(filename):
    content = Path(filename).read_text()
    bases = re.findall(r"^FROM (\S+)", content, re.MULTILINE)
    assert bases
    assert all(re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", base) for base in bases)
    assert "ARG DEBIAN_SNAPSHOT=20261018T000000Z" in content


def test_postgres_gosu_is_rebuilt_from_verified_source_with_patched_go():
    content = Path("containers/postgres/Dockerfile").read_text()
    assert "golang:1.26.9-bookworm@sha256:" in content
    assert "6456aaa0f3c854d199d0f037f068eb97515b7513" in content
    assert "33d7537d588ea49458b9509bcf4554bdf5ceacc66da71e5caa1058ea3b689c3b" in content
    assert "sha256sum -c -" in content and "go mod verify" in content
    assert "CGO_ENABLED=0" in content and "gosu nobody id" in content


def test_shell_runtime_has_no_python_packaging_toolchain():
    content = Path("mcp_shell_server/Dockerfile").read_text()
    assert "pip uninstall --yes pip setuptools wheel packaging" in content
    assert "/usr/local/lib/python3.11/ensurepip" in content
    assert "assert not list(importlib.metadata.distributions())" in content
    assert "command -v timeout" in content


def test_os_refresh_fails_closed_and_preserves_package_signatures():
    content = Path("containers/refresh-debian.sh").read_text()
    assert "APT::Update::Error-Mode=any" in content
    assert "https://snapshot.debian.org/" in content
    assert "ca-certificates" in content
    assert "trusted=yes" not in content and "allow-unauthenticated" not in content
    assert "ca-certificates.crt" in Path("containers/postgres/Dockerfile").read_text()


def test_build_context_excludes_private_data_by_default():
    rules = [line.strip() for line in Path(".dockerignore").read_text().splitlines()
             if line.strip() and not line.startswith("#")]
    assert rules[0] == "**"
    assert {rule for rule in rules if rule.startswith("!")} == {
        "!mcp_shell_server/", "!mcp_shell_server/**", "!containers/", "!containers/**",
    }


@pytest.mark.parametrize("filename", ["backend-checks.yml", "gitleaks.yml"])
def test_ci_uses_reviewed_node24_actions_and_explicit_runner(filename):
    workflow = yaml.safe_load((Path(".github/workflows") / filename).read_text())
    expected = {
        "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
        "gitleaks/gitleaks-action": "e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e",
    }
    for job in workflow["jobs"].values():
        assert job["runs-on"] == "ubuntu-24.04"
        for step in job["steps"]:
            if "uses" in step:
                action, ref = step["uses"].split("@")
                assert ref == expected[action]
