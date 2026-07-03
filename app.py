#!/usr/bin/env python3
"""
diff2png / app.py
Flask + Playwright によるエビデンス用 git diff スクリーンショットツール
"""

import hashlib
import difflib
import os
import re
import subprocess
import sys
import webbrowser
from copy import deepcopy
from html import escape
from datetime import datetime
from pathlib import Path
from threading import Timer
from uuid import uuid4

from flask import Flask, jsonify, render_template, request, send_from_directory

# ---- 設定 ----
CONTEXT_LINES = 5
MERGE_THRESHOLD = 8
APP_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR_NAME = "diff_screenshots"
OUTPUT_DIR = APP_ROOT / OUTPUT_DIR_NAME
HTML_WIDTH = 960
DIFF_MODE = "file"
# 背景モード: 'normal' | 'no_bg_footer' | 'transparent_no_footer'
BACKGROUND_MODE = 'normal'
INLINE_DIFF_DEFAULT_MODE = "full"

app = Flask(__name__)

# --- パフォーマンス用キャッシュ / コンパイル済み正規表現 ---
FILE_CONTENT_CACHE: dict[str, list[str]] = {}
ANALYSIS_SESSIONS: dict[str, dict] = {}
MAX_ANALYSIS_SESSIONS = 20
GIT_TIMEOUT_SECONDS = 30
INLINE_DIFF_MAX_CHANGED_CHARS = 40
INLINE_DIFF_MAX_CHANGED_CHARS_LIMIT = 500
INLINE_DIFF_MIN_SIMILARITY = 0.5
INLINE_DIFF_TAG_BLOCK_MIN_SIMILARITY = 0.8
INLINE_DIFF_MODES = {"full", "new", "off"}
_OLD_RE = re.compile(r"^--- (?:a/(.+)|/dev/null)$")
_NEW_RE = re.compile(r"^\+\+\+ (?:b/(.+)|/dev/null)$")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_INDENT_RE = re.compile(r"^[ \t]*")
_FILENAME_UNSAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_FILENAME_REPEAT_UNDERSCORE_RE = re.compile(r"_+")
_OUTPUT_PNG_RE = re.compile(r"^\d{8}_\d{6}_\d{3}_.+_L\d+\.png$", re.IGNORECASE)
_INLINE_DIFF_TOKEN_RE = re.compile(r"\w+|\s+|[^\w\s]+", re.UNICODE)
_HTML_TAG_NAME_RE = re.compile(r"(</?\s*)[A-Za-z][\w:-]*")
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


# ================================================================
# git / diff ユーティリティ
# ================================================================

def run_git(repo_path: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=repo_path,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"git コマンドがタイムアウトしました: git {' '.join(args)}") from e


def get_diff(
    repo_path: str,
    context_lines: int | None = None,
    merge_threshold: int | None = None,
    source_mode: str = "worktree",
    base_ref: str | None = None,
    target_ref: str | None = None,
) -> str:
    if source_mode == "worktree":
        cmd = ["git", "diff", "HEAD"]
    elif source_mode == "staged":
        # インデックス（ステージング済み）のみを表示
        cmd = ["git", "diff", "--staged"]
    elif source_mode == "commit":
        commit = (target_ref or "").strip()
        if not commit:
            raise RuntimeError("コミットを選択してください")
        cmd = ["git", "diff", f"{commit}^", commit]
    elif source_mode == "range":
        base = (base_ref or "").strip()
        target = (target_ref or "").strip()
        if not base or not target:
            raise RuntimeError("比較元/比較先コミットを選択してください")
        cmd = ["git", "diff", base, target]
    else:
        raise RuntimeError("不正な差分ソースです")

    if context_lines is not None:
        cmd.append(f"-U{max(0, int(context_lines))}")
    if merge_threshold is not None:
        cmd.append(f"--inter-hunk-context={max(0, int(merge_threshold))}")

    result = run_git(repo_path, cmd[1:])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout


def list_commits(repo_path: str, limit: int = 80) -> list[dict]:
    result = run_git(repo_path, ["log", f"-n{max(1, min(limit, 200))}", "--pretty=format:%H\t%h\t%s"])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())

    commits: list[dict] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        full_hash, short_hash, subject = parts
        commits.append({
            "hash": full_hash,
            "short": short_hash,
            "subject": subject,
        })
    return commits


def is_git_repo(repo_path: str) -> bool:
    result = run_git(repo_path, ["rev-parse", "--is-inside-work-tree"])
    return result.returncode == 0 and result.stdout.strip() == "true"


def get_git_top_level(repo_path: str) -> Path:
    result = run_git(repo_path, ["rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "Gitリポジトリルートの解決に失敗しました")

    top_level = result.stdout.strip()
    if not top_level:
        raise ValueError("Gitリポジトリルートの解決に失敗しました")

    try:
        return Path(top_level).resolve(strict=True)
    except Exception as e:
        raise ValueError(f"Gitリポジトリルートの解決に失敗しました: {e}") from e


def resolve_repo_path(repo_path: str) -> Path:
    p = Path(repo_path).expanduser()
    try:
        rp = p.resolve(strict=True)
    except Exception as e:
        raise ValueError(f"リポジトリパスの解決に失敗しました: {e}")
    if not rp.is_dir():
        raise ValueError("リポジトリパスが無効です")
    if not is_git_repo(str(rp)):
        raise ValueError("指定パスはGitリポジトリではありません")
    return get_git_top_level(str(rp))


def resolve_path_within(base_dir: Path, relative_path: str) -> Path:
    target = (base_dir / relative_path).resolve()
    try:
        target.relative_to(base_dir)
    except ValueError as e:
        raise ValueError("パスがリポジトリ外を指しています") from e
    return target


def clear_repo_file_cache(repo_path: str) -> None:
    prefix = f"{Path(repo_path).resolve()}|"
    for key in list(FILE_CONTENT_CACHE):
        if key.startswith(prefix):
            FILE_CONTENT_CACHE.pop(key, None)


def cache_key_for_file(repo_path: str, filepath: str, content_source: dict) -> str:
    source_type = content_source.get("type", "worktree")
    if source_type == "worktree":
        safe_path = resolve_path_within(Path(repo_path), filepath)
        try:
            stat = safe_path.stat()
            version = f"{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            version = "missing"
    elif source_type == "index":
        result = run_git(repo_path, ["ls-files", "-s", "--", filepath])
        version = result.stdout.strip() if result.returncode == 0 else "index"
    else:
        version = str(content_source.get("ref", "HEAD"))
    return f"{Path(repo_path).resolve()}|{source_type}|{version}|{filepath}"


def read_source_lines(repo_path: str, filepath: str, content_source: dict) -> list[str]:
    key = cache_key_for_file(repo_path, filepath, content_source)
    if key in FILE_CONTENT_CACHE:
        return FILE_CONTENT_CACHE[key]

    source_type = content_source.get("type", "worktree")
    if source_type == "worktree":
        safe_path = resolve_path_within(Path(repo_path), filepath)
        text = safe_path.read_text(encoding="utf-8", errors="replace")
    elif source_type == "index":
        result = run_git(repo_path, ["show", f":{filepath}"])
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        text = result.stdout
    elif source_type == "ref":
        ref = str(content_source.get("ref", "")).strip()
        if not ref:
            raise RuntimeError("参照先コミットが不正です")
        result = run_git(repo_path, ["show", f"{ref}:{filepath}"])
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        text = result.stdout
    else:
        raise RuntimeError("不正なファイル内容ソースです")

    lines = text.splitlines()
    FILE_CONTENT_CACHE[key] = lines
    return lines


def content_source_for_diff(source_mode: str, target_ref: str | None) -> dict:
    if source_mode == "staged":
        return {"type": "index"}
    if source_mode in ("commit", "range"):
        return {"type": "ref", "ref": (target_ref or "").strip()}
    return {"type": "worktree"}


def hunk_inline_diff_mode(hunk: dict) -> str:
    mode = str(hunk.get("inline_diff_mode", "")).strip().lower()
    if mode in INLINE_DIFF_MODES:
        return mode
    return "full" if bool(hunk.get("inline_diff_enabled", True)) else "off"


def normalize_inline_diff_mode(value: str | None, default: str = "full") -> str:
    mode = str(value or default).strip().lower()
    if mode not in INLINE_DIFF_MODES:
        raise ValueError("inline_diff_default_mode は full, new, off のいずれかを指定してください")
    return mode


def normalize_inline_diff_max_changed_chars(value: int | str | None) -> int:
    return max(0, min(INLINE_DIFF_MAX_CHANGED_CHARS_LIMIT, int(value)))


def make_hunk_summary(hunk: dict) -> dict:
    start = int(hunk.get("start", 1))
    end = int(hunk.get("end", start))
    default_start = int(hunk.get("default_start", start))
    default_end = int(hunk.get("default_end", end))
    inline_diff_mode = hunk_inline_diff_mode(hunk)
    inline_diff_enabled = inline_diff_mode != "off"
    return {
        "filepath": hunk.get("filepath", ""),
        "start": start,
        "end": end,
        "default_start": default_start,
        "default_end": default_end,
        "range_adjusted": start != default_start or end != default_end,
        "inline_diff_enabled": inline_diff_enabled,
        "inline_diff_mode": inline_diff_mode,
        "old_start": hunk.get("old_start"),
        "changed_count": hunk.get("changed_count", len(hunk.get("changed_lines", []))),
        "added_count": hunk.get("added_count", 0),
        "deleted_count": hunk.get("deleted_count", 0),
    }


def current_config_snapshot() -> dict:
    return {
        "context_lines": CONTEXT_LINES,
        "merge_threshold": MERGE_THRESHOLD,
        "html_width": HTML_WIDTH,
        "output_dir": OUTPUT_DIR_NAME,
        "diff_mode": DIFF_MODE,
        "background_mode": BACKGROUND_MODE,
        "inline_diff_default_mode": INLINE_DIFF_DEFAULT_MODE,
        "inline_diff_max_changed_chars": INLINE_DIFF_MAX_CHANGED_CHARS,
    }


def session_config(session: dict) -> dict:
    config = current_config_snapshot()
    stored = session.get("config")
    if isinstance(stored, dict):
        config.update(stored)
    elif "diff_mode" in session:
        config["diff_mode"] = session["diff_mode"]
    return config


def finalize_hunks(raw_hunks: list[dict], repo_path: str, content_source: dict, config: dict) -> list[dict]:
    hunks = deepcopy(raw_hunks)
    if config.get("diff_mode") == "file":
        hunks = expand_and_merge(
            hunks,
            repo_path,
            int(config.get("context_lines", CONTEXT_LINES)),
            int(config.get("merge_threshold", MERGE_THRESHOLD)),
            content_source,
        )
        for h in hunks:
            h["changed_count"] = len(h.get("changed_lines", []))
    else:
        for h in hunks:
            h["changed_count"] = int(h.get("added_count", 0)) + int(h.get("deleted_count", 0))
    for h in hunks:
        h["default_start"] = int(h.get("start", 1))
        h["default_end"] = int(h.get("end", h.get("start", 1)))
        inline_diff_mode = normalize_inline_diff_mode(
            str(config.get("inline_diff_default_mode", INLINE_DIFF_DEFAULT_MODE)),
            INLINE_DIFF_DEFAULT_MODE,
        )
        h["inline_diff_enabled"] = inline_diff_mode != "off"
        h["inline_diff_mode"] = inline_diff_mode
    return hunks


def _clamp_hunk_range(start: int, end: int, total_lines: int) -> tuple[int, int]:
    total = max(1, int(total_lines))
    start = max(1, min(int(start), total))
    end = max(start, min(int(end), total))
    return start, end


def _required_hunk_range(hunk: dict, total_lines: int) -> tuple[int, int]:
    anchors: list[int] = []
    for lineno in hunk.get("changed_lines", []):
        try:
            anchors.append(int(lineno))
        except (TypeError, ValueError):
            continue

    for key in ("orig_start", "orig_end"):
        if key not in hunk:
            continue
        try:
            anchors.append(int(hunk[key]))
        except (TypeError, ValueError):
            continue

    if not anchors:
        try:
            anchors.append(int(hunk.get("default_start", hunk.get("start", 1))))
        except (TypeError, ValueError):
            anchors.append(1)

    return _clamp_hunk_range(min(anchors), max(anchors), total_lines)


def adjust_hunk_range(
    hunk: dict,
    repo_path: str,
    content_source: dict,
    action: str,
    step: int = 1,
) -> dict:
    try:
        total_lines = len(read_source_lines(repo_path, hunk["filepath"], content_source))
    except Exception:
        total_lines = max(int(hunk.get("end", 1)), int(hunk.get("default_end", 1)), 1)

    start, end = _clamp_hunk_range(
        int(hunk.get("start", 1)),
        int(hunk.get("end", hunk.get("start", 1))),
        total_lines,
    )
    default_start, default_end = _clamp_hunk_range(
        int(hunk.get("default_start", start)),
        int(hunk.get("default_end", end)),
        total_lines,
    )
    required_start, required_end = _required_hunk_range(hunk, total_lines)
    delta = max(1, int(step))

    if action == "expand_up":
        next_start = max(1, start - delta)
        next_end = end
    elif action == "shrink_up":
        next_start = min(start + delta, required_start) if start < required_start else start
        next_end = end
    elif action == "expand_down":
        next_start = start
        next_end = min(total_lines, end + delta)
    elif action == "shrink_down":
        next_start = start
        next_end = max(end - delta, required_end) if end > required_end else end
    elif action == "reset":
        next_start = default_start
        next_end = default_end
    else:
        raise ValueError("不正な調整操作です")

    next_start, next_end = _clamp_hunk_range(next_start, next_end, total_lines)
    hunk["start"] = next_start
    hunk["end"] = next_end
    hunk["default_start"] = default_start
    hunk["default_end"] = default_end
    return make_hunk_summary(hunk)


def create_analysis_session(
    repo_path: str,
    raw_hunks: list[dict],
    hunks: list[dict],
    content_source: dict,
    config: dict,
) -> str:
    analysis_id = uuid4().hex
    ANALYSIS_SESSIONS[analysis_id] = {
        "repo_path": str(Path(repo_path).resolve()),
        "raw_hunks": deepcopy(raw_hunks),
        "hunks": hunks,
        "content_source": content_source,
        "config": deepcopy(config),
        "diff_mode": config.get("diff_mode", DIFF_MODE),
    }
    while len(ANALYSIS_SESSIONS) > MAX_ANALYSIS_SESSIONS:
        oldest = next(iter(ANALYSIS_SESSIONS))
        ANALYSIS_SESSIONS.pop(oldest, None)
    return analysis_id


def get_analysis_session(analysis_id: str, repo_path: str | None = None) -> dict:
    session = ANALYSIS_SESSIONS.get((analysis_id or "").strip())
    if not session:
        raise ValueError("解析結果が見つかりません。再解析してください")
    if repo_path:
        requested = str(Path(repo_path).expanduser().resolve())
        if requested != session["repo_path"]:
            raise ValueError("解析結果とリポジトリパスが一致しません")
    return session


def parse_output_dir(value: str) -> tuple[str, Path]:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("output_dir は空にできません")

    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve()
        output_dir_name = str(resolved)
    else:
        resolved = (APP_ROOT / candidate).resolve()
        try:
            resolved.relative_to(APP_ROOT)
        except ValueError as e:
            raise ValueError("相対パスの output_dir はアプリ配下を指定してください") from e
        output_dir_name = candidate.as_posix()

    if resolved == APP_ROOT:
        raise ValueError("アプリルートは出力フォルダにできません")
    if resolved.parent == resolved:
        raise ValueError("ドライブまたはファイルシステムのルートは出力フォルダにできません")

    return output_dir_name, resolved


def sanitize_filename_component(value: str, max_length: int = 120) -> str:
    raw = str(value or "").strip()
    safe = _FILENAME_UNSAFE_RE.sub("_", raw)
    safe = _FILENAME_REPEAT_UNDERSCORE_RE.sub("_", safe).strip(" ._")
    if not safe:
        safe = "file"

    base_name = safe.split(".", 1)[0].upper()
    if base_name in _WINDOWS_RESERVED_NAMES:
        safe = f"_{safe}"

    if len(safe) > max_length:
        digest = hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:10]
        safe = f"{safe[:max_length - len(digest) - 1].rstrip(' ._')}_{digest}"

    return safe or "file"


def output_dir_from_request(data: dict) -> tuple[str, Path]:
    return parse_output_dir(str(data.get("output_dir", OUTPUT_DIR_NAME)))


def open_directory(path: Path) -> None:
    resolved = path.resolve()
    if sys.platform.startswith("win"):
        os.startfile(str(resolved))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(resolved)])
    else:
        subprocess.Popen(["xdg-open", str(resolved)])


def choose_directory(title: str, initialdir: Path | None = None) -> str:
    root = None
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            title=title,
            initialdir=str(initialdir or APP_ROOT),
        )
        return selected
    except Exception as e:
        raise RuntimeError(f"フォルダ選択に失敗しました: {e}") from e
    finally:
        if root is not None:
            root.destroy()


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


def _strip_common_indent_from_lines(lines: list[str]) -> tuple[list[str], int]:
    """与えられたテキスト行リストから共通の先頭インデントを除去して返す。
    改行が空白のみの行は無視して最小インデント幅を決定する。
    戻り値は (新しい行リスト, 削除したインデント幅) 。
    """
    min_indent = None
    for t in lines:
        if t is None:
            continue
        if t.strip() == "":
            continue
        m = _INDENT_RE.match(t)
        if not m:
            continue
        indent_len = len(m.group(0))
        if min_indent is None or indent_len < min_indent:
            min_indent = indent_len

    if min_indent is None or min_indent == 0:
        return lines, 0

    new = [(s[min_indent:] if s is not None and len(s) >= min_indent else (s or "")) for s in lines]
    return new, min_indent


def expand_and_merge(
    hunks: list[dict],
    repo_path: str,
    context: int,
    merge_thresh: int,
    content_source: dict | None = None,
) -> list[dict]:
    by_file: dict[str, list[dict]] = {}
    for h in hunks:
        by_file.setdefault(h["filepath"], []).append(h)

    result = []
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


def read_lines(
    repo_path: str,
    filepath: str,
    start: int,
    end: int,
    content_source: dict | None = None,
) -> list[tuple[int, str]]:
    try:
        lines = read_source_lines(repo_path, filepath, content_source or {"type": "worktree"})
    except Exception as e:
        return [(start, f"# 読み込みエラー: {e}")]
    return [(i + 1, lines[i]) for i in range(start - 1, min(end, len(lines)))]


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
tr.changed td.lineno{{background:#fef9c3;color:#78716c}}
td.lineno.new{{border-right:none}}
td.marker{{width:18px;text-align:center;color:#64748b;font-weight:bold}}
tr.added td.marker{{color:#16a34a}}
tr.deleted td.marker{{color:#dc2626}}
td.code{{padding:2px 8px;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word}}
span.inline-added{{background:#bbf7d0;color:#14532d;border-radius:3px;padding:0 2px}}
span.inline-deleted{{background:#fecaca;color:#991b1b;border-radius:3px;padding:0 2px;text-decoration:line-through}}
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
        filepath=escape(filepath),
        meta=escape(meta),
        rows=rows_html,
    )
    if show_footer:
        html += footer_template.format(timestamp=escape(timestamp), diff_cmd=escape(diff_cmd))
    html += "</body></html>"
    return html


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

    deleted_block: list[tuple[int, str]] = []
    added_block: list[tuple[int, str]] = []

    def similarity(old_text: str, new_text: str) -> float:
        old_tokens = _INLINE_DIFF_TOKEN_RE.findall(old_text)
        new_tokens = _INLINE_DIFF_TOKEN_RE.findall(new_text)
        token_score = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False).ratio()
        char_score = difflib.SequenceMatcher(None, old_text, new_text, autojunk=False).ratio()
        return min(token_score, char_score)

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

    def block_allows_html_tag_pairing() -> bool:
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

    def flush_block() -> None:
        nonlocal deleted_block, added_block
        allow_html_tag_pairing = block_allows_html_tag_pairing()

        def candidate_score(old_text: str, new_text: str) -> float:
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
        deleted_block = []
        added_block = []

    for raw in diff_lines:
        if not raw or raw.startswith("\\"):
            continue

        if raw.startswith("-") and not raw.startswith("---"):
            if added_block:
                flush_block()
            deleted_block.append((old_ln, raw[1:]))
            old_ln += 1
        elif raw.startswith("+") and not raw.startswith("+++"):
            if added_line_index < len(changed_line_numbers):
                added_lineno = changed_line_numbers[added_line_index]
            else:
                added_lineno = new_ln
            added_line_index += 1
            added_block.append((added_lineno, raw[1:]))
            new_ln = added_lineno + 1
        elif raw.startswith(" "):
            flush_block()
            old_ln += 1
            new_ln += 1
        else:
            flush_block()

    flush_block()
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
            int(hunk.get("start", 1)),
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
) -> str | None:
    parts = []
    show_old = mode == "full"
    changed_chars_limit = (
        INLINE_DIFF_MAX_CHANGED_CHARS
        if max_changed_chars is None
        else normalize_inline_diff_max_changed_chars(max_changed_chars)
    )
    old_tokens = _INLINE_DIFF_TOKEN_RE.findall(old_text)
    new_tokens = _INLINE_DIFF_TOKEN_RE.findall(new_text)
    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
    changed_chars = 0
    opcodes = matcher.get_opcodes()
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        changed_chars += len("".join(old_tokens[i1:i2])) + len("".join(new_tokens[j1:j2]))
    if changed_chars > changed_chars_limit:
        return None

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
            parts.append(escape(new_part) if is_leading_indent else f'<span class="inline-added">{escape(new_part)}</span>')
        elif tag == "delete":
            if show_old and not is_leading_indent:
                parts.append(f'<span class="inline-deleted">{escape(old_part)}</span>')
        elif tag == "replace":
            if is_leading_indent:
                parts.append(escape(new_part))
            else:
                if show_old:
                    parts.append(f'<span class="inline-deleted">{escape(old_part)}</span>')
                parts.append(f'<span class="inline-added">{escape(new_part)}</span>')
    return "".join(parts)


def build_code_html(
    hunk: dict,
    repo_path: str,
    hunk_index: int,
    total: int,
    timestamp: str,
    content_source: dict | None = None,
    config: dict | None = None,
) -> str:
    render_config = config or current_config_snapshot()
    if render_config.get("diff_mode") == "patch":
        return build_patch_html(hunk, hunk_index, total, timestamp, render_config)

    if len(hunk.get("changed_lines", [])) == 0 and int(hunk.get("deleted_count", 0)) > 0 and hunk.get("diff_lines"):
        return build_deleted_context_html(
            hunk,
            repo_path,
            hunk_index,
            total,
            timestamp,
            content_source,
            render_config,
        )

    lines = read_lines(repo_path, hunk["filepath"], hunk["start"], hunk["end"], content_source)
    # 共通インデントを除去（Codesnap風）
    raw_texts = [t for (_, t) in lines]
    inline_diff_mode = hunk_inline_diff_mode(hunk)
    replacements = _line_replacements_by_new_lineno(hunk) if inline_diff_mode != "off" else {}
    inline_diff_max_changed_chars = normalize_inline_diff_max_changed_chars(
        render_config.get("inline_diff_max_changed_chars", INLINE_DIFF_MAX_CHANGED_CHARS)
    )
    replacement_texts = [text for _, text in replacements.values()]
    stripped_combined, _ = _strip_common_indent_from_lines(raw_texts + replacement_texts)
    stripped_texts = stripped_combined[:len(raw_texts)]
    stripped_replacements = stripped_combined[len(raw_texts):]
    replacement_by_lineno = {
        lineno: (old_lineno, stripped_replacements[idx])
        for idx, (lineno, (old_lineno, _)) in enumerate(replacements.items())
    }
    lang = detect_language(hunk["filepath"])
    changed_set = set(hunk["changed_lines"])

    rows = []
    for idx, (lineno, text) in enumerate(lines):
        # text は共通インデントを削除したものを使う
        text = stripped_texts[idx]
        is_changed = lineno in changed_set
        if is_changed and lineno in replacement_by_lineno:
            _, old_text = replacement_by_lineno[lineno]
            code_html = _inline_diff_html(
                old_text,
                text,
                inline_diff_mode,
                inline_diff_max_changed_chars,
            ) or escape(text)
        else:
            code_html = escape(text)
        row_class = ' class="changed"' if is_changed else ""
        marker = "+" if is_changed else " "
        rows.append(
            f'<tr{row_class}>'
            f'<td class="lineno">{lineno}</td>'
            f'<td class="marker">{marker}</td>'
            f'<td class="code">{code_html}</td>'
            f'</tr>'
        )

    diff_cmd = hunk.get("diff_cmd", "git diff HEAD")
    background_mode = str(render_config.get("background_mode", BACKGROUND_MODE))
    html_width = int(render_config.get("html_width", HTML_WIDTH))
    bg_color = 'transparent' if background_mode != 'normal' else '#fff'
    show_footer = False if background_mode == 'transparent_no_footer' else True

    meta = f"L{hunk['start']}–{hunk['end']} | {hunk_index}/{total} | {lang}"
    html = _compose_html(''.join(rows), hunk['filepath'], meta, lang, bg_color,
                         html_width, show_footer, timestamp, diff_cmd)
    return html


def build_deleted_context_html(
    hunk: dict,
    repo_path: str,
    hunk_index: int,
    total: int,
    timestamp: str,
    content_source: dict | None = None,
    config: dict | None = None,
) -> str:
    render_config = config or current_config_snapshot()
    lines = read_lines(repo_path, hunk["filepath"], hunk["start"], hunk["end"], content_source)
    deleted_texts = [
        raw[1:]
        for raw in hunk.get("diff_lines", [])
        if raw.startswith("-") and not raw.startswith("---")
    ]
    combined_texts = [t for (_, t) in lines] + deleted_texts
    stripped_combined, _ = _strip_common_indent_from_lines(combined_texts)
    stripped_lines = stripped_combined[:len(lines)]
    stripped_deleted = stripped_combined[len(lines):]

    lang = detect_language(hunk["filepath"])
    deletion_anchor = int(hunk.get("orig_start", hunk.get("start", 1)))
    rows = []
    deleted_inserted = False

    def append_deleted_rows() -> None:
        nonlocal deleted_inserted
        if deleted_inserted:
            return
        old_ln = int(hunk.get("old_start", deletion_anchor))
        for text in stripped_deleted:
            rows.append(
                '<tr class="deleted">'
                f'<td class="lineno">{old_ln}</td>'
                '<td class="marker">-</td>'
                f'<td class="code">{escape(text)}</td>'
                '</tr>'
            )
            old_ln += 1
        deleted_inserted = True

    if deletion_anchor < int(hunk["start"]):
        append_deleted_rows()

    for idx, (lineno, text) in enumerate(lines):
        if not deleted_inserted and lineno > deletion_anchor:
            append_deleted_rows()

        escaped = escape(stripped_lines[idx])
        rows.append(
            '<tr>'
            f'<td class="lineno">{lineno}</td>'
            '<td class="marker"> </td>'
            f'<td class="code">{escaped}</td>'
            '</tr>'
        )

    if not deleted_inserted:
        append_deleted_rows()

    diff_cmd = hunk.get("diff_cmd", "git diff HEAD")
    background_mode = str(render_config.get("background_mode", BACKGROUND_MODE))
    html_width = int(render_config.get("html_width", HTML_WIDTH))
    bg_color = 'transparent' if background_mode != 'normal' else '#fff'
    show_footer = False if background_mode == 'transparent_no_footer' else True

    meta = f"L{hunk['start']}–{hunk['end']} | {hunk_index}/{total} | {lang}"
    html = _compose_html(''.join(rows), hunk['filepath'], meta, lang, bg_color,
                         html_width, show_footer, timestamp, diff_cmd)
    return html


def build_patch_html(hunk: dict, hunk_index: int, total: int, timestamp: str, config: dict | None = None) -> str:
    render_config = config or current_config_snapshot()
    lang = detect_language(hunk["filepath"])
    old_ln = int(hunk.get("old_start", hunk["start"]))
    new_ln = int(hunk.get("start", 1))
    rows = []

    # diff_lines の各行から先頭の記号を除いたテキスト部分を収集し、共通インデントを削除する
    texts_for_indent = []
    for raw in hunk.get("diff_lines", []):
        if not raw or raw.startswith("\\"):
            continue
        part = raw[1:]
        if part.strip() == "":
            continue
        texts_for_indent.append(part)

    stripped_texts_by_index: dict[int, str] = {}
    if texts_for_indent:
        new_texts, _ = _strip_common_indent_from_lines(texts_for_indent)
        # assign stripped texts back to corresponding indices in diff_lines
        it = iter(new_texts)
        for idx, raw in enumerate(hunk.get("diff_lines", [])):
            if not raw or raw.startswith("\\"):
                continue
            part = raw[1:]
            if part.strip() == "":
                stripped_texts_by_index[idx] = part
                continue
            stripped_texts_by_index[idx] = next(it)

    for idx, raw in enumerate(hunk.get("diff_lines", [])):
        if not raw:
            continue
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
            new_ln += 1
        elif prefix == "-":
            rows.append(
                '<tr class="deleted">'
                f'<td class="lineno old">{old_ln}</td>'
                '<td class="lineno new"></td>'
                '<td class="marker">-</td>'
                f'<td class="code">{escaped}</td>'
                '</tr>'
            )
            old_ln += 1
        else:
            rows.append(
                '<tr>'
                f'<td class="lineno old">{old_ln}</td>'
                f'<td class="lineno new">{new_ln}</td>'
                '<td class="marker"> </td>'
                f'<td class="code">{escaped}</td>'
                '</tr>'
            )
            old_ln += 1
            new_ln += 1

    diff_cmd = hunk.get("diff_cmd", "git diff HEAD")
    background_mode = str(render_config.get("background_mode", BACKGROUND_MODE))
    html_width = int(render_config.get("html_width", HTML_WIDTH))
    bg_color = 'transparent' if background_mode != 'normal' else '#fff'
    show_footer = False if background_mode == 'transparent_no_footer' else True

    meta = f"-{hunk.get('old_start', hunk['start'])} +{hunk['start']} | {hunk_index}/{total} | {lang} | patch"
    html = _compose_html(''.join(rows), hunk['filepath'], meta, lang, bg_color,
                         html_width, show_footer, timestamp, diff_cmd)
    return html


# ================================================================
# PNG 出力
# ================================================================

def render_png(page, html: str, out_path: Path, config: dict):
    page.set_content(html, wait_until="load")
    page_size = page.evaluate("""() => ({
        width: Math.ceil(document.body.scrollWidth),
        height: Math.ceil(document.body.scrollHeight),
    })""")
    background_mode = str(config.get("background_mode", BACKGROUND_MODE))
    page.set_viewport_size({"width": page_size["width"], "height": page_size["height"]})
    # Playwright: omit background when transparent output requested
    if background_mode != 'normal':
        page.screenshot(path=str(out_path), full_page=True, omit_background=True)
    else:
        page.screenshot(path=str(out_path), full_page=True)


def render_png_batch(items: list[tuple[str, Path]], config: dict):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            for html, out_path in items:
                render_png(page, html, out_path, config)
        finally:
            browser.close()


# ================================================================
# Flask ルート
# ================================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/browse", methods=["GET"])
def browse_repo():
    try:
        selected = choose_directory("リポジトリフォルダを選択", APP_ROOT)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not selected:
        return jsonify({"cancelled": True})
    return jsonify({"repo_path": selected})


@app.route("/api/config", methods=["GET", "POST"])
def config():
    global CONTEXT_LINES, MERGE_THRESHOLD, HTML_WIDTH, OUTPUT_DIR, OUTPUT_DIR_NAME, DIFF_MODE, BACKGROUND_MODE, INLINE_DIFF_DEFAULT_MODE, INLINE_DIFF_MAX_CHANGED_CHARS
    if request.method == "GET":
        return jsonify({
            "context_lines": CONTEXT_LINES,
            "merge_threshold": MERGE_THRESHOLD,
            "html_width": HTML_WIDTH,
            "output_dir": OUTPUT_DIR_NAME,
            "diff_mode": DIFF_MODE,
            "background_mode": BACKGROUND_MODE,
            "inline_diff_default_mode": INLINE_DIFF_DEFAULT_MODE,
            "inline_diff_max_changed_chars": INLINE_DIFF_MAX_CHANGED_CHARS,
        })
    data = request.get_json(silent=True) or {}
    try:
        if "context_lines" in data:
            CONTEXT_LINES = max(0, int(data["context_lines"]))
        if "merge_threshold" in data:
            MERGE_THRESHOLD = max(0, int(data["merge_threshold"]))
        if "html_width" in data:
            HTML_WIDTH = max(400, int(data["html_width"]))
        if "output_dir" in data:
            OUTPUT_DIR_NAME, OUTPUT_DIR = parse_output_dir(str(data["output_dir"]))
        if "diff_mode" in data:
            mode = str(data["diff_mode"]).strip().lower()
            if mode not in ("file", "patch"):
                raise ValueError("diff_mode は file または patch を指定してください")
            DIFF_MODE = mode
        if "inline_diff_default_mode" in data:
            INLINE_DIFF_DEFAULT_MODE = normalize_inline_diff_mode(str(data["inline_diff_default_mode"]))
        if "inline_diff_max_changed_chars" in data:
            INLINE_DIFF_MAX_CHANGED_CHARS = normalize_inline_diff_max_changed_chars(data["inline_diff_max_changed_chars"])
        # New setting: background_mode
        if "background_mode" in data:
            bm = str(data["background_mode"]).strip()
            if bm not in ("normal", "no_bg_footer", "transparent_no_footer"):
                raise ValueError("background_mode が不正です")
            BACKGROUND_MODE = bm
        # Backward compatibility: accept transparent_background boolean
        if "transparent_background" in data:
            TRANSPARENT = bool(data["transparent_background"])
            BACKGROUND_MODE = "transparent_no_footer" if TRANSPARENT else "normal"
    except (ValueError, TypeError) as e:
        return jsonify({"error": f"不正な値: {e}"}), 400
    return jsonify({"ok": True})


@app.route("/api/commits", methods=["POST"])
def commits():
    data = request.get_json(silent=True) or {}
    repo_path = str(data.get("repo_path", "")).strip()
    if not repo_path:
        return jsonify({"error": "リポジトリパスが無効です"}), 400

    try:
        repo = resolve_repo_path(repo_path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        items = list_commits(str(repo))
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({"commits": items})


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.get_json(silent=True) or {}
    repo_path = str(data.get("repo_path", "")).strip()
    source_mode = str(data.get("source_mode", "worktree")).strip().lower() or "worktree"
    base_ref = str(data.get("base_ref", "")).strip()
    target_ref = str(data.get("target_ref", "")).strip()

    if source_mode not in ("worktree", "staged", "commit", "range"):
        return jsonify({"error": "不正なです"}), 400

    if not repo_path:
        return jsonify({"error": "リポジトリパスが無効です"}), 400

    try:
        repo = resolve_repo_path(repo_path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        clear_repo_file_cache(str(repo))
        config_snapshot = current_config_snapshot()
        content_source = content_source_for_diff(source_mode, target_ref)
        # 人間向けの差分コマンド文字列を作成
        if source_mode == "worktree":
            diff_cmd_label = "git diff HEAD"
        elif source_mode == "staged":
            diff_cmd_label = "git diff --staged"
        elif source_mode == "commit":
            diff_cmd_label = f"git diff {target_ref}^ {target_ref}"
        else:
            diff_cmd_label = f"git diff {base_ref} {target_ref}"

        if config_snapshot.get("diff_mode") == "patch":
            diff_text = get_diff(
                str(repo),
                context_lines=int(config_snapshot.get("context_lines", CONTEXT_LINES)),
                merge_threshold=int(config_snapshot.get("merge_threshold", MERGE_THRESHOLD)),
                source_mode=source_mode,
                base_ref=base_ref,
                target_ref=target_ref,
            )
        else:
            diff_text = get_diff(
                str(repo),
                context_lines=0,
                source_mode=source_mode,
                base_ref=base_ref,
                target_ref=target_ref,
            )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400

    if not diff_text.strip():
        return jsonify({"hunks": [], "message": "差分がありません"})

    raw_hunks = parse_hunks(diff_text)
    # 各hunkに差分コマンド表記を付与
    for h in raw_hunks:
        h["diff_cmd"] = diff_cmd_label

    hunks = finalize_hunks(raw_hunks, str(repo), content_source, config_snapshot)

    analysis_id = create_analysis_session(str(repo), raw_hunks, hunks, content_source, config_snapshot)
    return jsonify({
        "analysis_id": analysis_id,
        "hunks": [make_hunk_summary(h) for h in hunks],
        "total": len(hunks),
    })


@app.route("/api/reconfigure-analysis", methods=["POST"])
def reconfigure_analysis():
    data = request.get_json(silent=True) or {}
    repo_path = str(data.get("repo_path", "")).strip()
    analysis_id = str(data.get("analysis_id", "")).strip()
    if not repo_path:
        return jsonify({"error": "リポジトリパスが無効です"}), 400
    if not analysis_id:
        return jsonify({"error": "analysis_id が不正です"}), 400

    try:
        repo = resolve_repo_path(repo_path)
        session = get_analysis_session(analysis_id, str(repo))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    previous_config = session_config(session)
    config_snapshot = current_config_snapshot()
    if previous_config.get("diff_mode") != config_snapshot.get("diff_mode"):
        return jsonify({"requires_reanalyze": True})

    raw_hunks = session.get("raw_hunks")
    if not isinstance(raw_hunks, list):
        return jsonify({"requires_reanalyze": True})

    if config_snapshot.get("diff_mode") == "patch":
        context_changed = (
            int(previous_config.get("context_lines", CONTEXT_LINES))
            != int(config_snapshot.get("context_lines", CONTEXT_LINES))
            or int(previous_config.get("merge_threshold", MERGE_THRESHOLD))
            != int(config_snapshot.get("merge_threshold", MERGE_THRESHOLD))
        )
        if context_changed:
            return jsonify({"requires_reanalyze": True})

        session["config"] = deepcopy(config_snapshot)
        session["diff_mode"] = config_snapshot.get("diff_mode", DIFF_MODE)
        return jsonify({
            "analysis_id": analysis_id,
            "hunks": [make_hunk_summary(h) for h in session["hunks"]],
            "total": len(session["hunks"]),
        })

    context_changed = (
        int(previous_config.get("context_lines", CONTEXT_LINES))
        != int(config_snapshot.get("context_lines", CONTEXT_LINES))
        or int(previous_config.get("merge_threshold", MERGE_THRESHOLD))
        != int(config_snapshot.get("merge_threshold", MERGE_THRESHOLD))
    )
    inline_default_changed = (
        previous_config.get("inline_diff_default_mode", INLINE_DIFF_DEFAULT_MODE)
        != config_snapshot.get("inline_diff_default_mode", INLINE_DIFF_DEFAULT_MODE)
    )
    if not context_changed and not inline_default_changed:
        session["config"] = deepcopy(config_snapshot)
        session["diff_mode"] = config_snapshot.get("diff_mode", DIFF_MODE)
        return jsonify({
            "analysis_id": analysis_id,
            "hunks": [make_hunk_summary(h) for h in session["hunks"]],
            "total": len(session["hunks"]),
        })

    hunks = finalize_hunks(raw_hunks, str(repo), session["content_source"], config_snapshot)
    session["hunks"] = hunks
    session["config"] = deepcopy(config_snapshot)
    session["diff_mode"] = config_snapshot.get("diff_mode", DIFF_MODE)
    return jsonify({
        "analysis_id": analysis_id,
        "hunks": [make_hunk_summary(h) for h in hunks],
        "total": len(hunks),
    })


@app.route("/api/hunk-range/<int:hunk_index>", methods=["POST"])
def update_hunk_range(hunk_index: int):
    data = request.get_json(silent=True) or {}
    repo_path = str(data.get("repo_path", "")).strip()
    analysis_id = str(data.get("analysis_id", "")).strip()
    action = str(data.get("action", "")).strip()
    if not repo_path:
        return jsonify({"error": "リポジトリパスが無効です"}), 400
    if not analysis_id:
        return jsonify({"error": "analysis_id が不正です"}), 400

    try:
        repo = resolve_repo_path(repo_path)
        session = get_analysis_session(analysis_id, str(repo))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if session_config(session).get("diff_mode") == "patch":
        return jsonify({"error": "行範囲の調整は通常表示でのみ使用できます"}), 400

    hunks = session["hunks"]
    if hunk_index < 0 or hunk_index >= len(hunks):
        return jsonify({"error": "無効なインデックス"}), 400

    hunk = hunks[hunk_index]
    try:
        summary = adjust_hunk_range(hunk, str(repo), session["content_source"], action)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({"hunk": summary})


@app.route("/api/hunk-inline-diff/<int:hunk_index>", methods=["POST"])
def update_hunk_inline_diff(hunk_index: int):
    data = request.get_json(silent=True) or {}
    repo_path = str(data.get("repo_path", "")).strip()
    analysis_id = str(data.get("analysis_id", "")).strip()
    if not repo_path:
        return jsonify({"error": "リポジトリパスが無効です"}), 400
    if not analysis_id:
        return jsonify({"error": "analysis_id が不正です"}), 400

    try:
        repo = resolve_repo_path(repo_path)
        session = get_analysis_session(analysis_id, str(repo))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    hunks = session["hunks"]
    if hunk_index < 0 or hunk_index >= len(hunks):
        return jsonify({"error": "無効なインデックス"}), 400

    if "mode" in data:
        mode = str(data.get("mode", "")).strip().lower()
        if mode not in INLINE_DIFF_MODES:
            return jsonify({"error": "mode は full, new, off のいずれかを指定してください"}), 400
    else:
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            return jsonify({"error": "enabled は真偽値で指定してください"}), 400
        mode = "full" if enabled else "off"

    hunk = hunks[hunk_index]
    hunk["inline_diff_mode"] = mode
    hunk["inline_diff_enabled"] = mode != "off"
    return jsonify({"hunk": make_hunk_summary(hunk)})


@app.route("/api/preview/<int:hunk_index>", methods=["POST"])
def preview(hunk_index: int):
    data = request.get_json(silent=True) or {}
    repo_path = str(data.get("repo_path", "")).strip()
    analysis_id = str(data.get("analysis_id", "")).strip()
    if not repo_path:
        return jsonify({"error": "リポジトリパスが無効です"}), 400
    if not analysis_id:
        return jsonify({"error": "analysis_id が不正です"}), 400

    try:
        repo = resolve_repo_path(repo_path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        session = get_analysis_session(analysis_id, str(repo))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    hunks = session["hunks"]
    total = len(hunks)

    if hunk_index < 0 or hunk_index >= total:
        return jsonify({"error": "無効なインデックス"}), 400

    hunk = hunks[hunk_index]
    hunk["changed_lines"] = hunk.get("changed_lines", [])
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = build_code_html(
        hunk,
        str(repo),
        hunk_index + 1,
        total,
        timestamp,
        session["content_source"],
        session_config(session),
    )
    return html


@app.route("/api/export", methods=["POST"])
def export():
    data = request.get_json(silent=True) or {}
    repo_path = str(data.get("repo_path", "")).strip()
    analysis_id = str(data.get("analysis_id", "")).strip()
    if not repo_path:
        return jsonify({"error": "リポジトリパスが無効です"}), 400
    if not analysis_id:
        return jsonify({"error": "analysis_id が不正です"}), 400

    try:
        repo = resolve_repo_path(repo_path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        session = get_analysis_session(analysis_id, str(repo))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    config_snapshot = session_config(session)
    hunks = session["hunks"]

    raw_indices = data.get("indices")
    if raw_indices is None:
        indices = list(range(len(hunks)))
    elif isinstance(raw_indices, list):
        indices = raw_indices
    else:
        return jsonify({"error": "indices は配列で指定してください"}), 400

    normalized_indices: list[int] = []
    for idx in indices:
        if not isinstance(idx, int):
            return jsonify({"error": "indices は整数配列で指定してください"}), 400
        if idx < 0 or idx >= len(hunks):
            return jsonify({"error": f"indices に範囲外の値があります: {idx}"}), 400
        normalized_indices.append(idx)

    if not normalized_indices:
        return jsonify({"error": "出力対象がありません"}), 400

    try:
        output_dir_name, output_dir = parse_output_dir(str(config_snapshot.get("output_dir", OUTPUT_DIR_NAME)))
        output_dir.mkdir(parents=True, exist_ok=True)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"出力ディレクトリを作成できませんでした: {e}"}), 500

    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamp_disp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = len(hunks)
    render_items: list[tuple[str, Path]] = []
    saved: list[str] = []

    for i in normalized_indices:
        hunk = hunks[i]
        hunk["changed_lines"] = hunk.get("changed_lines", [])
        safe = sanitize_filename_component(hunk["filepath"])
        out_path = output_dir / f"{timestamp_str}_{i + 1 :03d}_{safe}_L{hunk['start']}.png"
        html = build_code_html(
            hunk,
            str(repo),
            i + 1,
            total,
            timestamp_disp,
            session["content_source"],
            config_snapshot,
        )
        render_items.append((html, out_path))
        saved.append(str(out_path))

    try:
        render_png_batch(render_items, config_snapshot)
    except Exception as e:
        return jsonify({"error": f"PNG出力に失敗しました: {e}"}), 500

    return jsonify({"saved": saved, "count": len(saved), "output_dir": output_dir_name})


@app.route("/api/open-output-dir", methods=["POST"])
def open_output_dir():
    data = request.get_json(silent=True) or {}
    try:
        output_dir_name, output_dir = output_dir_from_request(data)
        output_dir.mkdir(parents=True, exist_ok=True)
        open_directory(output_dir)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"保存先を開けませんでした: {e}"}), 500

    return jsonify({"ok": True, "output_dir": output_dir_name})


@app.route("/api/browse-output-dir", methods=["GET"])
def browse_output_dir():
    try:
        selected = choose_directory("出力フォルダを選択", OUTPUT_DIR if OUTPUT_DIR.exists() else APP_ROOT)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not selected:
        return jsonify({"cancelled": True})

    try:
        selected_path = Path(selected).resolve(strict=True)
        try:
            output_dir_name = selected_path.relative_to(APP_ROOT).as_posix()
        except ValueError:
            output_dir_name = str(selected_path)
        parse_output_dir(output_dir_name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"フォルダ選択に失敗しました: {e}"}), 500

    return jsonify({"output_dir": output_dir_name})


@app.route("/api/clear-output-dir", methods=["POST"])
def clear_output_dir():
    data = request.get_json(silent=True) or {}
    try:
        output_dir_name, output_dir = output_dir_from_request(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if output_dir == APP_ROOT:
        return jsonify({"error": "アプリルートはクリア対象にできません"}), 400
    if not output_dir.exists():
        return jsonify({"deleted": 0, "output_dir": output_dir_name})
    if not output_dir.is_dir():
        return jsonify({"error": "出力先がディレクトリではありません"}), 400

    deleted = 0
    try:
        for item in output_dir.iterdir():
            if item.is_file() and _OUTPUT_PNG_RE.match(item.name):
                item.unlink()
                deleted += 1
    except OSError as e:
        return jsonify({"error": f"PNGのクリアに失敗しました: {e}"}), 500

    return jsonify({"deleted": deleted, "output_dir": output_dir_name})


@app.route("/screenshots/<path:filename>")
def screenshot_file(filename):
    return send_from_directory(OUTPUT_DIR, filename)


# ================================================================
# 起動
# ================================================================

if __name__ == "__main__":
    url = "http://127.0.0.1:5000"
    Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"起動しました → {url}")
    app.run(debug=False)
