"""Render parsed diff hunks as standalone HTML."""

import difflib
import re
from collections.abc import Callable
from html import escape
from pathlib import Path

HTML_WIDTH = 960
BACKGROUND_MODE = "normal"
DIFF_MODE = "file"
INLINE_DIFF_DEFAULT_MODE = "full"
INLINE_DIFF_MAX_CHANGED_CHARS = 120
INLINE_DIFF_MAX_CHANGED_CHARS_LIMIT = 500
INLINE_DIFF_MERGE_SEPARATOR_MAX_CHARS = 12
INLINE_DIFF_NEGATION_BRIDGE_MAX_CHARS = 32
INLINE_DIFF_MIN_SIMILARITY = 0.62
INLINE_DIFF_TAG_BLOCK_MIN_SIMILARITY = 0.8
INLINE_DIFF_ISOLATED_DELETION_MAX_LINES = 2
INLINE_DIFF_ISOLATED_DELETION_MIN_ADDED_DISTANCE = 2
INLINE_DIFF_MODES = {"full", "off"}
INLINE_ADDED_MUTES_LIMIT = 200
INLINE_ADDED_MUTE_KEY_LIMIT = 1000
INLINE_HIDDEN_CHANGES_LIMIT = 400
INLINE_HIDDEN_CHANGE_KEY_LIMIT = 1000
MANUAL_ROW_HIGHLIGHTS_LIMIT = 500
MANUAL_ROW_HIGHLIGHT_COLORS = {"green", "yellow"}
CODE_TAB_SIZE = 4
_INLINE_DIFF_TOKEN_RE = re.compile(r"\w+|\s+|[^\w\s]+", re.UNICODE)
_CONTROL_INLINE_DIFF_TOKEN_RE = re.compile(r"\w+|\s+|[^\w\s]", re.UNICODE)
_HTML_TAG_NAME_RE = re.compile(r"(</?\s*)[A-Za-z][\w:-]*")
_CONTROL_HEADER_RE = re.compile(
    r"^\s*(?:}\s*)?(?:else\s+)?(if|elif|elseif|while|for|foreach|switch|case|catch)\b"
)
_CONTROL_EXIT_RE = re.compile(r"^\s*(?:return|throw|raise|break|continue)\b")


def _control_header_kind(text: str) -> str | None:
    match = _CONTROL_HEADER_RE.search(text)
    if not match:
        return None
    keyword = match.group(1)
    if keyword in {"elif", "elseif"}:
        return "if"
    if keyword == "foreach":
        return "for"
    return keyword


def hunk_inline_diff_mode(hunk: dict) -> str:
    mode = str(hunk.get("inline_diff_mode", "")).strip().lower()
    if mode == "new":
        return "off"
    if mode in INLINE_DIFF_MODES:
        return mode
    return "full" if bool(hunk.get("inline_diff_enabled", True)) else "off"


def normalize_inline_diff_mode(value: str | None, default: str = "full") -> str:
    mode = str(value or default).strip().lower()
    if mode == "new":
        return "off"
    if mode not in INLINE_DIFF_MODES:
        raise ValueError("inline_diff_default_mode は full, off のいずれかを指定してください")
    return mode


def normalize_inline_diff_max_changed_chars(value: int | str | None) -> int:
    return max(0, min(INLINE_DIFF_MAX_CHANGED_CHARS_LIMIT, int(value)))


def normalize_inline_added_mute_key(value: str | None) -> str:
    key = str(value or "")
    if not key or len(key) > INLINE_ADDED_MUTE_KEY_LIMIT:
        raise ValueError("key が不正です")
    return key


def hunk_inline_added_mutes(hunk: dict) -> set[str]:
    values = hunk.get("inline_added_mutes", [])
    if not isinstance(values, list):
        return set()
    return {
        str(value)
        for value in values[:INLINE_ADDED_MUTES_LIMIT]
        if isinstance(value, str) and value
    }


def normalize_inline_hidden_change_key(value: str | None) -> str:
    key = str(value or "")
    if not key or len(key) > INLINE_HIDDEN_CHANGE_KEY_LIMIT:
        raise ValueError("key が不正です")
    if not (key.startswith("added:") or key.startswith("deleted:")):
        raise ValueError("key が不正です")
    return key


def hunk_inline_hidden_changes(hunk: dict) -> set[str]:
    values = hunk.get("inline_hidden_changes", [])
    if not isinstance(values, list):
        return set()
    return {
        str(value)
        for value in values[:INLINE_HIDDEN_CHANGES_LIMIT]
        if isinstance(value, str)
        and (value.startswith("added:") or value.startswith("deleted:"))
    }


def normalize_manual_row_highlight_color(value: str | None) -> str:
    color = str(value or "").strip().lower()
    if color in ("", "none", "off", "clear"):
        return ""
    if color not in MANUAL_ROW_HIGHLIGHT_COLORS:
        raise ValueError("color は green, yellow, none のいずれかを指定してください")
    return color


def hunk_manual_row_highlights(hunk: dict) -> dict[int, str]:
    values = hunk.get("manual_row_highlights", {})
    if not isinstance(values, dict):
        return {}

    highlights: dict[int, str] = {}
    for raw_lineno, raw_color in list(values.items())[:MANUAL_ROW_HIGHLIGHTS_LIMIT]:
        try:
            lineno = int(raw_lineno)
        except (TypeError, ValueError):
            continue
        try:
            color = normalize_manual_row_highlight_color(str(raw_color))
        except ValueError:
            continue
        if lineno > 0 and color:
            highlights[lineno] = color
    return highlights


def _visual_indent_width(text: str, tab_size: int = CODE_TAB_SIZE) -> int:
    column = 0
    for char in text:
        if char == " ":
            column += 1
        elif char == "\t":
            column += tab_size - (column % tab_size)
        else:
            break
    return column


def _remove_visual_indent(text: str, width: int, tab_size: int = CODE_TAB_SIZE) -> str:
    if width <= 0:
        return text

    index = 0
    while index < len(text) and text[index] in {" ", "\t"}:
        index += 1
    indent_width = _visual_indent_width(text[:index], tab_size)
    remaining_width = max(0, indent_width - width)
    return (" " * remaining_width) + text[index:]


def _strip_common_indent_from_lines(lines: list[str]) -> tuple[list[str], int]:
    """Remove the common leading indentation measured in rendered columns."""
    indent_widths = []
    for text in lines:
        if text is None:
            continue
        if text.strip() == "":
            continue
        indent_widths.append(_visual_indent_width(text))

    if not indent_widths:
        return lines, 0

    common_width = min(indent_widths)
    if common_width == 0:
        return lines, 0

    stripped = [
        _remove_visual_indent(text or "", common_width)
        for text in lines
    ]
    return stripped, common_width

def read_lines(
    repo_path: str,
    filepath: str,
    start: int,
    end: int,
    content_source: dict | None = None,
    read_source_lines: Callable[[str, str, dict], list[str]] | None = None,
) -> list[tuple[int, str]]:
    if read_source_lines is None:
        raise ValueError("read_source_lines is required")
    try:
        lines = read_source_lines(repo_path, filepath, content_source or {"type": "worktree"})
    except Exception as e:
        return [(start, f"# 読み込みエラー: {e}")]
    return [(i + 1, lines[i]) for i in range(start - 1, min(end, len(lines)))]


def _default_render_config() -> dict:
    return {
        "html_width": HTML_WIDTH,
        "background_mode": BACKGROUND_MODE,
        "diff_mode": DIFF_MODE,
        "inline_diff_max_changed_chars": INLINE_DIFF_MAX_CHANGED_CHARS,
    }


def detect_language(filepath: str) -> str:
    ext_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".jsx": "javascript", ".tsx": "typescript", ".rb": "ruby",
        ".java": "java", ".cs": "csharp", ".go": "go", ".rs": "rust",
        ".cpp": "cpp", ".c": "c", ".php": "php", ".swift": "swift",
        ".kt": "kotlin", ".sh": "bash", ".html": "html", ".css": "css",
        ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
        ".sql": "sql", ".md": "markdown",
    }
    return ext_map.get(Path(filepath).suffix.lower(), "plaintext")


# ================================================================
# HTML 生成（PNG出力用・プレビュー共用）
# ================================================================


def _compose_html(rows_html: str, filepath: str, meta: str, lang: str, bg_color: str,
                  html_width: int, show_footer: bool, timestamp: str, diff_cmd: str) -> str:
    head_template = """<!DOCTYPE html>
<html lang=\"ja\"><head><meta charset=\"UTF-8\">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
html{{background:{bg_color};width:{HTML_WIDTH}px}}
body{{background:{bg_color};font-family:'Consolas','Menlo','Monaco',monospace;font-size:13px;
        color:#1a1a1a;width:{HTML_WIDTH}px;padding:16px}}
.header{{background:#1e1e2e;color:#cdd6f4;padding:10px 14px;border-radius:6px 6px 0 0;
    display:flex;justify-content:space-between;align-items:center;font-size:12px}}
.filepath{{color:#89dceb;font-weight:bold;word-break:break-all}}
.meta{{color:#6c7086;white-space:nowrap;margin-left:12px;flex-shrink:0}}
.code-block{{background:#fff;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 6px 6px;overflow:hidden}}
table{{width:100%;border-collapse:collapse}}
tr{{border-bottom:1px solid #f1f5f9}}
tr:last-child{{border-bottom:none}}
tr.changed{{background:#fefce8}}
tr.added{{background:#ecfdf5}}
tr.deleted{{background:#fef2f2}}
tr.note td{{color:#64748b;font-style:italic}}
td{{vertical-align:top;padding:2px 0;line-height:1.6}}
td.lineno{{width:52px;text-align:right;color:#94a3b8;padding:2px 10px 2px 6px;
    border-right:1px solid #e2e8f0;background:#f8fafc;user-select:none}}
td.lineno.deletion-before,td.lineno.deletion-after{{position:relative}}
td.lineno.deletion-before::after,td.lineno.deletion-after::before{{
    content:"";position:absolute;right:2px;width:6px;height:8px;z-index:2;
    background:#dc2626;clip-path:polygon(0 0,100% 50%,0 100%)}}
td.lineno.deletion-before::after{{top:0;transform:translateY(-50%)}}
td.lineno.deletion-after::before{{bottom:0;transform:translateY(50%)}}
tr.changed td.lineno{{background:#fef9c3;color:#78716c}}
tr.added td.lineno{{background:#ecfdf5;color:#64748b}}
td.lineno.new{{border-right:none}}
td.marker{{width:18px;text-align:center;color:#64748b;font-weight:bold}}
tr.added td.marker{{color:#16a34a}}
tr.deleted td.marker{{color:#dc2626}}
td.code{{padding:2px 8px;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;tab-size:{CODE_TAB_SIZE}}}
span.inline-added{{background:#bbf7d0;color:#14532d;border-radius:3px;padding:0 2px}}
span.inline-added.inline-added-muted{{background:transparent;color:inherit}}
span.inline-deleted{{background:#fecaca;color:#991b1b;border-radius:3px;padding:0 2px;text-decoration:line-through}}
span.inline-change-hidden{{display:none}}
tr.changed.inline-rendered{{background:#f8fafc}}
tr.changed.inline-rendered td.lineno{{background:#f8fafc;color:#64748b}}
tr.manual-row-green{{background:#ecfdf5}}
tr.manual-row-green td.lineno{{background:#dcfce7;color:#64748b}}
tr.manual-row-yellow{{background:#fefce8}}
tr.manual-row-yellow td.lineno{{background:#fef9c3;color:#78716c}}
.footer{{margin-top:8px;font-size:11px;color:#94a3b8;text-align:right}}
</style></head><body>
<div class=\"header\">
    <span class=\"filepath\">{filepath}</span>
    <span class=\"meta\">{meta}</span>
</div>
<div class=\"code-block\"><table>
{rows}
</table></div>
"""

    footer_template = "<div class=\"footer\">{timestamp} | {diff_cmd}</div>"

    html = head_template.format(
        bg_color=bg_color,
        HTML_WIDTH=html_width,
        CODE_TAB_SIZE=CODE_TAB_SIZE,
        filepath=escape(filepath),
        meta=escape(meta),
        rows=rows_html,
    )
    if show_footer:
        html += footer_template.format(timestamp=escape(timestamp), diff_cmd=escape(diff_cmd))
    html += "</body></html>"
    return html


def _html_render_options(config: dict) -> tuple[int, str, bool]:
    background_mode = str(config.get("background_mode", BACKGROUND_MODE))
    html_width = int(config.get("html_width", HTML_WIDTH))
    bg_color = "transparent" if background_mode != "normal" else "#fff"
    show_footer = background_mode != "transparent_no_footer"
    return html_width, bg_color, show_footer


def _compose_hunk_html(
    rows: list[str],
    hunk: dict,
    meta: str,
    lang: str,
    timestamp: str,
    config: dict,
) -> str:
    html_width, bg_color, show_footer = _html_render_options(config)
    return _compose_html(
        "".join(rows),
        hunk["filepath"],
        meta,
        lang,
        bg_color,
        html_width,
        show_footer,
        timestamp,
        hunk.get("diff_cmd", "git diff HEAD"),
    )


def _stripped_diff_texts_by_index(diff_lines: list[str], include_raw) -> dict[int, str]:
    texts_for_indent = []
    for raw in diff_lines:
        if not raw or not include_raw(raw):
            continue
        part = raw[1:]
        if part.strip() == "":
            continue
        texts_for_indent.append(part)

    if not texts_for_indent:
        return {}

    stripped_texts, _ = _strip_common_indent_from_lines(texts_for_indent)
    stripped_by_index: dict[int, str] = {}
    text_iter = iter(stripped_texts)
    for idx, raw in enumerate(diff_lines):
        if not raw or not include_raw(raw):
            continue
        part = raw[1:]
        if part.strip() == "":
            stripped_by_index[idx] = part
        else:
            stripped_by_index[idx] = next(text_iter)
    return stripped_by_index


def _line_replacements_for_diff_lines(
    old_start: int,
    new_start: int,
    changed_lines: list[int],
    diff_lines: list[str],
) -> dict[int, tuple[int | None, str]]:
    replacements: dict[int, tuple[int | None, str]] = {}
    try:
        old_ln = int(old_start)
    except (TypeError, ValueError):
        old_ln = 1
    try:
        new_ln = int(new_start)
    except (TypeError, ValueError):
        new_ln = 1
    changed_line_numbers: list[int] = []
    for lineno in changed_lines:
        try:
            changed_line_numbers.append(int(lineno))
        except (TypeError, ValueError):
            continue
    changed_line_numbers.sort()
    added_line_index = 0

    def similarity(old_text: str, new_text: str) -> float:
        old_stripped = old_text.strip()
        new_stripped = new_text.strip()
        if not old_stripped or not new_stripped:
            return 0.0
        if not re.search(r"\w", old_stripped) or not re.search(r"\w", new_stripped):
            return 0.0
        if old_stripped == new_stripped and old_text != new_text:
            return 0.0

        old_tokens = _INLINE_DIFF_TOKEN_RE.findall(old_text)
        new_tokens = _INLINE_DIFF_TOKEN_RE.findall(new_text)
        token_score = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False).ratio()
        char_score = difflib.SequenceMatcher(None, old_text, new_text, autojunk=False).ratio()
        matching_chars = sum(
            block.size
            for block in difflib.SequenceMatcher(None, old_text, new_text, autojunk=False).get_matching_blocks()
        )
        preserved_shorter_score = matching_chars / max(1, min(len(old_text), len(new_text)))
        length_ratio = min(len(old_text), len(new_text)) / max(1, max(len(old_text), len(new_text)))
        insertion_score = preserved_shorter_score * (0.55 + 0.45 * length_ratio)
        return max(min(token_score, char_score), insertion_score)

    def html_tag_shape(text: str) -> str:
        return _HTML_TAG_NAME_RE.sub(r"\1#", text)

    def html_tag_shape_similarity(old_text: str, new_text: str) -> float:
        if not _HTML_TAG_NAME_RE.search(old_text) or not _HTML_TAG_NAME_RE.search(new_text):
            return 0.0
        return difflib.SequenceMatcher(
            None,
            html_tag_shape(old_text),
            html_tag_shape(new_text),
            autojunk=False,
        ).ratio()

    def simple_html_tag_name(text: str) -> str | None:
        match = re.fullmatch(r"</?\s*([A-Za-z][\w:-]*)\s*/?\s*>", text.strip())
        return match.group(1).lower() if match else None

    def directly_pairable(old_text: str, new_text: str) -> bool:
        old_stripped = old_text.strip()
        new_stripped = new_text.strip()
        if not old_stripped or not new_stripped:
            return False
        if not re.search(r"\w", old_stripped) or not re.search(r"\w", new_stripped):
            return False
        if old_stripped == new_stripped and old_text != new_text:
            return False
        old_tag = simple_html_tag_name(old_text)
        new_tag = simple_html_tag_name(new_text)
        if old_tag and new_tag and old_tag != new_tag:
            return False
        return True

    def structural_line_role(text: str) -> str:
        if _control_header_kind(text):
            return "control_header"
        if _CONTROL_EXIT_RE.search(text):
            return "control_exit"
        return "statement"

    def structurally_pairable(old_text: str, new_text: str) -> bool:
        old_role = structural_line_role(old_text)
        new_role = structural_line_role(new_text)
        if old_role == new_role:
            return True
        return old_role == "statement" and new_role == "statement"

    def block_allows_html_tag_pairing(
        deleted_block: list[tuple[int, str]],
        added_block: list[tuple[int, str]],
    ) -> bool:
        if len(deleted_block) < 2 or len(added_block) < 2:
            return False
        old_text = "\n".join(text for _, text in deleted_block)
        new_text = "\n".join(text for _, text in added_block)
        if len(_HTML_TAG_NAME_RE.findall(old_text)) < 2 or len(_HTML_TAG_NAME_RE.findall(new_text)) < 2:
            return False
        score = difflib.SequenceMatcher(
            None,
            html_tag_shape(old_text),
            html_tag_shape(new_text),
            autojunk=False,
        ).ratio()
        return score >= INLINE_DIFF_TAG_BLOCK_MIN_SIMILARITY

    def pair_replace_block(
        deleted_block: list[tuple[int, str]],
        added_block: list[tuple[int, str]],
    ) -> None:
        allow_html_tag_pairing = block_allows_html_tag_pairing(deleted_block, added_block)

        def candidate_score(old_text: str, new_text: str) -> float:
            if not structurally_pairable(old_text, new_text):
                return 0.0
            score = similarity(old_text, new_text)
            if allow_html_tag_pairing:
                score = max(score, html_tag_shape_similarity(old_text, new_text))
            return score

        deleted_count = len(deleted_block)
        added_count = len(added_block)
        score_matrix = [
            [candidate_score(old_text, new_text) for _, new_text in added_block]
            for _, old_text in deleted_block
        ]

        def add_direct_pairs() -> None:
            for (old_lineno, old_text), (new_lineno, _) in zip(deleted_block, added_block):
                replacements[new_lineno] = (old_lineno, old_text)

        if deleted_count == 1 and added_count == 1:
            old_text = deleted_block[0][1]
            new_text = added_block[0][1]
            if (
                directly_pairable(old_text, new_text)
                and candidate_score(old_text, new_text) >= INLINE_DIFF_MIN_SIMILARITY
            ):
                add_direct_pairs()
            return

        if deleted_count == added_count and deleted_count > 1:
            shifted = False
            for idx in range(deleted_count):
                direct_score = score_matrix[idx][idx]
                off_axis_score = max(
                    [
                        score_matrix[idx][new_idx]
                        for new_idx in range(added_count)
                        if new_idx != idx
                    ] + [
                        score_matrix[old_idx][idx]
                        for old_idx in range(deleted_count)
                        if old_idx != idx
                    ],
                    default=0.0,
                )
                if direct_score < INLINE_DIFF_MIN_SIMILARITY and off_axis_score >= INLINE_DIFF_MIN_SIMILARITY:
                    shifted = True
                    break
            if not shifted and all(
                directly_pairable(old_text, new_text)
                and score_matrix[idx][idx] >= INLINE_DIFF_MIN_SIMILARITY
                for idx, ((_, old_text), (_, new_text)) in enumerate(
                    zip(deleted_block, added_block)
                )
            ):
                add_direct_pairs()
                return

        # Keep line order like an editor diff alignment. This avoids crossed matches
        # that can look plausible by score but highlight the wrong added line.
        dp: list[list[tuple[float, int, int, list[tuple[int, int]]]]] = [
            [(0.0, 0, 0, []) for _ in range(added_count + 1)]
            for _ in range(deleted_count + 1)
        ]

        def better(
            current: tuple[float, int, int, list[tuple[int, int]]],
            candidate: tuple[float, int, int, list[tuple[int, int]]],
        ) -> tuple[float, int, int, list[tuple[int, int]]]:
            if (
                candidate[0] > current[0]
                or (candidate[0] == current[0] and candidate[1] > current[1])
                or (candidate[0] == current[0] and candidate[1] == current[1] and candidate[2] < current[2])
            ):
                return candidate
            return current

        for deleted_idx in range(1, deleted_count + 1):
            for added_idx in range(1, added_count + 1):
                best = better(dp[deleted_idx - 1][added_idx], dp[deleted_idx][added_idx - 1])
                score = score_matrix[deleted_idx - 1][added_idx - 1]
                if score >= INLINE_DIFF_MIN_SIMILARITY:
                    previous_score, previous_pairs, previous_distance, previous_matches = dp[deleted_idx - 1][added_idx - 1]
                    best = better(best, (
                        previous_score + score,
                        previous_pairs + 1,
                        previous_distance + abs((deleted_idx - 1) - (added_idx - 1)),
                        previous_matches + [(deleted_idx - 1, added_idx - 1)],
                    ))
                dp[deleted_idx][added_idx] = best

        for deleted_idx, added_idx in dp[deleted_count][added_count][3]:
            old_lineno, old_text = deleted_block[deleted_idx]
            new_lineno, _ = added_block[added_idx]
            replacements[new_lineno] = (old_lineno, old_text)

    old_items: list[tuple[int, str]] = []
    new_items: list[tuple[int, str]] = []

    for raw in diff_lines:
        if not raw or raw.startswith("\\"):
            continue

        if raw.startswith("-") and not raw.startswith("---"):
            old_items.append((old_ln, raw[1:]))
            old_ln += 1
        elif raw.startswith("+") and not raw.startswith("+++"):
            if added_line_index < len(changed_line_numbers):
                added_lineno = changed_line_numbers[added_line_index]
            else:
                added_lineno = new_ln
            added_line_index += 1
            new_items.append((added_lineno, raw[1:]))
            new_ln = added_lineno + 1
        elif raw.startswith(" "):
            text = raw[1:]
            old_items.append((old_ln, text))
            new_items.append((new_ln, text))
            old_ln += 1
            new_ln += 1

    matcher = difflib.SequenceMatcher(
        None,
        [text for _, text in old_items],
        [text for _, text in new_items],
        autojunk=False,
    )
    for tag, old_start_idx, old_end_idx, new_start_idx, new_end_idx in matcher.get_opcodes():
        if tag == "replace":
            pair_replace_block(
                old_items[old_start_idx:old_end_idx],
                new_items[new_start_idx:new_end_idx],
            )
    return replacements


def _line_replacements_by_new_lineno(hunk: dict) -> dict[int, tuple[int | None, str]]:
    replacements: dict[int, tuple[int | None, str]] = {}
    diff_blocks = hunk.get("diff_blocks")
    if isinstance(diff_blocks, list) and diff_blocks:
        for block in diff_blocks:
            if not isinstance(block, dict):
                continue
            replacements.update(_line_replacements_for_diff_lines(
                int(block.get("old_start", hunk.get("old_start", hunk.get("start", 1)))),
                int(block.get("start", hunk.get("start", 1))),
                list(block.get("changed_lines", [])),
                list(block.get("diff_lines", [])),
            ))
    else:
        replacements = _line_replacements_for_diff_lines(
            int(hunk.get("old_start", hunk.get("start", 1))),
            int(hunk.get("orig_start", hunk.get("default_start", hunk.get("start", 1)))),
            list(hunk.get("changed_lines", [])),
            list(hunk.get("diff_lines", [])),
        )

    changed_set = set()
    for lineno in hunk.get("changed_lines", []):
        try:
            changed_set.add(int(lineno))
        except (TypeError, ValueError):
            continue
    return {lineno: value for lineno, value in replacements.items() if lineno in changed_set}


def _inline_diff_html(
    old_text: str,
    new_text: str,
    mode: str = "full",
    max_changed_chars: int | None = None,
    muted_added_keys: set[str] | None = None,
    added_key_prefix: str = "",
    hidden_change_keys: set[str] | None = None,
) -> str | None:
    parts = []
    added_index = 0
    deleted_index = 0
    muted_added_keys = muted_added_keys or set()
    hidden_change_keys = hidden_change_keys or set()
    changed_chars_limit = (
        INLINE_DIFF_MAX_CHANGED_CHARS
        if max_changed_chars is None
        else normalize_inline_diff_max_changed_chars(max_changed_chars)
    )
    old_header = _control_header_kind(old_text)
    preserve_control_boundary = bool(
        old_header and old_header == _control_header_kind(new_text)
    )
    token_re = (
        _CONTROL_INLINE_DIFF_TOKEN_RE
        if preserve_control_boundary
        else _INLINE_DIFF_TOKEN_RE
    )
    old_tokens = token_re.findall(old_text)
    new_tokens = token_re.findall(new_text)
    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
    changed_chars = 0
    opcodes = matcher.get_opcodes()
    opcodes = _merge_nearby_inline_fragments(
        opcodes,
        old_tokens,
        new_tokens,
        preserve_closing_parenthesis_boundary=preserve_control_boundary,
    )
    show_old = mode == "full"
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        changed_chars += len("".join(old_tokens[i1:i2])) + len("".join(new_tokens[j1:j2]))
    if changed_chars > changed_chars_limit:
        return None

    def added_span(text: str) -> str:
        nonlocal added_index
        legacy_key = f"{added_key_prefix}:{added_index}:{text}"
        hidden_key = f"added:{legacy_key}"
        added_index += 1
        classes = ["inline-added"]
        if legacy_key in muted_added_keys:
            classes.append("inline-added-muted")
        if hidden_key in hidden_change_keys:
            classes.append("inline-change-hidden")
        class_name = " ".join(classes)
        return f'<span class="{class_name}">{escape(text)}</span>'

    def deleted_span(text: str) -> str:
        nonlocal deleted_index
        hidden_key = f"deleted:{added_key_prefix}:{deleted_index}:{text}"
        deleted_index += 1
        class_name = "inline-deleted"
        if hidden_key in hidden_change_keys:
            class_name += " inline-change-hidden"
        return f'<span class="{class_name}">{escape(text)}</span>'

    for tag, i1, i2, j1, j2 in opcodes:
        old_part = "".join(old_tokens[i1:i2])
        new_part = "".join(new_tokens[j1:j2])
        is_leading_indent = (
            not parts
            and old_part.strip(" \t") == ""
            and new_part.strip(" \t") == ""
        )
        if tag == "equal":
            parts.append(escape(new_part))
        elif tag == "insert":
            parts.append(
                escape(new_part)
                if is_leading_indent
                else added_span(new_part)
            )
        elif tag == "delete":
            if show_old and not is_leading_indent:
                parts.append(deleted_span(old_part))
        elif tag == "replace":
            if is_leading_indent:
                parts.append(escape(new_part))
            else:
                if show_old:
                    parts.append(deleted_span(old_part))
                parts.append(added_span(new_part))
    return "".join(parts)


def _merge_nearby_inline_fragments(
    opcodes: list[tuple[str, int, int, int, int]],
    old_tokens: list[str],
    new_tokens: list[str],
    *,
    preserve_closing_parenthesis_boundary: bool = False,
) -> list[tuple[str, int, int, int, int]]:
    merged: list[tuple[str, int, int, int, int]] = []
    idx = 0

    def is_change(tag: str) -> bool:
        return tag in {"insert", "delete", "replace"}

    def is_short_separator(opcode: tuple[str, int, int, int, int]) -> bool:
        tag, i1, i2, j1, j2 = opcode
        if tag != "equal":
            return False
        old_part = "".join(old_tokens[i1:i2])
        new_part = "".join(new_tokens[j1:j2])
        return (
            old_part == new_part
            and len(old_part) <= INLINE_DIFF_MERGE_SEPARATOR_MAX_CHARS
            and not re.search(r"\w", old_part)
        )

    def is_closing_parenthesis_boundary(
        opcode: tuple[str, int, int, int, int],
    ) -> bool:
        tag, i1, i2, j1, j2 = opcode
        if tag != "equal":
            return False
        old_part = "".join(old_tokens[i1:i2])
        new_part = "".join(new_tokens[j1:j2])
        return old_part == new_part and ")" in old_part

    def change_text(opcode: tuple[str, int, int, int, int]) -> tuple[str, str]:
        tag, i1, i2, j1, j2 = opcode
        if not is_change(tag):
            return "", ""
        return "".join(old_tokens[i1:i2]), "".join(new_tokens[j1:j2])

    def is_negation_only_change(opcode: tuple[str, int, int, int, int]) -> bool:
        old_part, new_part = change_text(opcode)
        old_compact = old_part.strip()
        new_compact = new_part.strip()
        old_without_parentheses = old_compact.replace("(", "").replace(")", "")
        new_without_parentheses = new_compact.replace("(", "").replace(")", "")
        return (
            old_compact == "!"
            or new_compact == "!"
            or (old_without_parentheses == "!" and new_without_parentheses == "")
            or (new_without_parentheses == "!" and old_without_parentheses == "")
            or (
                old_compact.count("!") == 1
                and old_compact.replace("!", "", 1) == new_compact
            )
            or (
                new_compact.count("!") == 1
                and new_compact.replace("!", "", 1) == old_compact
            )
        )

    def should_bridge_negation_change(
        equal_opcode: tuple[str, int, int, int, int],
        current_opcode: tuple[str, int, int, int, int],
        next_opcode: tuple[str, int, int, int, int],
    ) -> bool:
        tag, i1, i2, j1, j2 = equal_opcode
        if tag != "equal":
            return False
        old_part = "".join(old_tokens[i1:i2])
        new_part = "".join(new_tokens[j1:j2])
        return (
            old_part == new_part
            and len(old_part) <= INLINE_DIFF_NEGATION_BRIDGE_MAX_CHARS
            and (is_negation_only_change(current_opcode) or is_negation_only_change(next_opcode))
        )

    def merged_tag(i1: int, i2: int, j1: int, j2: int) -> str:
        old_empty = i1 == i2
        new_empty = j1 == j2
        if old_empty:
            return "insert"
        if new_empty:
            return "delete"
        return "replace"

    while idx < len(opcodes):
        tag, i1, i2, j1, j2 = opcodes[idx]
        if not is_change(tag):
            merged.append(opcodes[idx])
            idx += 1
            continue

        end_idx = idx
        end_i2 = i2
        end_j2 = j2
        used_negation_bridge = False
        while (
            end_idx + 2 < len(opcodes)
            and is_change(opcodes[end_idx + 2][0])
        ):
            equal_opcode = opcodes[end_idx + 1]
            next_opcode = opcodes[end_idx + 2]
            if (
                preserve_closing_parenthesis_boundary
                and is_closing_parenthesis_boundary(equal_opcode)
            ):
                break
            merge_separator = is_short_separator(equal_opcode)
            bridge_negation = (
                not used_negation_bridge
                and should_bridge_negation_change(equal_opcode, opcodes[end_idx], next_opcode)
            )
            if not (merge_separator or bridge_negation):
                break
            _, _, equal_i2, _, equal_j2 = opcodes[end_idx + 1]
            _, _, next_i2, _, next_j2 = opcodes[end_idx + 2]
            end_i2 = next_i2 if next_i2 != equal_i2 else equal_i2
            end_j2 = next_j2 if next_j2 != equal_j2 else equal_j2
            used_negation_bridge = used_negation_bridge or bridge_negation
            end_idx += 2

        if end_idx == idx:
            merged.append(opcodes[idx])
        else:
            merged.append((merged_tag(i1, end_i2, j1, end_j2), i1, end_i2, j1, end_j2))
        idx = end_idx + 1

    return merged


def _merge_inline_punctuation_fragments(
    opcodes: list[tuple[str, int, int, int, int]],
    old_tokens: list[str],
    new_tokens: list[str],
) -> list[tuple[str, int, int, int, int]]:
    return _merge_nearby_inline_fragments(opcodes, old_tokens, new_tokens)


def _normal_view_unpaired_deletions(
    hunk: dict,
    replacements: dict[int, tuple[int | None, str]],
) -> tuple[list[dict], set[int]]:
    paired_old_linenos = {
        int(old_lineno)
        for old_lineno, _ in replacements.values()
        if old_lineno is not None
    }
    raw_blocks = hunk.get("diff_blocks")
    if isinstance(raw_blocks, list) and raw_blocks:
        blocks = [block for block in raw_blocks if isinstance(block, dict)]
    else:
        blocks = [{
            "start": hunk.get("orig_start", hunk.get("default_start", hunk.get("start", 1))),
            "old_start": hunk.get("old_start", hunk.get("start", 1)),
            "diff_lines": hunk.get("diff_lines", []),
        }]

    deletions = []
    added_linenos: set[int] = set()
    for block in blocks:
        old_lineno = int(block.get("old_start", hunk.get("old_start", 1)))
        new_lineno = int(block.get("start", hunk.get("orig_start", hunk.get("start", 1))))

        for raw in block.get("diff_lines", []):
            if not raw or raw.startswith("\\"):
                continue
            if raw.startswith("-") and not raw.startswith("---"):
                if old_lineno not in paired_old_linenos:
                    deletions.append({
                        "anchor": new_lineno,
                        "old_lineno": old_lineno,
                        "text": raw[1:],
                    })
                old_lineno += 1
            elif raw.startswith("+") and not raw.startswith("+++"):
                added_linenos.add(new_lineno)
                new_lineno += 1
            elif raw.startswith(" "):
                old_lineno += 1
                new_lineno += 1

    return sorted(
        deletions,
        key=lambda deletion: (deletion["anchor"], deletion["old_lineno"]),
    ), added_linenos


def _normal_view_anchor_is_visible(hunk: dict, anchor: int) -> bool:
    visible_start = int(hunk.get("start", 1))
    visible_end = int(hunk.get("end", visible_start))
    if visible_start <= anchor <= visible_end:
        return True

    default_end = int(hunk.get("default_end", visible_end))
    return anchor == visible_end + 1 and visible_end == default_end


def _normal_view_visible_deletions(
    hunk: dict,
    replacements: dict[int, tuple[int | None, str]],
    inline_diff_mode: str,
) -> list[dict]:
    if inline_diff_mode != "full":
        return []

    deletions, added_linenos = _normal_view_unpaired_deletions(hunk, replacements)
    if not 1 <= len(deletions) <= INLINE_DIFF_ISOLATED_DELETION_MAX_LINES:
        return []

    return [
        deletion
        for deletion in deletions
        if _normal_view_anchor_is_visible(hunk, deletion["anchor"])
        and all(
            abs(added_lineno - deletion["anchor"])
            >= INLINE_DIFF_ISOLATED_DELETION_MIN_ADDED_DISTANCE
            for added_lineno in added_linenos
        )
    ]


def _normal_view_deletion_markers(
    hunk: dict,
    replacements: dict[int, tuple[int | None, str]],
    visible_deletions: list[dict] | None = None,
) -> tuple[set[int], set[int]]:
    deletions, _ = _normal_view_unpaired_deletions(hunk, replacements)
    rendered = {
        (deletion["anchor"], deletion["old_lineno"])
        for deletion in (visible_deletions or [])
    }
    anchors = {
        deletion["anchor"]
        for deletion in deletions
        if (deletion["anchor"], deletion["old_lineno"]) not in rendered
    }

    visible_start = int(hunk.get("start", 1))
    visible_end = int(hunk.get("end", visible_start))
    before = {
        anchor
        for anchor in anchors
        if visible_start <= anchor <= visible_end
    }
    after = {
        visible_end
        for anchor in anchors
        if _normal_view_anchor_is_visible(hunk, anchor) and anchor > visible_end
    }
    return before, after


def build_code_html(
    hunk: dict,
    repo_path: str,
    hunk_index: int,
    total: int,
    timestamp: str,
    content_source: dict | None = None,
    config: dict | None = None,
    read_source_lines: Callable[[str, str, dict], list[str]] | None = None,
) -> str:
    render_config = config or _default_render_config()
    if render_config.get("diff_mode") == "patch":
        return build_patch_html(hunk, hunk_index, total, timestamp, render_config)
    if render_config.get("diff_mode") == "added":
        return build_added_patch_html(hunk, hunk_index, total, timestamp, render_config)
    if render_config.get("diff_mode") == "deleted":
        return build_deleted_patch_html(hunk, hunk_index, total, timestamp, render_config)

    if len(hunk.get("changed_lines", [])) == 0 and int(hunk.get("deleted_count", 0)) > 0 and hunk.get("diff_lines"):
        return build_deleted_context_html(
            hunk,
            repo_path,
            hunk_index,
            total,
            timestamp,
            content_source,
            render_config,
            read_source_lines,
        )

    lines = read_lines(repo_path, hunk["filepath"], hunk["start"], hunk["end"], content_source, read_source_lines)
    # 共通インデントを除去（Codesnap風）
    raw_texts = [t for (_, t) in lines]
    inline_diff_mode = hunk_inline_diff_mode(hunk)
    line_replacements = _line_replacements_by_new_lineno(hunk)
    replacements = line_replacements if inline_diff_mode != "off" else {}
    visible_deletions = _normal_view_visible_deletions(
        hunk,
        line_replacements,
        inline_diff_mode,
    )
    deletion_markers_before, deletion_markers_after = _normal_view_deletion_markers(
        hunk,
        line_replacements,
        visible_deletions,
    )
    inline_diff_max_changed_chars = normalize_inline_diff_max_changed_chars(
        render_config.get("inline_diff_max_changed_chars", INLINE_DIFF_MAX_CHANGED_CHARS)
    )
    inline_added_mutes = hunk_inline_added_mutes(hunk)
    inline_hidden_changes = hunk_inline_hidden_changes(hunk)
    manual_row_highlights = hunk_manual_row_highlights(hunk)
    replacement_texts = [text for _, text in replacements.values()]
    deletion_texts = [deletion["text"] for deletion in visible_deletions]
    stripped_combined, _ = _strip_common_indent_from_lines(
        raw_texts + replacement_texts + deletion_texts
    )
    stripped_texts = stripped_combined[:len(raw_texts)]
    replacement_end = len(raw_texts) + len(replacement_texts)
    stripped_replacements = stripped_combined[len(raw_texts):replacement_end]
    stripped_deletions = stripped_combined[replacement_end:]
    replacement_by_lineno = {
        lineno: (old_lineno, stripped_replacements[idx])
        for idx, (lineno, (old_lineno, _)) in enumerate(replacements.items())
    }
    for deletion, stripped_text in zip(visible_deletions, stripped_deletions):
        deletion["text"] = stripped_text
    lang = detect_language(hunk["filepath"])
    changed_set = set(hunk["changed_lines"])

    rows = []
    next_visible_deletion = 0

    def append_visible_deletion(deletion: dict) -> None:
        old_lineno = int(deletion["old_lineno"])
        row_classes = ["deleted"]
        manual_row_highlight = manual_row_highlights.get(old_lineno)
        if manual_row_highlight:
            row_classes.append(f"manual-row-{manual_row_highlight}")
        rows.append(
            f'<tr class="{" ".join(row_classes)}">'
            '<td class="lineno"></td>'
            '<td class="marker">-</td>'
            f'<td class="code">{escape(deletion["text"])}</td>'
            '</tr>'
        )

    for idx, (lineno, text) in enumerate(lines):
        while (
            next_visible_deletion < len(visible_deletions)
            and visible_deletions[next_visible_deletion]["anchor"] <= lineno
        ):
            append_visible_deletion(visible_deletions[next_visible_deletion])
            next_visible_deletion += 1

        # text は共通インデントを削除したものを使う
        text = stripped_texts[idx]
        is_changed = lineno in changed_set
        inline_rendered = False
        inline_missed = False
        if is_changed and lineno in replacement_by_lineno:
            _, old_text = replacement_by_lineno[lineno]
            inline_html = _inline_diff_html(
                old_text,
                text,
                inline_diff_mode,
                inline_diff_max_changed_chars,
                inline_added_mutes,
                str(lineno),
                inline_hidden_changes,
            )
            if inline_html is not None:
                code_html = inline_html
                inline_rendered = "inline-" in inline_html
            else:
                code_html = escape(text)
                inline_missed = True
        else:
            code_html = escape(text)
        row_classes = []
        if inline_missed or inline_rendered:
            row_classes.append("changed")
        elif is_changed:
            row_classes.append("added")
        if inline_rendered:
            row_classes.append("inline-rendered")
        manual_row_highlight = manual_row_highlights.get(int(lineno))
        if manual_row_highlight:
            row_classes.append(f"manual-row-{manual_row_highlight}")
        row_class = f' class="{" ".join(row_classes)}"' if row_classes else ""
        lineno_classes = ["lineno"]
        if lineno in deletion_markers_before:
            lineno_classes.append("deletion-before")
        if lineno in deletion_markers_after:
            lineno_classes.append("deletion-after")
        marker = "+" if is_changed else " "
        rows.append(
            f'<tr{row_class}>'
            f'<td class="{" ".join(lineno_classes)}">{lineno}</td>'
            f'<td class="marker">{marker}</td>'
            f'<td class="code">{code_html}</td>'
            f'</tr>'
        )

    for deletion in visible_deletions[next_visible_deletion:]:
        append_visible_deletion(deletion)

    meta = f"L{hunk['start']}–{hunk['end']} | {hunk_index}/{total} | {lang}"
    return _compose_hunk_html(rows, hunk, meta, lang, timestamp, render_config)


def _deleted_diff_blocks(hunk: dict) -> list[dict]:
    raw_blocks = hunk.get("diff_blocks")
    using_hunk_fallback = not isinstance(raw_blocks, list) or not raw_blocks
    if using_hunk_fallback:
        raw_blocks = [hunk]

    blocks = []
    for block in raw_blocks:
        if not isinstance(block, dict):
            continue
        deleted_texts = [
            raw[1:]
            for raw in block.get("diff_lines", [])
            if raw.startswith("-") and not raw.startswith("---")
        ]
        if deleted_texts:
            if using_hunk_fallback:
                anchor = hunk.get(
                    "orig_start",
                    hunk.get("default_start", hunk.get("start", 1)),
                )
            else:
                anchor = block.get(
                    "start",
                    hunk.get("orig_start", hunk.get("default_start", hunk.get("start", 1))),
                )
            blocks.append({
                "anchor": int(anchor),
                "old_start": int(block.get("old_start", hunk.get("old_start", hunk.get("start", 1)))),
                "texts": deleted_texts,
            })
    return sorted(blocks, key=lambda block: (block["anchor"], block["old_start"]))


def build_deleted_context_html(
    hunk: dict,
    repo_path: str,
    hunk_index: int,
    total: int,
    timestamp: str,
    content_source: dict | None = None,
    config: dict | None = None,
    read_source_lines: Callable[[str, str, dict], list[str]] | None = None,
) -> str:
    render_config = config or _default_render_config()
    lines = read_lines(repo_path, hunk["filepath"], hunk["start"], hunk["end"], content_source, read_source_lines)
    visible_start = int(hunk.get("start", 1))
    visible_end = int(hunk.get("end", visible_start))
    deleted_blocks = [
        block
        for block in _deleted_diff_blocks(hunk)
        if visible_start <= block["anchor"] <= visible_end
    ]
    deleted_texts = [text for block in deleted_blocks for text in block["texts"]]
    combined_texts = [t for (_, t) in lines] + deleted_texts
    stripped_combined, _ = _strip_common_indent_from_lines(combined_texts)
    stripped_lines = stripped_combined[:len(lines)]
    stripped_deleted = stripped_combined[len(lines):]

    text_offset = 0
    for block in deleted_blocks:
        text_count = len(block["texts"])
        block["texts"] = stripped_deleted[text_offset:text_offset + text_count]
        text_offset += text_count

    lang = detect_language(hunk["filepath"])
    manual_row_highlights = hunk_manual_row_highlights(hunk)
    rows = []
    next_deleted_block = 0

    def append_deleted_rows(block: dict) -> None:
        old_ln = block["old_start"]
        for text in block["texts"]:
            row_classes = ["deleted"]
            manual_row_highlight = manual_row_highlights.get(old_ln)
            if manual_row_highlight:
                row_classes.append(f"manual-row-{manual_row_highlight}")
            rows.append(
                f'<tr class="{" ".join(row_classes)}">'
                '<td class="lineno"></td>'
                '<td class="marker">-</td>'
                f'<td class="code">{escape(text)}</td>'
                '</tr>'
            )
            old_ln += 1

    for idx, (lineno, text) in enumerate(lines):
        while (
            next_deleted_block < len(deleted_blocks)
            and deleted_blocks[next_deleted_block]["anchor"] < lineno
        ):
            append_deleted_rows(deleted_blocks[next_deleted_block])
            next_deleted_block += 1

        escaped = escape(stripped_lines[idx])
        manual_row_highlight = manual_row_highlights.get(int(lineno))
        row_class = f' class="manual-row-{manual_row_highlight}"' if manual_row_highlight else ""
        rows.append(
            f'<tr{row_class}>'
            f'<td class="lineno">{lineno}</td>'
            '<td class="marker"> </td>'
            f'<td class="code">{escaped}</td>'
            '</tr>'
        )

    for block in deleted_blocks[next_deleted_block:]:
        append_deleted_rows(block)

    meta = f"L{hunk['start']}–{hunk['end']} | {hunk_index}/{total} | {lang}"
    return _compose_hunk_html(rows, hunk, meta, lang, timestamp, render_config)


def _visible_diff_rows(hunk: dict) -> list[tuple[str, int | None, int | None]]:
    old_ln = int(hunk.get("old_start", hunk.get("default_start", hunk.get("start", 1))))
    new_ln = int(hunk.get("default_start", hunk.get("start", 1)))
    original_end = int(hunk.get("default_end", hunk.get("end", new_ln)))
    visible_start = int(hunk.get("start", new_ln))
    visible_end = int(hunk.get("end", original_end))
    rows: list[tuple[str, int | None, int | None]] = []
    previous_visible = False

    for raw in hunk.get("diff_lines", []):
        if not raw:
            continue
        if raw.startswith("\\"):
            if previous_visible:
                rows.append((raw, None, None))
            continue

        prefix = raw[0]
        if prefix == "+":
            visible = visible_start <= new_ln <= visible_end
            if visible:
                rows.append((raw, None, new_ln))
            new_ln += 1
        elif prefix == "-":
            anchor = min(new_ln, original_end)
            visible = visible_start <= anchor <= visible_end
            if visible:
                rows.append((raw, old_ln, None))
            old_ln += 1
        else:
            visible = visible_start <= new_ln <= visible_end
            if visible:
                rows.append((raw, old_ln, new_ln))
            old_ln += 1
            new_ln += 1
        previous_visible = visible

    return rows


def build_patch_html(hunk: dict, hunk_index: int, total: int, timestamp: str, config: dict | None = None) -> str:
    render_config = config or _default_render_config()
    lang = detect_language(hunk["filepath"])
    rows = []
    visible_rows = _visible_diff_rows(hunk)
    visible_diff_lines = [raw for raw, _, _ in visible_rows]

    stripped_texts_by_index = _stripped_diff_texts_by_index(
        visible_diff_lines,
        lambda raw: not raw.startswith("\\"),
    )

    for idx, (raw, old_ln, new_ln) in enumerate(visible_rows):
        if raw.startswith("\\"):
            note = escape(raw)
            rows.append(
                '<tr class="note">'
                '<td class="lineno old"></td>'
                '<td class="lineno new"></td>'
                '<td class="marker">\\</td>'
                f'<td class="code">{note}</td>'
                '</tr>'
            )
            continue

        prefix = raw[0]
        # 可能であれば共通インデントを削除したテキストを使う
        text = raw[1:]
        stripped = stripped_texts_by_index.get(idx)
        if stripped is not None:
            text = stripped
        escaped = escape(text)

        if prefix == "+":
            rows.append(
                '<tr class="added">'
                '<td class="lineno old"></td>'
                f'<td class="lineno new">{new_ln}</td>'
                '<td class="marker">+</td>'
                f'<td class="code">{escaped}</td>'
                '</tr>'
            )
        elif prefix == "-":
            rows.append(
                '<tr class="deleted">'
                f'<td class="lineno old">{old_ln}</td>'
                '<td class="lineno new"></td>'
                '<td class="marker">-</td>'
                f'<td class="code">{escaped}</td>'
                '</tr>'
            )
        else:
            rows.append(
                '<tr>'
                f'<td class="lineno old">{old_ln}</td>'
                f'<td class="lineno new">{new_ln}</td>'
                '<td class="marker"> </td>'
                f'<td class="code">{escaped}</td>'
                '</tr>'
            )

    meta = f"-{hunk.get('old_start', hunk['start'])} +{hunk['start']} | {hunk_index}/{total} | {lang} | patch"
    return _compose_hunk_html(rows, hunk, meta, lang, timestamp, render_config)


def _build_filtered_patch_html(
    hunk: dict,
    hunk_index: int,
    total: int,
    timestamp: str,
    change_type: str,
    config: dict | None = None,
) -> str:
    if change_type not in {"added", "deleted"}:
        raise ValueError("change_type は added, deleted のいずれかを指定してください")

    render_config = config or _default_render_config()
    lang = detect_language(hunk["filepath"])
    rows = []
    visible_rows = _visible_diff_rows(hunk)
    visible_diff_lines = [raw for raw, _, _ in visible_rows]
    hidden_prefix = "-" if change_type == "added" else "+"
    visible_prefix = "+" if change_type == "added" else "-"

    stripped_texts_by_index = _stripped_diff_texts_by_index(
        visible_diff_lines,
        lambda raw: not raw.startswith("\\") and not raw.startswith(hidden_prefix),
    )

    for idx, (raw, old_ln, new_ln) in enumerate(visible_rows):
        if raw.startswith("\\"):
            note = escape(raw)
            rows.append(
                '<tr class="note">'
                '<td class="lineno"></td>'
                '<td class="marker">\\</td>'
                f'<td class="code">{note}</td>'
                '</tr>'
            )
            continue

        prefix = raw[0]
        if prefix == hidden_prefix:
            continue

        text = stripped_texts_by_index.get(idx, raw[1:])
        escaped = escape(text)

        if prefix == visible_prefix:
            row_class = "added" if change_type == "added" else "deleted"
            line_number = new_ln if change_type == "added" else ""
            rows.append(
                f'<tr class="{row_class}">'
                f'<td class="lineno">{line_number}</td>'
                f'<td class="marker">{visible_prefix}</td>'
                f'<td class="code">{escaped}</td>'
                '</tr>'
            )
        else:
            line_number = new_ln if change_type == "added" else old_ln
            rows.append(
                '<tr>'
                f'<td class="lineno">{line_number}</td>'
                '<td class="marker"> </td>'
                f'<td class="code">{escaped}</td>'
                '</tr>'
            )

    if change_type == "added":
        meta = f"+{hunk['start']} | {hunk_index}/{total} | {lang} | 追加"
    else:
        meta = f"-{hunk.get('old_start', hunk['start'])} | {hunk_index}/{total} | {lang} | 削除"
    return _compose_hunk_html(rows, hunk, meta, lang, timestamp, render_config)


def build_added_patch_html(
    hunk: dict,
    hunk_index: int,
    total: int,
    timestamp: str,
    config: dict | None = None,
) -> str:
    return _build_filtered_patch_html(
        hunk,
        hunk_index,
        total,
        timestamp,
        "added",
        config,
    )


def build_deleted_patch_html(
    hunk: dict,
    hunk_index: int,
    total: int,
    timestamp: str,
    config: dict | None = None,
) -> str:
    return _build_filtered_patch_html(
        hunk,
        hunk_index,
        total,
        timestamp,
        "deleted",
        config,
    )


# ================================================================
# PNG 出力
# ================================================================
