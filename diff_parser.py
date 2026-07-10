"""Parse unified Git diffs and build display hunks."""

import re
from collections.abc import Callable

_OLD_RE = re.compile(r"^--- (?:a/(.+)|/dev/null)$")
_NEW_RE = re.compile(r"^\+\+\+ (?:b/(.+)|/dev/null)$")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

def parse_hunks(diff_text: str) -> list[dict]:
    hunks = []
    current_file = None
    old_file = None
    new_file = None
    for line in diff_text.splitlines():
        m = _OLD_RE.match(line)
        if m:
            old_file = m.group(1)
            continue

        m = _NEW_RE.match(line)
        if m:
            new_file = m.group(1)
            current_file = new_file or old_file
            continue

        m = _HUNK_RE.match(line)
        if m and current_file:
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            hunks.append({
                "filepath": current_file,
                "start": new_start,
                "end": max(new_start, new_start + new_count - 1),
                "old_start": old_start,
                "old_end": max(old_start, old_start + old_count - 1),
                "changed_lines": set(),
                "_cursor": new_start,
                "diff_lines": [],
                "added_count": 0,
                "deleted_count": 0,
            })
            continue

        if hunks and current_file == hunks[-1]["filepath"]:
            h = hunks[-1]
            if line.startswith("+") and not line.startswith("+++"):
                h["diff_lines"].append(line)
                h["changed_lines"].add(h["_cursor"])
                h["_cursor"] += 1
                h["added_count"] += 1
            elif line.startswith("-"):
                h["diff_lines"].append(line)
                h["deleted_count"] += 1
            elif line.startswith(" "):
                h["diff_lines"].append(line)
                h["_cursor"] += 1
            elif line.startswith("\\"):
                h["diff_lines"].append(line)

    for h in hunks:
        h.pop("_cursor", None)
        h["changed_lines"] = sorted(h["changed_lines"])
        h["changed_count"] = h["added_count"] + h["deleted_count"]

    return hunks


def expand_and_merge(
    hunks: list[dict],
    repo_path: str,
    context: int,
    merge_thresh: int,
    content_source: dict | None = None,
    read_source_lines: Callable[[str, str, dict], list[str]] | None = None,
) -> list[dict]:
    by_file: dict[str, list[dict]] = {}
    for h in hunks:
        by_file.setdefault(h["filepath"], []).append(h)

    result = []
    if read_source_lines is None:
        raise ValueError("read_source_lines is required")

    source = content_source or {"type": "worktree"}
    for filepath, fhunks in by_file.items():
        try:
            total_lines = len(read_source_lines(repo_path, filepath, source))
        except Exception:
            total_lines = 10 ** 6

        expanded = []
        for h in fhunks:
            diff_block = {
                "start": int(h["start"]),
                "old_start": int(h.get("old_start", h["start"])),
                "changed_lines": sorted(h.get("changed_lines", [])),
                "diff_lines": list(h.get("diff_lines", [])),
            }
            # preserve original hunk bounds (orig_start/orig_end)
            expanded.append({
                "filepath": filepath,
                "start": max(1, h["start"] - context),
                "end": min(total_lines, h["end"] + context),
                "orig_start": int(h["start"]),
                "orig_end": int(h["end"]),
                "old_start": h.get("old_start", h["start"]),
                "changed_lines": set(h["changed_lines"]),
                "diff_lines": list(h.get("diff_lines", [])),
                "diff_blocks": [diff_block],
                "added_count": int(h.get("added_count", 0)),
                "deleted_count": int(h.get("deleted_count", 0)),
                "diff_cmd": h.get("diff_cmd"),
            })

        merged = [expanded[0]]
        for h in expanded[1:]:
            prev = merged[-1]
            # 判定は拡張前のオリジナル位置（orig_start/orig_end）で行う
            gap = h["orig_start"] - prev.get("orig_end", prev["end"]) - 1
            if gap <= merge_thresh:
                prev["end"] = max(prev["end"], h["end"])
                prev["orig_start"] = min(int(prev.get("orig_start", prev["start"])), int(h.get("orig_start", h["start"])))
                prev["orig_end"] = max(int(prev.get("orig_end", prev["end"])), int(h.get("orig_end", h["end"])))
                prev["old_start"] = min(int(prev.get("old_start", prev["start"])), int(h.get("old_start", h["start"])))
                prev["changed_lines"] |= h["changed_lines"]
                prev["diff_lines"].extend(h.get("diff_lines", []))
                prev.setdefault("diff_blocks", []).extend(h.get("diff_blocks", []))
                prev["added_count"] = int(prev.get("added_count", 0)) + int(h.get("added_count", 0))
                prev["deleted_count"] = int(prev.get("deleted_count", 0)) + int(h.get("deleted_count", 0))
            else:
                merged.append(h)

        for h in merged:
            h["changed_lines"] = sorted(h["changed_lines"])

        result.extend(merged)

    return result



