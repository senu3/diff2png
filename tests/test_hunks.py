import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as diff2png


def _raw_hunk(start: int, end: int | None = None) -> dict:
    end = start if end is None else end
    return {
        "filepath": "sample.py",
        "start": start,
        "end": end,
        "old_start": start,
        "changed_lines": [start],
        "diff_lines": [f"+line {start} changed"],
        "added_count": 1,
        "deleted_count": 0,
    }


class HunkMergeTests(unittest.TestCase):
    def test_browse_repo_uses_directory_picker(self):
        with patch.object(diff2png, "choose_directory", return_value=r"C:\work\repo") as picker:
            client = diff2png.app.test_client()
            response = client.get("/api/browse")

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json(), {"repo_path": r"C:\work\repo"})
        picker.assert_called_once()

    def test_browse_output_dir_uses_directory_picker(self):
        with tempfile.TemporaryDirectory() as tmp:
            selected = str(Path(tmp).resolve())
            with patch.object(diff2png, "choose_directory", return_value=selected) as picker:
                client = diff2png.app.test_client()
                response = client.get("/api/browse-output-dir")

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json(), {"output_dir": selected})
        picker.assert_called_once()

    def test_merge_threshold_counts_unchanged_lines_between_hunks(self):
        source_lines = [f"line {i}" for i in range(1, 101)]
        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            merged = diff2png.expand_and_merge(
                [_raw_hunk(10), _raw_hunk(19)],
                repo_path=".",
                context=0,
                merge_thresh=8,
                content_source={"type": "worktree"},
            )
            not_merged = diff2png.expand_and_merge(
                [_raw_hunk(10), _raw_hunk(19)],
                repo_path=".",
                context=0,
                merge_thresh=7,
                content_source={"type": "worktree"},
            )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["start"], 10)
        self.assertEqual(merged[0]["end"], 19)
        self.assertEqual(len(not_merged), 2)

    def test_adjust_hunk_range_expands_shrinks_and_resets(self):
        hunk = {
            **_raw_hunk(8, 14),
            "default_start": 8,
            "default_end": 14,
            "orig_start": 10,
            "orig_end": 12,
            "changed_lines": [10, 12],
        }
        source_lines = [f"line {i}" for i in range(1, 31)]

        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            shrink_up = diff2png.adjust_hunk_range(hunk, ".", {"type": "worktree"}, "shrink_up")
            shrink_down = diff2png.adjust_hunk_range(hunk, ".", {"type": "worktree"}, "shrink_down")
            expand_up = diff2png.adjust_hunk_range(hunk, ".", {"type": "worktree"}, "expand_up")
            expand_down = diff2png.adjust_hunk_range(hunk, ".", {"type": "worktree"}, "expand_down")
            reset = diff2png.adjust_hunk_range(hunk, ".", {"type": "worktree"}, "reset")

        self.assertEqual((shrink_up["start"], shrink_up["end"]), (9, 14))
        self.assertTrue(shrink_up["range_adjusted"])
        self.assertEqual((shrink_down["start"], shrink_down["end"]), (9, 13))
        self.assertEqual((expand_up["start"], expand_up["end"]), (8, 13))
        self.assertEqual((expand_down["start"], expand_down["end"]), (8, 14))
        self.assertEqual((reset["start"], reset["end"]), (8, 14))
        self.assertFalse(reset["range_adjusted"])

    def test_adjust_hunk_range_clamps_to_file_bounds(self):
        hunk = {
            **_raw_hunk(28, 30),
            "default_start": 28,
            "default_end": 30,
        }
        source_lines = [f"line {i}" for i in range(1, 31)]

        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            expanded_up = diff2png.adjust_hunk_range(hunk, ".", {"type": "worktree"}, "expand_up")
            expanded_down = diff2png.adjust_hunk_range(hunk, ".", {"type": "worktree"}, "expand_down")

        self.assertEqual((expanded_up["start"], expanded_up["end"]), (27, 30))
        self.assertEqual((expanded_down["start"], expanded_down["end"]), (27, 30))

    def test_deleted_only_hunk_normal_view_keeps_deleted_row_with_context(self):
        hunk = {
            "filepath": "sample.py",
            "start": 4,
            "end": 6,
            "default_start": 4,
            "default_end": 6,
            "orig_start": 5,
            "old_start": 6,
            "changed_lines": [],
            "diff_lines": ["-removed line"],
            "added_count": 0,
            "deleted_count": 1,
            "changed_count": 1,
        }
        source_lines = [f"line {i}" for i in range(1, 11)]

        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            diff2png.adjust_hunk_range(hunk, ".", {"type": "worktree"}, "expand_down")
            html = diff2png.build_code_html(
                hunk,
                ".",
                1,
                1,
                "2026-06-11 00:00:00",
                {"type": "worktree"},
                {"diff_mode": "file", "html_width": 960, "background_mode": "normal"},
            )

        self.assertIn("L4–7", html)
        self.assertIn("line 5", html)
        self.assertIn('class="deleted"', html)
        self.assertIn("removed line", html)

    def test_normal_view_shows_inline_added_span_for_single_line_replacement(self):
        hunk = {
            "filepath": "sample.html",
            "start": 3,
            "end": 3,
            "default_start": 3,
            "default_end": 3,
            "old_start": 3,
            "changed_lines": [3],
            "diff_lines": ['-<div class="card">', '+<div class="card active">'],
            "added_count": 1,
            "deleted_count": 1,
            "changed_count": 2,
        }
        source_lines = ["<main>", "  <section>", '<div class="card active">', "  </section>"]

        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            html = diff2png.build_code_html(
                hunk,
                ".",
                1,
                1,
                "2026-06-11 00:00:00",
                {"type": "worktree"},
                {"diff_mode": "file", "html_width": 960, "background_mode": "normal"},
            )

        self.assertIn('class="changed"', html)
        self.assertIn('class="inline-added"', html)
        self.assertIn('&lt;div class=&quot;card', html)
        self.assertIn(' active', html)
        self.assertNotIn('class="inline-deleted"', html)

    def test_normal_view_shows_inline_old_and_new_text_for_short_replacement(self):
        hunk = {
            "filepath": "sample.py",
            "start": 1,
            "end": 1,
            "default_start": 1,
            "default_end": 1,
            "old_start": 1,
            "changed_lines": [1],
            "diff_lines": ['-status = "draft"', '+status = "published"'],
            "added_count": 1,
            "deleted_count": 1,
            "changed_count": 2,
        }
        source_lines = ['status = "published"']

        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            html = diff2png.build_code_html(
                hunk,
                ".",
                1,
                1,
                "2026-06-11 00:00:00",
                {"type": "worktree"},
                {"diff_mode": "file", "html_width": 960, "background_mode": "normal"},
            )

        self.assertIn('class="inline-added"', html)
        self.assertIn('class="inline-deleted"', html)
        self.assertIn('draft', html)
        self.assertIn('published', html)

    def test_normal_view_skips_inline_spans_when_replacement_is_too_large(self):
        old_text = 'message = "' + ('old-' * 20) + '"'
        new_text = 'message = "' + ('new-' * 20) + '"'
        hunk = {
            "filepath": "sample.py",
            "start": 1,
            "end": 1,
            "default_start": 1,
            "default_end": 1,
            "old_start": 1,
            "changed_lines": [1],
            "diff_lines": [f"-{old_text}", f"+{new_text}"],
            "added_count": 1,
            "deleted_count": 1,
            "changed_count": 2,
        }
        source_lines = [new_text]

        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            html = diff2png.build_code_html(
                hunk,
                ".",
                1,
                1,
                "2026-06-11 00:00:00",
                {"type": "worktree"},
                {"diff_mode": "file", "html_width": 960, "background_mode": "normal"},
            )

        self.assertNotIn('class="inline-deleted"', html)
        self.assertNotIn('class="inline-added"', html)
        self.assertIn('new-new-new', html)

    def test_normal_view_skips_inline_spans_for_multi_line_replacement(self):
        hunk = {
            "filepath": "sample.html",
            "start": 3,
            "end": 4,
            "default_start": 3,
            "default_end": 4,
            "old_start": 3,
            "changed_lines": [3, 4],
            "diff_lines": [
                '-<div class="card">',
                '-<span>old</span>',
                '+<div class="card active">',
                '+<span>new</span>',
            ],
            "added_count": 2,
            "deleted_count": 2,
            "changed_count": 4,
        }
        source_lines = [
            "<main>",
            "  <section>",
            '<div class="card active">',
            "<span>new</span>",
        ]

        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            html = diff2png.build_code_html(
                hunk,
                ".",
                1,
                1,
                "2026-06-11 00:00:00",
                {"type": "worktree"},
                {"diff_mode": "file", "html_width": 960, "background_mode": "normal"},
            )

        self.assertNotIn('class="inline-deleted"', html)
        self.assertNotIn('class="inline-added"', html)

    def test_normal_analysis_uses_zero_context_raw_diff(self):
        if shutil.which("git") is None:
            self.skipTest("git is not available")

        original_config = {
            "CONTEXT_LINES": diff2png.CONTEXT_LINES,
            "MERGE_THRESHOLD": diff2png.MERGE_THRESHOLD,
            "DIFF_MODE": diff2png.DIFF_MODE,
        }

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)

            target = repo / "sample.py"
            target.write_text(
                "\n".join(f"line {i:02d}" for i in range(1, 41)) + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "sample.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True, text=True)

            lines = target.read_text(encoding="utf-8").splitlines()
            lines[9] = "line 10 changed"
            lines[24] = "line 25 changed"
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")

            try:
                diff2png.CONTEXT_LINES = 5
                diff2png.MERGE_THRESHOLD = 0
                diff2png.DIFF_MODE = "file"
                diff2png.ANALYSIS_SESSIONS.clear()

                client = diff2png.app.test_client()
                response = client.post("/api/analyze", json={"repo_path": str(repo), "source_mode": "worktree"})
            finally:
                diff2png.CONTEXT_LINES = original_config["CONTEXT_LINES"]
                diff2png.MERGE_THRESHOLD = original_config["MERGE_THRESHOLD"]
                diff2png.DIFF_MODE = original_config["DIFF_MODE"]
                diff2png.ANALYSIS_SESSIONS.clear()

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        hunks = response.get_json()["hunks"]
        self.assertEqual([(h["start"], h["end"]) for h in hunks], [(5, 15), (20, 30)])


if __name__ == "__main__":
    unittest.main()
