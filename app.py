#!/usr/bin/env python3
"""
diff2png / app.py
Flask + Playwright によるエビデンス用 git diff スクリーンショットツール
"""

import re
import subprocess
import webbrowser
from datetime import datetime
from pathlib import Path
from threading import Timer

from flask import Flask, jsonify, render_template, request, send_from_directory

# ---- 設定 ----
CONTEXT_LINES = 5
MERGE_THRESHOLD = 8
APP_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR_NAME = "diff_screenshots"
OUTPUT_DIR = APP_ROOT / OUTPUT_DIR_NAME
HTML_WIDTH = 960
DIFF_MODE = "file"

app = Flask(__name__)


# ================================================================
# git / diff ユーティリティ
# ================================================================

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

    result = subprocess.run(
        cmd,
        capture_output=True, text=True, encoding="utf-8",
        cwd=repo_path,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout


def list_commits(repo_path: str, limit: int = 80) -> list[dict]:
    result = subprocess.run(
        ["git", "log", f"-n{max(1, min(limit, 200))}", "--pretty=format:%H\t%h\t%s"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo_path,
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


def is_git_repo(repo_path: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo_path,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


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
    return rp


def resolve_path_within(base_dir: Path, relative_path: str) -> Path:
    target = (base_dir / relative_path).resolve()
    try:
        target.relative_to(base_dir)
    except ValueError as e:
        raise ValueError("パスがリポジトリ外を指しています") from e
    return target


def parse_output_dir(value: str) -> tuple[str, Path]:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("output_dir は空にできません")

    candidate = Path(raw)
    if candidate.is_absolute():
        raise ValueError("output_dir は相対パスで指定してください")

    resolved = (APP_ROOT / candidate).resolve()
    try:
        resolved.relative_to(APP_ROOT)
    except ValueError as e:
        raise ValueError("output_dir はアプリ配下を指定してください") from e

    return candidate.as_posix(), resolved


def parse_hunks(diff_text: str) -> list[dict]:
    hunks = []
    current_file = None
    old_file = None
    new_file = None
    old_re = re.compile(r"^--- (?:a/(.+)|/dev/null)$")
    new_re = re.compile(r"^\+\+\+ (?:b/(.+)|/dev/null)$")
    hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

    for line in diff_text.splitlines():
        m = old_re.match(line)
        if m:
            old_file = m.group(1)
            continue

        m = new_re.match(line)
        if m:
            new_file = m.group(1)
            current_file = new_file or old_file
            continue

        m = hunk_re.match(line)
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
    import re

    min_indent = None
    for t in lines:
        if t is None:
            continue
        if t.strip() == "":
            continue
        m = re.match(r"^[ \t]*", t)
        if not m:
            continue
        indent_len = len(m.group(0))
        if min_indent is None or indent_len < min_indent:
            min_indent = indent_len

    if min_indent is None or min_indent == 0:
        return lines, 0

    new = [(s[min_indent:] if s is not None and len(s) >= min_indent else (s or "")) for s in lines]
    return new, min_indent


def expand_and_merge(hunks: list[dict], repo_path: str, context: int, merge_thresh: int) -> list[dict]:
    by_file: dict[str, list[dict]] = {}
    for h in hunks:
        by_file.setdefault(h["filepath"], []).append(h)

    result = []
    for filepath, fhunks in by_file.items():
        try:
            safe_path = resolve_path_within(Path(repo_path), filepath)
            total_lines = len(safe_path.read_text(encoding="utf-8").splitlines())
        except Exception:
            total_lines = 10 ** 6

        expanded = []
        for h in fhunks:
            expanded.append({
                "filepath": filepath,
                "start": max(1, h["start"] - context),
                "end": min(total_lines, h["end"] + context),
                "old_start": h.get("old_start", h["start"]),
                "changed_lines": set(h["changed_lines"]),
                "diff_lines": list(h.get("diff_lines", [])),
                "added_count": int(h.get("added_count", 0)),
                "deleted_count": int(h.get("deleted_count", 0)),
                "diff_cmd": h.get("diff_cmd"),
            })

        merged = [expanded[0]]
        for h in expanded[1:]:
            prev = merged[-1]
            if h["start"] - prev["end"] <= merge_thresh:
                prev["end"] = max(prev["end"], h["end"])
                prev["old_start"] = min(int(prev.get("old_start", prev["start"])), int(h.get("old_start", h["start"])))
                prev["changed_lines"] |= h["changed_lines"]
                prev["diff_lines"].extend(h.get("diff_lines", []))
                prev["added_count"] = int(prev.get("added_count", 0)) + int(h.get("added_count", 0))
                prev["deleted_count"] = int(prev.get("deleted_count", 0)) + int(h.get("deleted_count", 0))
            else:
                merged.append(h)

        for h in merged:
            h["changed_lines"] = sorted(h["changed_lines"])

        result.extend(merged)

    return result


def read_lines(repo_path: str, filepath: str, start: int, end: int) -> list[tuple[int, str]]:
    try:
        safe_path = resolve_path_within(Path(repo_path), filepath)
        lines = safe_path.read_text(encoding="utf-8").splitlines()
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

def build_code_html(hunk: dict, repo_path: str, hunk_index: int, total: int, timestamp: str) -> str:
    if DIFF_MODE == "patch":
        return build_patch_html(hunk, hunk_index, total, timestamp)

    if len(hunk.get("changed_lines", [])) == 0 and int(hunk.get("deleted_count", 0)) > 0 and hunk.get("diff_lines"):
        return build_patch_html(hunk, hunk_index, total, timestamp)

    lines = read_lines(repo_path, hunk["filepath"], hunk["start"], hunk["end"])
    # 共通インデントを除去（Codesnap風）
    raw_texts = [t for (_, t) in lines]
    stripped_texts, _ = _strip_common_indent_from_lines(raw_texts)
    lang = detect_language(hunk["filepath"])
    changed_set = set(hunk["changed_lines"])

    rows = []
    for idx, (lineno, text) in enumerate(lines):
        # text は共通インデントを削除したものを使う
        text = stripped_texts[idx]
        is_changed = lineno in changed_set
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        row_class = ' class="changed"' if is_changed else ""
        marker = "+" if is_changed else " "
        rows.append(
            f'<tr{row_class}>'
            f'<td class="lineno">{lineno}</td>'
            f'<td class="marker">{marker}</td>'
            f'<td class="code">{escaped}</td>'
            f'</tr>'
        )

    diff_cmd = hunk.get("diff_cmd", "git diff HEAD")
    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#fff;font-family:'Consolas','Menlo','Monaco',monospace;font-size:13px;
  color:#1a1a1a;width:{HTML_WIDTH}px;padding:16px}}
.header{{background:#1e1e2e;color:#cdd6f4;padding:10px 14px;border-radius:6px 6px 0 0;
  display:flex;justify-content:space-between;align-items:center;font-size:12px}}
.filepath{{color:#89dceb;font-weight:bold;word-break:break-all}}
.meta{{color:#6c7086;white-space:nowrap;margin-left:12px;flex-shrink:0}}
.code-block{{border:1px solid #e2e8f0;border-top:none;border-radius:0 0 6px 6px;overflow:hidden}}
table{{width:100%;border-collapse:collapse}}
tr{{border-bottom:1px solid #f1f5f9}}
tr:last-child{{border-bottom:none}}
tr.changed{{background:#fefce8}}
td{{vertical-align:top;padding:2px 0;line-height:1.6}}
td.lineno{{width:48px;text-align:right;color:#94a3b8;padding:2px 10px 2px 6px;
  border-right:1px solid #e2e8f0;background:#f8fafc;user-select:none}}
tr.changed td.lineno{{background:#fef9c3;color:#78716c}}
td.marker{{width:18px;text-align:center;color:#16a34a;font-weight:bold}}
td.code{{padding:2px 8px;white-space:pre}}
.footer{{margin-top:8px;font-size:11px;color:#94a3b8;text-align:right}}
</style></head><body>
<div class="header">
  <span class="filepath">{hunk['filepath']}</span>
  <span class="meta">L{hunk['start']}–{hunk['end']} | {hunk_index}/{total} | {lang}</span>
</div>
<div class="code-block"><table>
{''.join(rows)}
</table></div>
<div class="footer">{timestamp} | {diff_cmd}</div>
</body></html>"""


def build_patch_html(hunk: dict, hunk_index: int, total: int, timestamp: str) -> str:
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
            note = raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
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
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

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
    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#fff;font-family:'Consolas','Menlo','Monaco',monospace;font-size:13px;
  color:#1a1a1a;width:{HTML_WIDTH}px;padding:16px}}
.header{{background:#1e1e2e;color:#cdd6f4;padding:10px 14px;border-radius:6px 6px 0 0;
  display:flex;justify-content:space-between;align-items:center;font-size:12px}}
.filepath{{color:#89dceb;font-weight:bold;word-break:break-all}}
.meta{{color:#6c7086;white-space:nowrap;margin-left:12px;flex-shrink:0}}
.code-block{{border:1px solid #e2e8f0;border-top:none;border-radius:0 0 6px 6px;overflow:hidden}}
table{{width:100%;border-collapse:collapse}}
tr{{border-bottom:1px solid #f1f5f9}}
tr:last-child{{border-bottom:none}}
tr.added{{background:#ecfdf5}}
tr.deleted{{background:#fef2f2}}
tr.note td{{color:#64748b;font-style:italic}}
td{{vertical-align:top;padding:2px 0;line-height:1.6}}
td.lineno{{width:52px;text-align:right;color:#94a3b8;padding:2px 10px 2px 6px;
  border-right:1px solid #e2e8f0;background:#f8fafc;user-select:none}}
td.lineno.new{{border-right:none}}
td.marker{{width:18px;text-align:center;color:#64748b;font-weight:bold}}
tr.added td.marker{{color:#16a34a}}
tr.deleted td.marker{{color:#dc2626}}
td.code{{padding:2px 8px;white-space:pre}}
.footer{{margin-top:8px;font-size:11px;color:#94a3b8;text-align:right}}
</style></head><body>
<div class="header">
  <span class="filepath">{hunk['filepath']}</span>
  <span class="meta">-{hunk.get('old_start', hunk['start'])} +{hunk['start']} | {hunk_index}/{total} | {lang} | patch</span>
</div>
<div class="code-block"><table>
{''.join(rows)}
</table></div>
<div class="footer">{timestamp} | {diff_cmd}</div>
</body></html>"""


# ================================================================
# PNG 出力
# ================================================================

def render_png(page, html: str, out_path: Path):
    page.set_content(html, wait_until="load")
    height = page.evaluate("document.body.scrollHeight")
    page.set_viewport_size({"width": HTML_WIDTH + 32, "height": height + 32})
    page.screenshot(path=str(out_path), full_page=True)


def render_png_batch(items: list[tuple[str, Path]]):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        for html, out_path in items:
            render_png(page, html, out_path)
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
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(title="リポジトリフォルダを選択")
        root.destroy()
    except Exception as e:
        return jsonify({"error": f"フォルダ選択に失敗しました: {e}"}), 500

    if not selected:
        return jsonify({"cancelled": True})
    return jsonify({"repo_path": selected})


@app.route("/api/config", methods=["GET", "POST"])
def config():
    global CONTEXT_LINES, MERGE_THRESHOLD, HTML_WIDTH, OUTPUT_DIR, OUTPUT_DIR_NAME, DIFF_MODE
    if request.method == "GET":
        return jsonify({
            "context_lines": CONTEXT_LINES,
            "merge_threshold": MERGE_THRESHOLD,
            "html_width": HTML_WIDTH,
            "output_dir": OUTPUT_DIR_NAME,
            "diff_mode": DIFF_MODE,
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
        return jsonify({"error": "不正な差分ソースです"}), 400

    if not repo_path:
        return jsonify({"error": "リポジトリパスが無効です"}), 400

    try:
        repo = resolve_repo_path(repo_path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        # 人間向けの差分コマンド文字列を作成
        if source_mode == "worktree":
            diff_cmd_label = "git diff HEAD"
        elif source_mode == "staged":
            diff_cmd_label = "git diff --staged"
        elif source_mode == "commit":
            diff_cmd_label = f"git diff {target_ref}^ {target_ref}"
        else:
            diff_cmd_label = f"git diff {base_ref} {target_ref}"

        if DIFF_MODE == "patch":
            diff_text = get_diff(
                str(repo),
                context_lines=CONTEXT_LINES,
                merge_threshold=MERGE_THRESHOLD,
                source_mode=source_mode,
                base_ref=base_ref,
                target_ref=target_ref,
            )
        else:
            diff_text = get_diff(
                str(repo),
                source_mode=source_mode,
                base_ref=base_ref,
                target_ref=target_ref,
            )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400

    if not diff_text.strip():
        return jsonify({"hunks": [], "message": "差分がありません"})

    hunks = parse_hunks(diff_text)
    # 各hunkに差分コマンド表記を付与
    for h in hunks:
        h["diff_cmd"] = diff_cmd_label

    if DIFF_MODE == "file":
        hunks = expand_and_merge(hunks, str(repo), CONTEXT_LINES, MERGE_THRESHOLD)
        for h in hunks:
            h["changed_count"] = len(h.get("changed_lines", []))
    else:
        for h in hunks:
            h["changed_count"] = int(h.get("added_count", 0)) + int(h.get("deleted_count", 0))

    return jsonify({"hunks": hunks, "total": len(hunks)})


@app.route("/api/preview/<int:hunk_index>", methods=["POST"])
def preview(hunk_index: int):
    data = request.get_json(silent=True) or {}
    repo_path = str(data.get("repo_path", "")).strip()
    hunks = data.get("hunks")
    if not repo_path:
        return jsonify({"error": "リポジトリパスが無効です"}), 400
    if not isinstance(hunks, list):
        return jsonify({"error": "hunks が不正です"}), 400

    try:
        repo = resolve_repo_path(repo_path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    total = len(hunks)

    if hunk_index < 0 or hunk_index >= total:
        return jsonify({"error": "無効なインデックス"}), 400

    hunk = hunks[hunk_index]
    hunk["changed_lines"] = hunk.get("changed_lines", [])
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = build_code_html(hunk, str(repo), hunk_index + 1, total, timestamp)
    return html


@app.route("/api/export", methods=["POST"])
def export():
    data = request.get_json(silent=True) or {}
    repo_path = str(data.get("repo_path", "")).strip()
    hunks = data.get("hunks")
    if not repo_path:
        return jsonify({"error": "リポジトリパスが無効です"}), 400
    if not isinstance(hunks, list):
        return jsonify({"error": "hunks が不正です"}), 400

    try:
        repo = resolve_repo_path(repo_path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

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

    OUTPUT_DIR.mkdir(exist_ok=True)
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamp_disp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = len(hunks)
    render_items: list[tuple[str, Path]] = []
    saved: list[str] = []

    for i in normalized_indices:
        hunk = hunks[i]
        hunk["changed_lines"] = hunk.get("changed_lines", [])
        safe = hunk["filepath"].replace("/", "_").replace("\\", "_")
        out_path = OUTPUT_DIR / f"{timestamp_str}_{i + 1 :03d}_{safe}_L{hunk['start']}.png"
        html = build_code_html(hunk, str(repo), i + 1, total, timestamp_disp)
        render_items.append((html, out_path))
        saved.append(str(out_path))

    render_png_batch(render_items)

    return jsonify({"saved": saved, "count": len(saved), "output_dir": OUTPUT_DIR_NAME})


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
