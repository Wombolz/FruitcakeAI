import json

import pytest

from app.coding_runtime.review_input import captured_read, numbered_read, review_prompt
from app.coding_runtime.review_output import ReviewOutput
from app.coding_runtime.contracts import CodingRuntimeError


@pytest.mark.parametrize("source", ["one\n  two\n", "one\r\n\ttwo\r\n", "\u754c\nlast", "", "\n", "no newline"])
def test_numbered_read_is_complete_reversible_and_uses_original_lines(source):
    presentation = numbered_read("file.txt", "after", source)
    lines = presentation.split("\n", 2)[2].splitlines(keepends=True)
    assert "".join(line.split("|", 1)[1] for line in lines) == source
    assert json.loads(presentation.splitlines()[0])["source_lines"] == len(source.splitlines())
    for number, line in enumerate(lines, 1):
        assert line.startswith(f"L{number}|")


def test_prompt_contains_metadata_not_source_or_duplicate_patch():
    source = "PRIVATE_CAPTURE_MARKER\n"
    manifest = {"scope": "checkout_changes", "base": "base", "head": "head", "context": [], "omission_counts": {},
        "files": [{"path": "file.txt", "before": None, "after": {"bytes": len(source)}, "before_mode": None, "after_mode": "100644"}]}
    prompt = review_prompt(manifest, {("file.txt", "after"): source})
    data = json.loads(prompt.split("\nCapture data:\n", 1)[1])
    assert source.strip() not in prompt
    assert "patches" not in data and data["source_delivery"] == "captured_read_tools_only"
    assert data["files"][0]["after"] == {"lines": 1, "source_bytes": len(source.encode())}
    assert data["files"][0]["change"] == "created" and data["files"][0]["before"] is None


def test_number_labels_are_not_accepted_as_source_evidence_or_reanchored():
    sides = {("file.txt", "after"): "first\n    return None\nthird\n"}
    finding = {"type": "finding", "severity": "should_fix", "title": "Example", "body": "Example", "path": "file.txt",
        "side": "after", "line_start": 2, "line_end": 2, "evidence": "return None"}
    assert list(ReviewOutput(sides).feed(json.dumps(finding), flush=True)) == [finding]
    for change in ({"line_start": 1, "line_end": 1}, {"evidence": "L2|    return None"}, {"evidence": "return ..."}):
        with pytest.raises(CodingRuntimeError) as error:
            list(ReviewOutput(sides).feed(json.dumps({**finding, **change}), flush=True))
        assert error.value.validation_reason == "evidence_mismatch"


def test_patch_presentation_is_explicitly_diff_not_source():
    patch = "@@ -1 +1 @@\n-old\n+new\n"
    value = captured_read("file.txt", "patch", patch)
    assert value.endswith(patch) and "use before/after for findings" in value
