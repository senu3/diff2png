#!/usr/bin/env python3
"""
diff2png / app.py
Flask + Playwright によるエビデンス用 git diff スクリーンショットツール
"""

import hashlib
import os
import re
import socket
import subprocess
import sys
import webbrowser
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from threading import Timer
from uuid import uuid4

from flask import Flask, jsonify, render_template, request, send_from_directory

import diff_parser
import diff_renderer
from diff_renderer import (
    hunk_inline_added_mutes,
    hunk_inline_diff_mode,
    hunk_manual_row_highlights,
    normalize_inline_added_mute_key,
    normalize_inline_diff_max_changed_chars,
    normalize_inline_diff_mode,
    normalize_manual_row_highlight_color,
)

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
SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 5127
SERVER_PORT_SCAN_LIMIT = 100

app = Flask(__name__)

# --- パフォーマンス用キャッシュ / コンパイル済み正規表現 ---
FILE_CONTENT_CACHE: dict[str, list[str]] = {}
ANALYSIS_SESSIONS: dict[str, dict] = {}
MAX_ANALYSIS_SESSIONS = 20
GIT_TIMEOUT_SECONDS = 30
INLINE_DIFF_MAX_CHANGED_CHARS = 120
INLINE_DIFF_MAX_CHANGED_CHARS_LIMIT = 500
INLINE_DIFF_MIN_SIMILARITY = 0.62
INLINE_DIFF_TAG_BLOCK_MIN_SIMILARITY = 0.8
DIFF_MODES = {"file", "patch", "deleted"}
PATCH_LIKE_DIFF_MODES = {"patch", "deleted"}
SOURCE_MODES = {"worktree", "staged", "commit", "range"}
SOURCE_KEY_UNSTAGED = "unstaged"
SOURCE_KEY_STAGED = "staged"
SOURCE_COMMIT_PREFIX = "commit:"
BACKGROUND_MODES = {"normal", "no_bg_footer", "transparent_no_footer"}
INLINE_DIFF_MODES = {"full", "new", "off"}
INLINE_ADDED_MUTES_LIMIT = 200
INLINE_ADDED_MUTE_KEY_LIMIT = 1000
MANUAL_ROW_HIGHLIGHTS_LIMIT = 500
MANUAL_ROW_HIGHLIGHT_COLORS = {"green", "yellow"}
_FILENAME_UNSAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_FILENAME_REPEAT_UNDERSCORE_RE = re.compile(r"_+")
_OUTPUT_PNG_RE = re.compile(r"^\d{8}_\d{6}_\d{3}_.+_L\d+\.png$", re.IGNORECASE)
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


# ================================================================
# git / diff ユーティリティ
# ================================================================

def run_git(
    repo_path: str,
    args: list[str],
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=repo_path,
            timeout=GIT_TIMEOUT_SECONDS,
            input=input_text,
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
    result = run_git(
        repo_path,
        [
            "log",
            "--first-parent",
            f"-n{max(1, min(limit, 200))}",
            "--pretty=format:%H\t%h\t%s",
        ],
    )
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


def _source_layers(commits: list[dict]) -> list[dict]:
    layers = [
        {
            "key": SOURCE_KEY_UNSTAGED,
            "kind": SOURCE_KEY_UNSTAGED,
            "label": "未ステージ",
        },
        {
            "key": SOURCE_KEY_STAGED,
            "kind": SOURCE_KEY_STAGED,
            "label": "ステージ済み",
        },
    ]
    for commit in commits:
        commit_hash = str(commit.get("hash", "")).strip()
        if not commit_hash:
            continue
        layers.append({
            "key": f"{SOURCE_COMMIT_PREFIX}{commit_hash}",
            "kind": "commit",
            "label": str(commit.get("short", commit_hash[:7])),
            "hash": commit_hash,
            "short": str(commit.get("short", commit_hash[:7])),
            "subject": str(commit.get("subject", "")),
        })
    return layers


def _commit_parent_or_empty_tree(repo_path: str, commit_hash: str) -> tuple[str, bool]:
    parent_result = run_git(repo_path, ["rev-parse", "--verify", f"{commit_hash}^"])
    if parent_result.returncode == 0 and parent_result.stdout.strip():
        return parent_result.stdout.strip(), False

    empty_tree_result = run_git(repo_path, ["mktree"], input_text="")
    if empty_tree_result.returncode != 0 or not empty_tree_result.stdout.strip():
        raise RuntimeError(
            empty_tree_result.stderr.strip()
            or "ルートコミットの比較元を解決できませんでした"
        )
    return empty_tree_result.stdout.strip(), True


def _source_range_summary(selected_layers: list[dict]) -> str:
    commit_count = sum(1 for layer in selected_layers if layer["kind"] == "commit")
    parts = []
    if commit_count:
        parts.append(f"{commit_count}コミット")
    if any(layer["kind"] == SOURCE_KEY_STAGED for layer in selected_layers):
        parts.append("ステージ済み")
    if any(layer["kind"] == SOURCE_KEY_UNSTAGED for layer in selected_layers):
        parts.append("未ステージ")
    return " + ".join(parts)


def resolve_source_selection(repo_path: str, raw_keys) -> dict:
    if not isinstance(raw_keys, list) or not raw_keys:
        raise ValueError("差分ソースを1件以上選択してください")

    commits = list_commits(repo_path)
    layers = _source_layers(commits)
    index_by_key = {layer["key"]: idx for idx, layer in enumerate(layers)}

    requested_keys = []
    for raw_key in raw_keys:
        key = str(raw_key or "").strip()
        if not key or key not in index_by_key:
            raise ValueError("選択された差分ソースが見つかりません")
        if key not in requested_keys:
            requested_keys.append(key)

    requested_indices = [index_by_key[key] for key in requested_keys]
    newest_index = min(requested_indices)
    oldest_index = max(requested_indices)
    selected_layers = layers[newest_index:oldest_index + 1]
    newest_layer = selected_layers[0]
    oldest_layer = selected_layers[-1]

    if oldest_layer["kind"] == SOURCE_KEY_UNSTAGED:
        base_source = {"type": "index"}
        base_label = "インデックス"
    elif oldest_layer["kind"] == SOURCE_KEY_STAGED:
        base_source = {"type": "ref", "ref": "HEAD"}
        base_label = "HEAD"
    else:
        parent_ref, is_root = _commit_parent_or_empty_tree(repo_path, oldest_layer["hash"])
        base_source = {"type": "ref", "ref": parent_ref}
        base_label = "空のツリー" if is_root else f"{oldest_layer['short']}^"

    if newest_layer["kind"] == SOURCE_KEY_UNSTAGED:
        target_source = {"type": "worktree"}
        target_label = "作業ツリー"
    elif newest_layer["kind"] == SOURCE_KEY_STAGED:
        target_source = {"type": "index"}
        target_label = "インデックス"
    else:
        target_source = {"type": "ref", "ref": newest_layer["hash"]}
        target_label = newest_layer["short"]

    return {
        "keys": [layer["key"] for layer in selected_layers],
        "requested_keys": requested_keys,
        "base_source": base_source,
        "target_source": target_source,
        "base_label": base_label,
        "target_label": target_label,
        "range_label": f"{base_label} → {target_label}",
        "summary": _source_range_summary(selected_layers),
        "commit_count": sum(1 for layer in selected_layers if layer["kind"] == "commit"),
    }


def get_diff_for_source_selection(
    repo_path: str,
    selection: dict,
    context_lines: int | None = None,
    merge_threshold: int | None = None,
) -> tuple[str, str]:
    base_source = selection["base_source"]
    target_source = selection["target_source"]
    base_type = base_source["type"]
    target_type = target_source["type"]
    args = ["diff"]

    if target_type == "worktree":
        if base_type == "ref":
            args.append(str(base_source["ref"]))
        elif base_type != "index":
            raise RuntimeError("比較元と作業ツリーの組み合わせが不正です")
    elif target_type == "index":
        if base_type != "ref":
            raise RuntimeError("比較元とインデックスの組み合わせが不正です")
        args.extend(["--cached", str(base_source["ref"])])
    elif target_type == "ref":
        if base_type != "ref":
            raise RuntimeError("コミット間の比較元が不正です")
        args.extend([str(base_source["ref"]), str(target_source["ref"])])
    else:
        raise RuntimeError("比較先が不正です")

    if context_lines is not None:
        args.append(f"-U{max(0, int(context_lines))}")
    if merge_threshold is not None:
        args.append(f"--inter-hunk-context={max(0, int(merge_threshold))}")

    result = run_git(repo_path, args)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout, f"git {' '.join(args)}"


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
        "inline_added_mutes": sorted(hunk_inline_added_mutes(hunk)),
        "manual_row_highlights": {
            str(lineno): color
            for lineno, color in sorted(hunk_manual_row_highlights(hunk).items())
        },
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


def request_json_data() -> dict:
    data = request.get_json(silent=True) or {}
    return data if isinstance(data, dict) else {}


def error_response(message: str, status: int = 400):
    return jsonify({"error": message}), status


def require_payload_text(data: dict, key: str, error_message: str) -> str:
    value = str(data.get(key, "")).strip()
    if not value:
        raise ValueError(error_message)
    return value


def resolve_repo_from_payload(data: dict) -> Path:
    repo_path = require_payload_text(data, "repo_path", "リポジトリパスが無効です")
    return resolve_repo_path(repo_path)


def resolve_analysis_context(data: dict) -> tuple[Path, dict]:
    repo = resolve_repo_from_payload(data)
    analysis_id = require_payload_text(data, "analysis_id", "analysis_id が不正です")
    return repo, get_analysis_session(analysis_id, str(repo))


def require_file_mode(session: dict, message: str) -> None:
    if session_config(session).get("diff_mode") != "file":
        raise ValueError(message)


def hunk_at(session: dict, hunk_index: int) -> dict:
    hunks = session["hunks"]
    if hunk_index < 0 or hunk_index >= len(hunks):
        raise ValueError("無効なインデックス")
    return hunks[hunk_index]


def hunks_payload(
    analysis_id: str,
    hunks: list[dict],
    source_selection: dict | None = None,
) -> dict:
    payload = {
        "analysis_id": analysis_id,
        "hunks": [make_hunk_summary(h) for h in hunks],
        "total": len(hunks),
    }
    if isinstance(source_selection, dict):
        payload["source_selection"] = {
            key: deepcopy(source_selection[key])
            for key in (
                "keys",
                "requested_keys",
                "base_label",
                "target_label",
                "range_label",
                "summary",
                "commit_count",
            )
            if key in source_selection
        }
    return payload


def diff_command_label(source_mode: str, base_ref: str, target_ref: str) -> str:
    if source_mode == "worktree":
        return "git diff HEAD"
    if source_mode == "staged":
        return "git diff --staged"
    if source_mode == "commit":
        return f"git diff {target_ref}^ {target_ref}"
    return f"git diff {base_ref} {target_ref}"


def get_analysis_diff_text(
    repo_path: str,
    source_mode: str,
    base_ref: str,
    target_ref: str,
    config: dict,
) -> str:
    diff_kwargs = {
        "source_mode": source_mode,
        "base_ref": base_ref,
        "target_ref": target_ref,
    }
    if config.get("diff_mode") in PATCH_LIKE_DIFF_MODES:
        return get_diff(
            repo_path,
            context_lines=int(config.get("context_lines", CONTEXT_LINES)),
            merge_threshold=int(config.get("merge_threshold", MERGE_THRESHOLD)),
            **diff_kwargs,
        )
    return get_diff(repo_path, context_lines=0, **diff_kwargs)


def normalize_export_indices(raw_indices, hunk_count: int) -> list[int]:
    if raw_indices is None:
        indices = list(range(hunk_count))
    elif isinstance(raw_indices, list):
        indices = raw_indices
    else:
        raise ValueError("indices は配列で指定してください")

    normalized_indices: list[int] = []
    for idx in indices:
        if not isinstance(idx, int):
            raise ValueError("indices は整数配列で指定してください")
        if idx < 0 or idx >= hunk_count:
            raise ValueError(f"indices に範囲外の値があります: {idx}")
        normalized_indices.append(idx)

    if not normalized_indices:
        raise ValueError("出力対象がありません")
    return normalized_indices


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
        h["inline_added_mutes"] = []
        h["manual_row_highlights"] = {}
    return hunks


def _clamp_hunk_range(start: int, end: int, total_lines: int) -> tuple[int, int]:
    total = max(1, int(total_lines))
    start = max(1, min(int(start), total))
    end = max(start, min(int(end), total))
    return start, end


def _required_hunk_range(
    hunk: dict,
    total_lines: int,
    include_diff_anchors: bool = False,
) -> tuple[int, int]:
    anchors: list[int] = []
    for lineno in hunk.get("changed_lines", []):
        try:
            anchors.append(int(lineno))
        except (TypeError, ValueError):
            continue

    if include_diff_anchors:
        raw_blocks = hunk.get("diff_blocks")
        if not isinstance(raw_blocks, list) or not raw_blocks:
            raw_blocks = [hunk]
        for block in raw_blocks:
            if not isinstance(block, dict):
                continue
            try:
                cursor = int(block.get("start", hunk.get("default_start", hunk.get("start", 1))))
            except (TypeError, ValueError):
                cursor = 1
            for raw in block.get("diff_lines", []):
                if not raw or raw.startswith("\\"):
                    continue
                if raw.startswith("+") and not raw.startswith("+++"):
                    anchors.append(cursor)
                    cursor += 1
                elif raw.startswith("-") and not raw.startswith("---"):
                    anchors.append(cursor)
                elif raw.startswith(" "):
                    cursor += 1

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
    include_diff_anchors: bool = False,
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
    required_start, required_end = _required_hunk_range(hunk, total_lines, include_diff_anchors)
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
    source_selection: dict | None = None,
) -> str:
    analysis_id = uuid4().hex
    ANALYSIS_SESSIONS[analysis_id] = {
        "repo_path": str(Path(repo_path).resolve()),
        "raw_hunks": deepcopy(raw_hunks),
        "hunks": hunks,
        "content_source": content_source,
        "config": deepcopy(config),
        "diff_mode": config.get("diff_mode", DIFF_MODE),
        "source_selection": deepcopy(source_selection),
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


def configured_server_port(value: str | None = None) -> int:
    raw = value if value is not None else os.environ.get("DIFF2PNG_PORT", str(DEFAULT_SERVER_PORT))
    try:
        port = int(raw)
    except (TypeError, ValueError) as e:
        raise ValueError("DIFF2PNG_PORT は1〜65535の整数で指定してください") from e
    if not 1 <= port <= 65535:
        raise ValueError("DIFF2PNG_PORT は1〜65535の整数で指定してください")
    return port


def can_bind_server_port(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((host, port))
    except OSError:
        return False
    return True


def find_available_server_port(
    host: str = SERVER_HOST,
    preferred_port: int = DEFAULT_SERVER_PORT,
    scan_limit: int = SERVER_PORT_SCAN_LIMIT,
) -> int:
    if not 1 <= preferred_port <= 65535:
        raise ValueError("preferred_port は1〜65535で指定してください")

    stop = min(65536, preferred_port + max(1, int(scan_limit)))
    for port in range(preferred_port, stop):
        if can_bind_server_port(host, port):
            return port
    raise RuntimeError(f"利用可能なポートが見つかりません: {preferred_port}〜{stop - 1}")


def parse_hunks(diff_text: str) -> list[dict]:
    return diff_parser.parse_hunks(diff_text)


def expand_and_merge(
    hunks: list[dict],
    repo_path: str,
    context: int,
    merge_thresh: int,
    content_source: dict | None = None,
) -> list[dict]:
    return diff_parser.expand_and_merge(
        hunks,
        repo_path,
        context,
        merge_thresh,
        content_source,
        read_source_lines=read_source_lines,
    )


def build_code_html(
    hunk: dict,
    repo_path: str,
    hunk_index: int,
    total: int,
    timestamp: str,
    content_source: dict | None = None,
    config: dict | None = None,
) -> str:
    return diff_renderer.build_code_html(
        hunk,
        repo_path,
        hunk_index,
        total,
        timestamp,
        content_source,
        config or current_config_snapshot(),
        read_source_lines,
    )


def read_lines(
    repo_path: str,
    filepath: str,
    start: int,
    end: int,
    content_source: dict | None = None,
) -> list[tuple[int, str]]:
    return diff_renderer.read_lines(
        repo_path,
        filepath,
        start,
        end,
        content_source,
        read_source_lines,
    )


def build_deleted_context_html(
    hunk: dict,
    repo_path: str,
    hunk_index: int,
    total: int,
    timestamp: str,
    content_source: dict | None = None,
    config: dict | None = None,
) -> str:
    return diff_renderer.build_deleted_context_html(
        hunk,
        repo_path,
        hunk_index,
        total,
        timestamp,
        content_source,
        config or current_config_snapshot(),
        read_source_lines,
    )


# Compatibility exports for callers and tests that use the original app module.
_strip_common_indent_from_lines = diff_renderer._strip_common_indent_from_lines
_line_replacements_for_diff_lines = diff_renderer._line_replacements_for_diff_lines
_line_replacements_by_new_lineno = diff_renderer._line_replacements_by_new_lineno
_inline_diff_html = diff_renderer._inline_diff_html
_merge_nearby_inline_fragments = diff_renderer._merge_nearby_inline_fragments
_merge_inline_punctuation_fragments = diff_renderer._merge_inline_punctuation_fragments
build_patch_html = diff_renderer.build_patch_html
build_deleted_patch_html = diff_renderer.build_deleted_patch_html
detect_language = diff_renderer.detect_language


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
        return error_response(str(e), 500)

    if not selected:
        return jsonify({"cancelled": True})
    return jsonify({"repo_path": selected})


@app.route("/api/config", methods=["GET", "POST"])
def config():
    global CONTEXT_LINES, MERGE_THRESHOLD, HTML_WIDTH, OUTPUT_DIR, OUTPUT_DIR_NAME, DIFF_MODE, BACKGROUND_MODE, INLINE_DIFF_DEFAULT_MODE, INLINE_DIFF_MAX_CHANGED_CHARS
    if request.method == "GET":
        return jsonify(current_config_snapshot())
    data = request_json_data()
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
            if mode not in DIFF_MODES:
                raise ValueError("diff_mode は file, patch, deleted のいずれかを指定してください")
            DIFF_MODE = mode
        if "inline_diff_default_mode" in data:
            INLINE_DIFF_DEFAULT_MODE = normalize_inline_diff_mode(str(data["inline_diff_default_mode"]))
        if "inline_diff_max_changed_chars" in data:
            INLINE_DIFF_MAX_CHANGED_CHARS = normalize_inline_diff_max_changed_chars(data["inline_diff_max_changed_chars"])
        # New setting: background_mode
        if "background_mode" in data:
            bm = str(data["background_mode"]).strip()
            if bm not in BACKGROUND_MODES:
                raise ValueError("background_mode が不正です")
            BACKGROUND_MODE = bm
        # Backward compatibility: accept transparent_background boolean
        if "transparent_background" in data:
            TRANSPARENT = bool(data["transparent_background"])
            BACKGROUND_MODE = "transparent_no_footer" if TRANSPARENT else "normal"
    except (ValueError, TypeError) as e:
        return error_response(f"不正な値: {e}")
    return jsonify({"ok": True})


@app.route("/api/commits", methods=["POST"])
def commits():
    data = request_json_data()
    try:
        repo = resolve_repo_from_payload(data)
    except ValueError as e:
        return error_response(str(e))

    try:
        items = list_commits(str(repo))
    except RuntimeError as e:
        return error_response(str(e))

    return jsonify({"commits": items})


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request_json_data()
    repo_path = str(data.get("repo_path", "")).strip()
    source_mode = str(data.get("source_mode", "worktree")).strip().lower() or "worktree"
    base_ref = str(data.get("base_ref", "")).strip()
    target_ref = str(data.get("target_ref", "")).strip()
    source_keys = data.get("source_keys")

    if source_keys is None and source_mode not in SOURCE_MODES:
        return error_response("不正なです")

    if not repo_path:
        return error_response("リポジトリパスが無効です")

    try:
        repo = resolve_repo_path(repo_path)
    except ValueError as e:
        return error_response(str(e))

    try:
        clear_repo_file_cache(str(repo))
        config_snapshot = current_config_snapshot()
        source_selection = None
        if source_keys is not None:
            source_selection = resolve_source_selection(str(repo), source_keys)
            content_source = source_selection["target_source"]
            context_lines = (
                int(config_snapshot.get("context_lines", CONTEXT_LINES))
                if config_snapshot.get("diff_mode") in PATCH_LIKE_DIFF_MODES
                else 0
            )
            merge_threshold = (
                int(config_snapshot.get("merge_threshold", MERGE_THRESHOLD))
                if config_snapshot.get("diff_mode") in PATCH_LIKE_DIFF_MODES
                else None
            )
            diff_text, diff_cmd_label = get_diff_for_source_selection(
                str(repo),
                source_selection,
                context_lines=context_lines,
                merge_threshold=merge_threshold,
            )
        else:
            content_source = content_source_for_diff(source_mode, target_ref)
            diff_cmd_label = diff_command_label(source_mode, base_ref, target_ref)
            diff_text = get_analysis_diff_text(str(repo), source_mode, base_ref, target_ref, config_snapshot)
    except (RuntimeError, ValueError) as e:
        return error_response(str(e))

    if not diff_text.strip():
        payload = {"hunks": [], "message": "差分がありません"}
        if isinstance(source_selection, dict):
            payload["source_selection"] = {
                key: deepcopy(source_selection[key])
                for key in (
                    "keys",
                    "requested_keys",
                    "base_label",
                    "target_label",
                    "range_label",
                    "summary",
                    "commit_count",
                )
                if key in source_selection
            }
        return jsonify(payload)

    raw_hunks = parse_hunks(diff_text)
    # 各hunkに差分コマンド表記を付与
    for h in raw_hunks:
        h["diff_cmd"] = diff_cmd_label

    hunks = finalize_hunks(raw_hunks, str(repo), content_source, config_snapshot)

    analysis_id = create_analysis_session(
        str(repo),
        raw_hunks,
        hunks,
        content_source,
        config_snapshot,
        source_selection,
    )
    return jsonify(hunks_payload(analysis_id, hunks, source_selection))


@app.route("/api/reconfigure-analysis", methods=["POST"])
def reconfigure_analysis():
    data = request_json_data()
    analysis_id = str(data.get("analysis_id", "")).strip()
    if not analysis_id:
        return error_response("analysis_id が不正です")

    try:
        repo, session = resolve_analysis_context(data)
    except ValueError as e:
        return error_response(str(e))

    previous_config = session_config(session)
    config_snapshot = current_config_snapshot()
    if previous_config.get("diff_mode") != config_snapshot.get("diff_mode"):
        return jsonify({"requires_reanalyze": True})

    raw_hunks = session.get("raw_hunks")
    if not isinstance(raw_hunks, list):
        return jsonify({"requires_reanalyze": True})

    if config_snapshot.get("diff_mode") in PATCH_LIKE_DIFF_MODES:
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
        return jsonify(hunks_payload(
            analysis_id,
            session["hunks"],
            session.get("source_selection"),
        ))

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
        return jsonify(hunks_payload(
            analysis_id,
            session["hunks"],
            session.get("source_selection"),
        ))

    hunks = finalize_hunks(raw_hunks, str(repo), session["content_source"], config_snapshot)
    session["hunks"] = hunks
    session["config"] = deepcopy(config_snapshot)
    session["diff_mode"] = config_snapshot.get("diff_mode", DIFF_MODE)
    return jsonify(hunks_payload(
        analysis_id,
        hunks,
        session.get("source_selection"),
    ))


@app.route("/api/hunk-range/<int:hunk_index>", methods=["POST"])
def update_hunk_range(hunk_index: int):
    data = request_json_data()
    action = str(data.get("action", "")).strip()

    try:
        repo, session = resolve_analysis_context(data)
        diff_mode = session_config(session).get("diff_mode")
        if diff_mode in PATCH_LIKE_DIFF_MODES and action in {"expand_up", "expand_down"}:
            raise ValueError("差分表示と赤のみ表示では範囲を拡大できません")
        hunk = hunk_at(session, hunk_index)
    except ValueError as e:
        return error_response(str(e))

    try:
        summary = adjust_hunk_range(
            hunk,
            str(repo),
            session["content_source"],
            action,
            include_diff_anchors=diff_mode in PATCH_LIKE_DIFF_MODES,
        )
    except ValueError as e:
        return error_response(str(e))

    return jsonify({"hunk": summary})


@app.route("/api/hunk-inline-diff/<int:hunk_index>", methods=["POST"])
def update_hunk_inline_diff(hunk_index: int):
    data = request_json_data()

    try:
        _, session = resolve_analysis_context(data)
        require_file_mode(session, "行内差分は通常表示でのみ使用できます")
        hunk = hunk_at(session, hunk_index)
    except ValueError as e:
        return error_response(str(e))

    if "mode" in data:
        mode = str(data.get("mode", "")).strip().lower()
        if mode not in INLINE_DIFF_MODES:
            return error_response("mode は full, new, off のいずれかを指定してください")
    else:
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            return error_response("enabled は真偽値で指定してください")
        mode = "full" if enabled else "off"

    hunk["inline_diff_mode"] = mode
    hunk["inline_diff_enabled"] = mode != "off"
    return jsonify({"hunk": make_hunk_summary(hunk)})


@app.route("/api/hunk-inline-added-mute/<int:hunk_index>", methods=["POST"])
def update_hunk_inline_added_mute(hunk_index: int):
    data = request_json_data()
    muted = data.get("muted")
    try:
        require_payload_text(data, "repo_path", "リポジトリパスが無効です")
        require_payload_text(data, "analysis_id", "analysis_id が不正です")
    except ValueError as e:
        return error_response(str(e))

    if not isinstance(muted, bool):
        return error_response("muted は真偽値で指定してください")

    try:
        key = normalize_inline_added_mute_key(data.get("key"))
        _, session = resolve_analysis_context(data)
        require_file_mode(session, "追加ハイライトのミュートは通常表示でのみ使用できます")
        hunk = hunk_at(session, hunk_index)
    except ValueError as e:
        return error_response(str(e))

    if hunk_inline_diff_mode(hunk) == "off":
        return error_response("行内差分が非表示のhunkでは使用できません")

    mutes = hunk_inline_added_mutes(hunk)
    if muted:
        if len(mutes) >= INLINE_ADDED_MUTES_LIMIT and key not in mutes:
            return error_response("ミュート数が上限に達しました")
        mutes.add(key)
    else:
        mutes.discard(key)
    hunk["inline_added_mutes"] = sorted(mutes)
    return jsonify({"hunk": make_hunk_summary(hunk)})


@app.route("/api/hunk-row-highlight/<int:hunk_index>", methods=["POST"])
def update_hunk_row_highlight(hunk_index: int):
    data = request_json_data()
    try:
        require_payload_text(data, "repo_path", "リポジトリパスが無効です")
        require_payload_text(data, "analysis_id", "analysis_id が不正です")
    except ValueError as e:
        return error_response(str(e))

    try:
        lineno = int(data.get("lineno"))
        color = normalize_manual_row_highlight_color(data.get("color"))
        _, session = resolve_analysis_context(data)
        require_file_mode(session, "行背景の編集は通常表示でのみ使用できます")
        hunk = hunk_at(session, hunk_index)
    except (TypeError, ValueError) as e:
        return error_response(str(e) if str(e) else "lineno が不正です")

    if lineno <= 0:
        return error_response("lineno が不正です")

    highlights = hunk_manual_row_highlights(hunk)
    if color:
        if len(highlights) >= MANUAL_ROW_HIGHLIGHTS_LIMIT and lineno not in highlights:
            return error_response("行背景の編集数が上限に達しました")
        highlights[lineno] = color
    else:
        highlights.pop(lineno, None)

    hunk["manual_row_highlights"] = {
        str(line): row_color
        for line, row_color in sorted(highlights.items())
    }
    return jsonify({"hunk": make_hunk_summary(hunk)})


@app.route("/api/preview/<int:hunk_index>", methods=["POST"])
def preview(hunk_index: int):
    data = request_json_data()
    try:
        repo, session = resolve_analysis_context(data)
        hunk = hunk_at(session, hunk_index)
    except ValueError as e:
        return error_response(str(e))

    hunks = session["hunks"]
    total = len(hunks)
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
    data = request_json_data()
    try:
        repo, session = resolve_analysis_context(data)
    except ValueError as e:
        return error_response(str(e))

    config_snapshot = session_config(session)
    hunks = session["hunks"]

    try:
        normalized_indices = normalize_export_indices(data.get("indices"), len(hunks))
    except ValueError as e:
        return error_response(str(e))

    try:
        output_dir_name, output_dir = parse_output_dir(str(config_snapshot.get("output_dir", OUTPUT_DIR_NAME)))
        output_dir.mkdir(parents=True, exist_ok=True)
    except ValueError as e:
        return error_response(str(e))
    except OSError as e:
        return error_response(f"出力ディレクトリを作成できませんでした: {e}", 500)

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
        return error_response(f"PNG出力に失敗しました: {e}", 500)

    return jsonify({"saved": saved, "count": len(saved), "output_dir": output_dir_name})


@app.route("/api/open-output-dir", methods=["POST"])
def open_output_dir():
    data = request_json_data()
    try:
        output_dir_name, output_dir = output_dir_from_request(data)
        output_dir.mkdir(parents=True, exist_ok=True)
        open_directory(output_dir)
    except ValueError as e:
        return error_response(str(e))
    except OSError as e:
        return error_response(f"保存先を開けませんでした: {e}", 500)

    return jsonify({"ok": True, "output_dir": output_dir_name})


@app.route("/api/browse-output-dir", methods=["GET"])
def browse_output_dir():
    try:
        selected = choose_directory("出力フォルダを選択", OUTPUT_DIR if OUTPUT_DIR.exists() else APP_ROOT)
    except Exception as e:
        return error_response(str(e), 500)

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
        return error_response(str(e))
    except OSError as e:
        return error_response(f"フォルダ選択に失敗しました: {e}", 500)

    return jsonify({"output_dir": output_dir_name})


@app.route("/api/clear-output-dir", methods=["POST"])
def clear_output_dir():
    data = request_json_data()
    try:
        output_dir_name, output_dir = output_dir_from_request(data)
    except ValueError as e:
        return error_response(str(e))

    if output_dir == APP_ROOT:
        return error_response("アプリルートはクリア対象にできません")
    if not output_dir.exists():
        return jsonify({"deleted": 0, "output_dir": output_dir_name})
    if not output_dir.is_dir():
        return error_response("出力先がディレクトリではありません")

    deleted = 0
    try:
        for item in output_dir.iterdir():
            if item.is_file() and _OUTPUT_PNG_RE.match(item.name):
                item.unlink()
                deleted += 1
    except OSError as e:
        return error_response(f"PNGのクリアに失敗しました: {e}", 500)

    return jsonify({"deleted": deleted, "output_dir": output_dir_name})


@app.route("/screenshots/<path:filename>")
def screenshot_file(filename):
    return send_from_directory(OUTPUT_DIR, filename)


# ================================================================
# 起動
# ================================================================

if __name__ == "__main__":
    preferred_port = configured_server_port()
    port = find_available_server_port(SERVER_HOST, preferred_port)
    url = f"http://{SERVER_HOST}:{port}"
    if port != preferred_port:
        print(f"ポート {preferred_port} は使用中のため {port} を使用します")
    Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"起動しました → {url}")
    app.run(host=SERVER_HOST, port=port, debug=False, use_reloader=False)
