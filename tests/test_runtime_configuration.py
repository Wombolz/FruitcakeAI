"""Keep local runtime upgrades explicit and database exposure loopback-only."""

from pathlib import Path
import re

import yaml


def test_database_image_uses_versioned_local_build_without_registry_fallback():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    database = compose["services"]["postgres"]
    assert re.fullmatch(r"fruitcake/postgres-vector:16\.\d+-pgvector0\.8\.\d+-\d{8}-r\d+", database["image"])
    assert database["pull_policy"] == "never"
    assert database["build"] == {"context": ".", "dockerfile": "containers/postgres/Dockerfile"}


def test_database_only_publishes_to_ipv4_loopback():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    assert compose["services"]["postgres"]["ports"] == ["127.0.0.1:5432:5432"]


def test_backend_interpreter_stays_on_tested_security_patch_series():
    assert re.fullmatch(r"3\.11\.\d+", Path(".python-version").read_text().strip())


def test_elgato_uses_tested_bridge_and_patched_sdk_without_install_hooks():
    config = yaml.safe_load(Path("config/mcp_config.yaml").read_text())
    bridge = config["mcp_servers"]["elgato"]
    assert bridge["type"] == "stdio"
    assert bridge["command"] == "npx"
    assert bridge["args"] == [
        "--yes",
        "--ignore-scripts",
        "--package=@elgato/mcp-server@0.1.7",
        "--package=@modelcontextprotocol/sdk@1.31.0",
        "elgato-mcp-server",
    ]
