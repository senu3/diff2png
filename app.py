#!/usr/bin/env python3
"""
diff_shot / app.py
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
OUTPUT_DIR = Path("diff_screenshots")
HTML_WIDTH = 960

app = Flask(__name__)


# ================================================================
# git / diff ユーティリティ
# ================================================================

def get_diff(repo_path: str) -> str:
    result = subprocess.run(
        ["git", "diff", "HEAD"],
        capture_output=True, text=True, encoding="utf-8",
        cwd=repo_path,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout


def parse_hunks(diff_text: str) -> list[dict]:
    hunks = []
    current_file = None
    file_re = re.compile(r"^\+\+\+ b/(.+)$")
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

    for line in diff_text.splitlines():
        m = file_re.match(line)
        if m:
            current_file = m.group(1)
            continue

        m = hunk_re.match(line)
        if m and current_file:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            hunks.append({
                "filepath": current_file,
                "start": start,
                "end": start + count - 1,
                "changed_lines": set(),
                "_cursor": start,
            })
            continue

        if hunks and current_file == hunks[-1]["filepath"]:
            h = hunks[-1]
            if line.startswith("+") and not line.startswith("+++"):
                h["changed_lines"].add(h["_cursor"])
                h["_cursor"] += 1
            elif line.startswith("-"):
                pass
            elif not line.startswith("\\"):
                h["_cursor"] += 1

    for h in hunks:
        h.pop("_cursor", None)
        h["changed_lines"] = sorted(h["changed_lines"])

    return hunks


def expand_and_merge(hunks: list[dict], repo_path: str, context: int, merge_thresh: int) -> list[dict]:
    by_file: dict[str, list[dict]] = {}
    for h in hunks:
        by_file.setdefault(h["filepath"], []).append(h)

    result = []
    for filepath, fhunks in by_file.items():
        try:
            total_lines = len((Path(repo_path) / filepath).read_text(encoding="utf-8").splitlines())
        except Exception:
            total_lines = 10 ** 6

        expanded = []
        for h in fhunks:
            expanded.append({
                "filepath": filepath,
                "start": max(1, h["start"] - context),
                "end": min(total_lines, h["end"] + context),
                "changed_lines": set(h["changed_lines"]),
            })

        merged = [expanded[0]]
        for h in expanded[1:]:
            prev = merged[-1]
            if h["start"] - prev["end"] <= merge_thresh:
                prev["end"] = max(prev["end"], h["end"])
                prev["changed_lines"] |= h["changed_lines"]
            else:
                merged.append(h)

        for h in merged:
            h["changed_lines"] = sorted(h["changed_lines"])

        result.extend(merged)

    return result


def read_lines(repo_path: str, filepath: str, start: int, end: int) -> list[tuple[int, str]]:
    try:
        lines = (Path(repo_path) / filepath).read_text(encoding="utf-8").splitlines()
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
    lines = read_lines(repo_path, hunk["filepath"], hunk["start"], hunk["end"])
    lang = detect_language(hunk["filepath"])
    changed_set = set(hunk["changed_lines"])

    rows = []
    for lineno, text in lines:
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
<div class="footer">{timestamp} | git diff HEAD</div>
</body></html>"""


# ================================================================
# PNG 出力
# ================================================================

def render_png(html: str, out_path: Path):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        height = page.evaluate("document.body.scrollHeight")
        page.set_viewport_size({"width": HTML_WIDTH + 32, "height": height + 32})
        page.screenshot(path=str(out_path), full_page=True)
        browser.close()


# ================================================================
# Flask ルート
# ================================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET", "POST"])
def config():
    global CONTEXT_LINES, MERGE_THRESHOLD, HTML_WIDTH, OUTPUT_DIR
    if request.method == "GET":
        return jsonify({
            "context_lines": CONTEXT_LINES,
            "merge_threshold": MERGE_THRESHOLD,
            "html_width": HTML_WIDTH,
            "output_dir": str(OUTPUT_DIR),
        })
    data = request.json
    try:
        if "context_lines" in data:
            CONTEXT_LINES = max(0, int(data["context_lines"]))
        if "merge_threshold" in data:
            MERGE_THRESHOLD = max(0, int(data["merge_threshold"]))
        if "html_width" in data:
            HTML_WIDTH = max(400, int(data["html_width"]))
        if "output_dir" in data and data["output_dir"].strip():
            OUTPUT_DIR = Path(data["output_dir"].strip())
    except (ValueError, TypeError) as e:
        return jsonify({"error": f"不正な値: {e}"}), 400
    return jsonify({"ok": True})


@app.route("/api/analyze", methods=["POST"])
def analyze():
    repo_path = request.json.get("repo_path", "").strip()
    if not repo_path or not Path(repo_path).is_dir():
        return jsonify({"error": "リポジトリパスが無効です"}), 400

    try:
        diff_text = get_diff(repo_path)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400

    if not diff_text.strip():
        return jsonify({"hunks": [], "message": "差分がありません"})

    hunks = parse_hunks(diff_text)
    hunks = expand_and_merge(hunks, repo_path, CONTEXT_LINES, MERGE_THRESHOLD)

    return jsonify({"hunks": hunks, "total": len(hunks)})


@app.route("/api/preview/<int:hunk_index>", methods=["POST"])
def preview(hunk_index: int):
    data = request.json
    repo_path = data["repo_path"]
    hunks = data["hunks"]
    total = len(hunks)

    if hunk_index < 0 or hunk_index >= total:
        return jsonify({"error": "無効なインデックス"}), 400

    hunk = hunks[hunk_index]
    hunk["changed_lines"] = hunk.get("changed_lines", [])
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = build_code_html(hunk, repo_path, hunk_index + 1, total, timestamp)
    return html


@app.route("/api/export", methods=["POST"])
def export():
    data = request.json
    repo_path = data["repo_path"]
    hunks = data["hunks"]
    indices = data.get("indices", list(range(len(hunks))))

    OUTPUT_DIR.mkdir(exist_ok=True)
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamp_disp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = len(hunks)
    saved = []

    for i in indices:
        hunk = hunks[i]
        hunk["changed_lines"] = hunk.get("changed_lines", [])
        safe = hunk["filepath"].replace("/", "_").replace("\\", "_")
        out_path = OUTPUT_DIR / f"{timestamp_str}_{i + 1 :03d}_{safe}_L{hunk['start']}.png"
        html = build_code_html(hunk, repo_path, i + 1, total, timestamp_disp)
        render_png(html, out_path)
        saved.append(str(out_path))

    return jsonify({"saved": saved, "count": len(saved)})


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
