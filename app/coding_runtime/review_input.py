"""Model-facing views of retained evidence; never read the current checkout."""
import json

from app.coding_runtime.review_output import RUBRIC


def review_prompt(manifest, sides):
    files = []
    for item in manifest["files"]:
        entry = {"path": item["path"], "before_mode": item["before_mode"], "after_mode": item["after_mode"]}
        for side in ("before", "after"):
            text = sides.get((item["path"], side))
            entry[side] = None if text is None else {"lines": len(text.splitlines()), "source_bytes": len(text.encode())}
        entry["change"] = "created" if item["before"] is None else "deleted" if item["after"] is None else "modified"
        files.append(entry)
    data = {"scope": manifest["scope"], "base": manifest["base"], "head": manifest["head"],
        "files": files, "context_paths": [item["path"] for item in manifest["context"]],
        "omission_counts": manifest["omission_counts"], "source_delivery": "captured_read_tools_only"}
    return RUBRIC + "\nCapture data:\n" + json.dumps(data, ensure_ascii=False)


def numbered_read(path, side, text):
    # Prefix the original logical lines without stripping source whitespace or
    # changing line endings. Prefixes/header are presentation, never evidence.
    header = json.dumps({"path": path, "side": side, "source_lines": len(text.splitlines()),
        "source_bytes": len(text.encode()), "presentation": "L<number>| followed by original source"}, ensure_ascii=False)
    numbered = "".join(f"L{number}|{line}" for number, line in enumerate(text.splitlines(keepends=True), 1))
    return header + "\nLine labels are not source. Quote only text after | as evidence.\n" + numbered


def captured_read(path, side, text):
    if side == "patch":
        return json.dumps({"path": path, "side": side, "presentation": "retained unified diff; use before/after for findings"}, ensure_ascii=False) + "\n" + text
    return numbered_read(path, side, text)
