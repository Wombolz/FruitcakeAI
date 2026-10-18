"""Keep generated install baselines complete and runtime/dev versions aligned."""

from pathlib import Path
from importlib.metadata import version
import hashlib
import re

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def _locked(filename):
    text = (ROOT / filename).read_text()
    requirements = []
    for entry in text.replace("\\\n", " ").splitlines():
        entry = entry.strip()
        if not entry or entry.startswith("#"):
            continue
        requirement, *hashes = entry.split(" --hash=")
        assert hashes, f"Unhashed requirement in {filename}: {entry}"
        assert all(re.fullmatch(r"sha256:[a-f0-9]{64}", h.strip()) for h in hashes)
        parsed = Requirement(requirement)
        assert len(parsed.specifier) == 1 and next(iter(parsed.specifier)).operator == "=="
        requirements.append(parsed)
    return requirements


def test_runtime_baseline_has_no_retired_auth_or_development_tools():
    names = {r.name.lower() for r in _locked("requirements.txt")}
    assert not names & {"python-jose", "passlib", "pytest", "pytest-asyncio", "pip-audit"}
    assert {"pyjwt", "argon2-cffi", "bcrypt", "mcp", "pytesseract"} <= names


def test_generated_baselines_match_their_input_fingerprints():
    for output, inputs in (
        ("requirements.txt", ["requirements.in"]),
        ("requirements-dev.txt", ["requirements.in", "requirements-dev.in", "requirements.txt"]),
    ):
        text = (ROOT / output).read_text()
        for filename in inputs:
            digest = hashlib.sha256((ROOT / filename).read_bytes()).hexdigest()
            assert f"# Input SHA256: {digest}  {filename}\n" in text


def test_dev_baseline_preserves_runtime_versions():
    def identity(r):
        return r.name.lower(), str(r.specifier), str(r.marker)
    runtime = {identity(r) for r in _locked("requirements.txt")}
    dev = {identity(r) for r in _locked("requirements-dev.txt")}
    assert runtime <= dev


def test_declared_runtime_inputs_are_in_the_baseline():
    locked = _locked("requirements.txt")
    for line in (ROOT / "requirements.in").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        declared = Requirement(line)
        candidates = [r for r in locked if r.name.lower() == declared.name.lower()]
        assert candidates, f"Regenerate requirements.txt for {declared.name}"
        assert all(next(iter(r.specifier)).version in declared.specifier for r in candidates)


def test_real_bm25_adapter_retrieves_expected_node():
    from llama_index.core.schema import TextNode
    from llama_index.retrievers.bm25 import BM25Retriever

    retriever = BM25Retriever.from_defaults(
        nodes=[
            TextNode(id_="av", text="Projector audio amplifier lecture hall"),
            TextNode(id_="food", text="Carrots potatoes onions garden vegetables"),
        ],
        similarity_top_k=1,
    )
    assert retriever.retrieve("projector amplifier")[0].node.node_id == "av"


def test_installed_runtime_matches_reviewed_baseline():
    for requirement in _locked("requirements.txt"):
        if requirement.marker is None or requirement.marker.evaluate():
            assert version(requirement.name) in requirement.specifier, str(requirement)
