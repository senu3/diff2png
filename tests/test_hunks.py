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

    def test_config_accepts_inline_diff_default_mode(self):
        original = diff2png.INLINE_DIFF_DEFAULT_MODE
        try:
            client = diff2png.app.test_client()
            response = client.post("/api/config", json={"inline_diff_default_mode": "new"})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

            get_response = client.get("/api/config")
            self.assertEqual(get_response.status_code, 200, get_response.get_data(as_text=True))
            self.assertEqual(get_response.get_json()["inline_diff_default_mode"], "new")

            invalid_response = client.post("/api/config", json={"inline_diff_default_mode": "invalid"})
            self.assertEqual(invalid_response.status_code, 400)
        finally:
            diff2png.INLINE_DIFF_DEFAULT_MODE = original

    def test_config_accepts_inline_diff_max_changed_chars(self):
        original = diff2png.INLINE_DIFF_MAX_CHANGED_CHARS
        try:
            client = diff2png.app.test_client()
            response = client.post("/api/config", json={"inline_diff_max_changed_chars": 120})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

            get_response = client.get("/api/config")
            self.assertEqual(get_response.status_code, 200, get_response.get_data(as_text=True))
            self.assertEqual(get_response.get_json()["inline_diff_max_changed_chars"], 120)

            invalid_response = client.post("/api/config", json={"inline_diff_max_changed_chars": "invalid"})
            self.assertEqual(invalid_response.status_code, 400)
        finally:
            diff2png.INLINE_DIFF_MAX_CHANGED_CHARS = original

    def test_finalize_hunks_uses_inline_diff_default_mode(self):
        source_lines = [f"line {i}" for i in range(1, 11)]
        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            hunks = diff2png.finalize_hunks(
                [_raw_hunk(3)],
                repo_path=".",
                content_source={"type": "worktree"},
                config={
                    "diff_mode": "file",
                    "context_lines": 0,
                    "merge_threshold": 0,
                    "inline_diff_default_mode": "new",
                },
            )

        self.assertEqual(hunks[0]["inline_diff_mode"], "new")
        self.assertTrue(hunks[0]["inline_diff_enabled"])

        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            off_hunks = diff2png.finalize_hunks(
                [_raw_hunk(3)],
                repo_path=".",
                content_source={"type": "worktree"},
                config={
                    "diff_mode": "file",
                    "context_lines": 0,
                    "merge_threshold": 0,
                    "inline_diff_default_mode": "off",
                },
            )

        self.assertEqual(off_hunks[0]["inline_diff_mode"], "off")
        self.assertFalse(off_hunks[0]["inline_diff_enabled"])

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

    def test_merged_hunk_preserves_raw_diff_blocks(self):
        source_lines = [f"line {i}" for i in range(1, 101)]
        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            merged = diff2png.expand_and_merge(
                [_raw_hunk(10), _raw_hunk(19)],
                repo_path=".",
                context=0,
                merge_thresh=8,
                content_source={"type": "worktree"},
            )

        self.assertEqual(len(merged), 1)
        self.assertEqual([block["start"] for block in merged[0]["diff_blocks"]], [10, 19])

    def test_inline_replacement_mapping_resets_for_each_raw_diff_block(self):
        hunk = {
            "filepath": "sample.py",
            "start": 10,
            "end": 20,
            "old_start": 10,
            "changed_lines": [10, 20],
            "diff_lines": [
                "-alpha = make_value()",
                "+alpha = make_value(enabled=True)",
                "-omega = make_value()",
                "+omega = make_value(enabled=True)",
            ],
            "diff_blocks": [
                {
                    "start": 10,
                    "old_start": 10,
                    "changed_lines": [10],
                    "diff_lines": ["-alpha = make_value()", "+alpha = make_value(enabled=True)"],
                },
                {
                    "start": 20,
                    "old_start": 30,
                    "changed_lines": [20],
                    "diff_lines": ["-omega = make_value()", "+omega = make_value(enabled=True)"],
                },
            ],
        }

        replacements = diff2png._line_replacements_by_new_lineno(hunk)

        self.assertEqual(replacements[10][0], 10)
        self.assertEqual(replacements[20][0], 30)

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

        self.assertIn('class="changed inline-rendered"', html)
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

    def test_normal_view_does_not_pair_single_html_tag_rename(self):
        hunk = {
            "filepath": "sample.html",
            "start": 1,
            "end": 1,
            "default_start": 1,
            "default_end": 1,
            "old_start": 1,
            "changed_lines": [1],
            "diff_lines": ["-<table>", "+<div>"],
            "added_count": 1,
            "deleted_count": 1,
            "changed_count": 2,
        }
        source_lines = ["<div>"]

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

    def test_normal_view_pairs_html_tag_renames_in_structural_block(self):
        hunk = {
            "filepath": "sample.html",
            "start": 1,
            "end": 5,
            "default_start": 1,
            "default_end": 5,
            "old_start": 1,
            "changed_lines": [1, 2, 3, 4, 5],
            "diff_lines": [
                "-<table>",
                "-  <tr>",
                "-    <td>value</td>",
                "-  </tr>",
                "-</table>",
                "+<div>",
                "+  <div>",
                "+    <div>value</div>",
                "+  </div>",
                "+</div>",
            ],
            "added_count": 5,
            "deleted_count": 5,
            "changed_count": 10,
        }
        source_lines = [
            "<div>",
            "  <div>",
            "    <div>value</div>",
            "  </div>",
            "</div>",
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

        self.assertIn('<span class="inline-deleted">table</span>', html)
        self.assertIn('<span class="inline-deleted">td</span>', html)
        self.assertGreaterEqual(html.count('class="inline-added"'), 5)

    def test_normal_view_skips_inline_diff_when_hunk_flag_is_off(self):
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
            "inline_diff_enabled": False,
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

        self.assertNotIn('class="inline-added"', html)
        self.assertNotIn('class="inline-deleted"', html)
        self.assertIn('status = &quot;published&quot;', html)

    def test_normal_view_inline_diff_new_only_mode_shows_added_highlight_only(self):
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
            "inline_diff_mode": "new",
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
        self.assertNotIn('class="inline-deleted"', html)
        self.assertNotIn('draft', html)
        self.assertIn('published', html)

    def test_hunk_inline_diff_endpoint_updates_target_hunk(self):
        if shutil.which("git") is None:
            self.skipTest("git is not available")

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            hunks = [
                {
                    "filepath": "sample.py",
                    "start": 1,
                    "end": 1,
                    "default_start": 1,
                    "default_end": 1,
                    "old_start": 1,
                    "changed_lines": [1],
                    "diff_lines": ['-a = "old"', '+a = "new"'],
                    "added_count": 1,
                    "deleted_count": 1,
                    "changed_count": 2,
                    "inline_diff_enabled": True,
                },
                {
                    "filepath": "sample.py",
                    "start": 2,
                    "end": 2,
                    "default_start": 2,
                    "default_end": 2,
                    "old_start": 2,
                    "changed_lines": [2],
                    "diff_lines": ['-b = "old"', '+b = "new"'],
                    "added_count": 1,
                    "deleted_count": 1,
                    "changed_count": 2,
                    "inline_diff_enabled": True,
                },
            ]

            try:
                diff2png.ANALYSIS_SESSIONS.clear()
                analysis_id = diff2png.create_analysis_session(
                    str(repo),
                    hunks,
                    hunks,
                    {"type": "worktree"},
                    {"diff_mode": "file", "html_width": 960, "background_mode": "normal"},
                )
                client = diff2png.app.test_client()
                response = client.post(
                    "/api/hunk-inline-diff/1",
                    json={"repo_path": str(repo), "analysis_id": analysis_id, "enabled": False},
                )
            finally:
                diff2png.ANALYSIS_SESSIONS.clear()

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        self.assertFalse(data["hunk"]["inline_diff_enabled"])
        self.assertEqual(data["hunk"]["inline_diff_mode"], "off")
        self.assertTrue(hunks[0]["inline_diff_enabled"])
        self.assertFalse(hunks[1]["inline_diff_enabled"])
        self.assertEqual(hunks[1]["inline_diff_mode"], "off")

    def test_hunk_inline_diff_endpoint_accepts_new_only_mode(self):
        if shutil.which("git") is None:
            self.skipTest("git is not available")

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            hunks = [
                {
                    "filepath": "sample.py",
                    "start": 1,
                    "end": 1,
                    "default_start": 1,
                    "default_end": 1,
                    "old_start": 1,
                    "changed_lines": [1],
                    "diff_lines": ['-a = "old"', '+a = "new"'],
                    "added_count": 1,
                    "deleted_count": 1,
                    "changed_count": 2,
                    "inline_diff_mode": "full",
                    "inline_diff_enabled": True,
                },
            ]

            try:
                diff2png.ANALYSIS_SESSIONS.clear()
                analysis_id = diff2png.create_analysis_session(
                    str(repo),
                    hunks,
                    hunks,
                    {"type": "worktree"},
                    {"diff_mode": "file", "html_width": 960, "background_mode": "normal"},
                )
                client = diff2png.app.test_client()
                response = client.post(
                    "/api/hunk-inline-diff/0",
                    json={"repo_path": str(repo), "analysis_id": analysis_id, "mode": "new"},
                )
            finally:
                diff2png.ANALYSIS_SESSIONS.clear()

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        self.assertTrue(data["hunk"]["inline_diff_enabled"])
        self.assertEqual(data["hunk"]["inline_diff_mode"], "new")
        self.assertEqual(hunks[0]["inline_diff_mode"], "new")

    def test_normal_view_inline_diff_uses_changed_line_after_context_expansion(self):
        hunk = {
            "filepath": "sample.html",
            "start": 8,
            "end": 12,
            "default_start": 8,
            "default_end": 12,
            "orig_start": 10,
            "orig_end": 10,
            "old_start": 10,
            "changed_lines": [10],
            "diff_lines": ['-<div class="card">', '+<div class="card active">'],
            "added_count": 1,
            "deleted_count": 1,
            "changed_count": 2,
        }
        source_lines = [f"line {i}" for i in range(1, 8)] + [
            "<main>",
            "  <section>",
            '<div class="card active">',
            "  </section>",
            "</main>",
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

        self.assertIn('<td class="lineno">10</td>', html)
        self.assertIn('class="inline-added"', html)
        self.assertIn(' active', html)

    def test_normal_view_inlines_html_table_class_addition(self):
        hunk = {
            "filepath": "sample.html",
            "start": 1,
            "end": 1,
            "default_start": 1,
            "default_end": 1,
            "old_start": 1,
            "changed_lines": [1],
            "diff_lines": [
                '-<table class="table">',
                '+<table class="table table-striped">',
            ],
            "added_count": 1,
            "deleted_count": 1,
            "changed_count": 2,
        }
        source_lines = ['<table class="table table-striped">']

        replacements = diff2png._line_replacements_by_new_lineno(hunk)
        self.assertIn(1, replacements)

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
        self.assertIn(' table-striped', html)

    def test_normal_view_inlines_long_html_table_class_addition(self):
        old_text = '<table class="table">'
        new_text = '<table class="table table-striped table-hover table-bordered align-middle">'
        hunk = {
            "filepath": "sample.html",
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
            default_html = diff2png.build_code_html(
                hunk,
                ".",
                1,
                1,
                "2026-06-11 00:00:00",
                {"type": "worktree"},
                {"diff_mode": "file", "html_width": 960, "background_mode": "normal"},
            )
            expanded_html = diff2png.build_code_html(
                hunk,
                ".",
                1,
                1,
                "2026-06-11 00:00:00",
                {"type": "worktree"},
                {
                    "diff_mode": "file",
                    "html_width": 960,
                    "background_mode": "normal",
                    "inline_diff_max_changed_chars": 120,
                },
            )

        self.assertIn('class="inline-added"', default_html)
        self.assertIn('class="inline-added"', expanded_html)
        self.assertIn(' table-striped table-hover table-bordered align-middle', expanded_html)

    def test_normal_view_extreme_length_mismatch_is_not_inline_rendered(self):
        old_text = "const value = getConfig()"
        new_text = old_text + " + " + ("extraValue" * 40)
        hunk = {
            "filepath": "sample.js",
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

        replacements = diff2png._line_replacements_by_new_lineno(hunk)

        self.assertIn(1, replacements)

        with patch.object(diff2png, "read_source_lines", return_value=[new_text]):
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
        self.assertNotIn('class="changed inline-rendered"', html)
        self.assertNotIn('class="inline-added"', html)

    def test_normal_view_does_not_inline_highlight_leading_indent_change(self):
        hunk = {
            "filepath": "sample.py",
            "start": 1,
            "end": 1,
            "default_start": 1,
            "default_end": 1,
            "old_start": 1,
            "changed_lines": [1],
            "diff_lines": ["-\treturn value", "+    return value"],
            "added_count": 1,
            "deleted_count": 1,
            "changed_count": 2,
        }
        source_lines = ["    return value"]

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

        self.assertIn("return value", html)
        self.assertNotIn('class="inline-deleted"', html)
        self.assertNotIn('class="inline-added"', html)

    def test_normal_view_inline_diff_handles_merged_raw_hunks(self):
        hunk = {
            "filepath": "sample.html",
            "start": 8,
            "end": 18,
            "default_start": 8,
            "default_end": 18,
            "orig_start": 10,
            "orig_end": 16,
            "old_start": 10,
            "changed_lines": [10, 16],
            "diff_lines": [
                '-<div class="card">',
                '+<div class="card active">',
                '-<button disabled>',
                '+<button disabled aria-label="save">',
            ],
            "added_count": 2,
            "deleted_count": 2,
            "changed_count": 4,
        }
        source_lines = [f"line {i}" for i in range(1, 8)] + [
            "<main>",
            "  <section>",
            '<div class="card active">',
            "    <p>body</p>",
            "    <p>body</p>",
            "    <p>body</p>",
            "    <p>body</p>",
            "    <p>body</p>",
            '<button disabled aria-label="save">',
            "  </section>",
            "</main>",
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

        self.assertIn('<td class="lineno">10</td>', html)
        self.assertIn('<td class="lineno">16</td>', html)
        self.assertGreaterEqual(html.count('class="inline-added"'), 2)
        self.assertIn('aria-label', html)

    def test_normal_view_skips_inline_spans_when_replacement_is_too_large(self):
        old_text = 'config = { label: "' + ('a' * 80) + '", enabled: true, mode: "strict", retries: 3 }'
        new_text = 'config = { label: "' + ('b' * 80) + '", enabled: true, mode: "strict", retries: 3 }'
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
        self.assertIn('bbbbbbbbbb', html)

    def test_normal_view_uses_configured_inline_diff_max_changed_chars(self):
        old_text = 'config = { label: "' + ('a' * 30) + '", enabled: true, mode: "strict", retries: 3 }'
        new_text = 'config = { label: "' + ('b' * 30) + '", enabled: true, mode: "strict", retries: 3 }'
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
                {
                    "diff_mode": "file",
                    "html_width": 960,
                    "background_mode": "normal",
                    "inline_diff_max_changed_chars": 200,
                },
            )

        self.assertIn('class="inline-deleted"', html)
        self.assertIn('class="inline-added"', html)
        self.assertIn('aaaaaaaaaa', html)
        self.assertIn('bbbbbbbbbb', html)

    def test_normal_view_shows_inline_spans_for_equal_size_multi_line_replacement(self):
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

        self.assertIn('class="inline-deleted"', html)
        self.assertIn('class="inline-added"', html)
        self.assertIn('active', html)
        self.assertIn('old', html)
        self.assertIn('new', html)

    def test_normal_view_pairs_similar_lines_in_uneven_replacement_block(self):
        hunk = {
            "filepath": "sample.js",
            "start": 3,
            "end": 4,
            "default_start": 3,
            "default_end": 4,
            "old_start": 3,
            "changed_lines": [3, 4],
            "diff_lines": [
                "-const value = buildConfig()",
                "+const value = buildConfig({",
                '+  mode: "strict",',
            ],
            "added_count": 2,
            "deleted_count": 1,
            "changed_count": 3,
        }
        source_lines = [
            "const before = true",
            "",
            "const value = buildConfig({",
            '  mode: "strict",',
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

        self.assertIn('class="inline-deleted"', html)
        self.assertIn('class="inline-added"', html)
        self.assertIn("buildConfig", html)
        self.assertIn("strict", html)

    def test_normal_view_skips_inline_spans_for_dissimilar_uneven_block(self):
        hunk = {
            "filepath": "sample.js",
            "start": 3,
            "end": 4,
            "default_start": 3,
            "default_end": 4,
            "old_start": 3,
            "changed_lines": [3, 4],
            "diff_lines": [
                "-const value = buildConfig()",
                "+renderDashboard()",
                '+  mode: "strict",',
            ],
            "added_count": 2,
            "deleted_count": 1,
            "changed_count": 3,
        }
        source_lines = [
            "const before = true",
            "",
            "renderDashboard()",
            '  mode: "strict",',
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

    def test_normal_view_does_not_inline_pair_with_far_added_line(self):
        hunk = {
            "filepath": "sample.js",
            "start": 1,
            "end": 5,
            "default_start": 1,
            "default_end": 5,
            "old_start": 1,
            "changed_lines": [1, 2, 3, 4, 5],
            "diff_lines": [
                "-const target = config.value",
                "+const inserted0 = 0",
                "+const inserted1 = 1",
                "+const inserted2 = 2",
                "+const inserted3 = 3",
                "+const target = config.value.updated",
            ],
            "added_count": 5,
            "deleted_count": 1,
            "changed_count": 6,
        }
        source_lines = [
            "const inserted0 = 0",
            "const inserted1 = 1",
            "const inserted2 = 2",
            "const inserted3 = 3",
            "const target = config.value.updated",
        ]

        replacements = diff2png._line_replacements_by_new_lineno(hunk)

        self.assertNotIn(1, replacements)
        self.assertIn(5, replacements)

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

        first_row_end = html.index('</tr>')
        self.assertNotIn('class="inline-deleted"', html[:first_row_end])
        self.assertNotIn('class="inline-added"', html[:first_row_end])
        self.assertNotIn('class="deleted"', html)

    def test_normal_view_pairs_best_added_line_after_nearby_insertion(self):
        hunk = {
            "filepath": "sample.js",
            "start": 10,
            "end": 11,
            "default_start": 10,
            "default_end": 11,
            "old_start": 10,
            "changed_lines": [10, 11],
            "diff_lines": [
                "-const userName = profile.name",
                "+const inserted = config.value",
                "+const userName = profile.name.trim()",
            ],
            "added_count": 2,
            "deleted_count": 1,
            "changed_count": 3,
        }
        source_lines = [
            *[f"line {i}" for i in range(1, 10)],
            "const inserted = config.value",
            "const userName = profile.name.trim()",
        ]

        replacements = diff2png._line_replacements_by_new_lineno(hunk)

        self.assertNotIn(10, replacements)
        self.assertIn(11, replacements)

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

        inserted_row_start = html.index("const inserted = config.value")
        inserted_row_end = html.index("</tr>", inserted_row_start)
        inserted_row = html[html.rfind("<tr", 0, inserted_row_start):inserted_row_end]
        self.assertNotIn("inline-added", inserted_row)
        self.assertNotIn("inline-deleted", inserted_row)
        self.assertIn('const userName = profile.name<span class="inline-added">.trim()</span>', html)

    def test_normal_view_pairs_only_similar_lines_inside_replace_block(self):
        hunk = {
            "filepath": "sample.js",
            "start": 1,
            "end": 3,
            "default_start": 1,
            "default_end": 3,
            "old_start": 1,
            "changed_lines": [1, 2, 3],
            "diff_lines": [
                "-alphaTokenAlphaTokenAlphaToken()",
                "-betaValueBetaValueBetaValue()",
                "-omegaValueOmegaValueOmegaValue()",
                "+alphaTokenAlphaTokenAlphaToken.trim()",
                "+insertedCompletelyDifferentCall()",
                "+omegaValueOmegaValueOmegaValue.trim()",
            ],
            "added_count": 3,
            "deleted_count": 3,
            "changed_count": 6,
        }

        replacements = diff2png._line_replacements_by_new_lineno(hunk)

        self.assertEqual(
            {lineno: old_lineno for lineno, (old_lineno, _) in replacements.items()},
            {1: 1, 3: 3},
        )

    def test_normal_view_inline_matching_preserves_line_order(self):
        hunk = {
            "filepath": "sample.js",
            "start": 1,
            "end": 2,
            "default_start": 1,
            "default_end": 2,
            "old_start": 1,
            "changed_lines": [1, 2],
            "diff_lines": [
                "-alphaTokenAlphaTokenAlphaToken()",
                "-omegaValueOmegaValueOmegaValue()",
                "+omegaValueOmegaValueOmegaValue.trim()",
                "+alphaTokenAlphaTokenAlphaToken.trim()",
            ],
            "added_count": 2,
            "deleted_count": 2,
            "changed_count": 4,
        }

        replacements = diff2png._line_replacements_by_new_lineno(hunk)

        self.assertEqual(len(replacements), 1)
        self.assertNotEqual(
            {lineno: old_lineno for lineno, (old_lineno, _) in replacements.items()},
            {1: 2, 2: 1},
        )

    def test_normal_preview_inline_matching_uses_real_git_diff(self):
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

            target = repo / "sample.js"
            target.write_text("const target = config.value\n", encoding="utf-8")
            subprocess.run(["git", "add", "sample.js"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True, text=True)

            target.write_text(
                "\n".join([
                    "const inserted0 = 0",
                    "const inserted1 = 1",
                    "const inserted2 = 2",
                    "const inserted3 = 3",
                    "const target = config.value.updated",
                ]) + "\n",
                encoding="utf-8",
            )

            try:
                diff2png.CONTEXT_LINES = 0
                diff2png.MERGE_THRESHOLD = 8
                diff2png.DIFF_MODE = "file"
                diff2png.ANALYSIS_SESSIONS.clear()

                client = diff2png.app.test_client()
                analyze_response = client.post("/api/analyze", json={"repo_path": str(repo), "source_mode": "worktree"})
                self.assertEqual(analyze_response.status_code, 200, analyze_response.get_data(as_text=True))
                analysis_id = analyze_response.get_json()["analysis_id"]

                preview_response = client.post(
                    "/api/preview/0",
                    json={"repo_path": str(repo), "analysis_id": analysis_id},
                )
            finally:
                diff2png.CONTEXT_LINES = original_config["CONTEXT_LINES"]
                diff2png.MERGE_THRESHOLD = original_config["MERGE_THRESHOLD"]
                diff2png.DIFF_MODE = original_config["DIFF_MODE"]
                diff2png.ANALYSIS_SESSIONS.clear()

        self.assertEqual(preview_response.status_code, 200, preview_response.get_data(as_text=True))
        html = preview_response.get_data(as_text=True)
        first_row_start = html.index("const inserted0 = 0")
        first_row_end = html.index("</tr>", first_row_start)
        first_row = html[html.rfind("<tr", 0, first_row_start):first_row_end]
        self.assertNotIn("inline-added", first_row)
        self.assertNotIn("inline-deleted", first_row)
        self.assertIn('const target = config.value<span class="inline-added">.updated</span>', html)
        self.assertNotIn('<tr class="deleted">', html)

    def test_normal_preview_pairs_html_tag_block_renames_from_real_git_diff(self):
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

            target = repo / "sample.html"
            target.write_text(
                "\n".join([
                    "<table>",
                    "  <tr>",
                    "    <td>value</td>",
                    "  </tr>",
                    "</table>",
                ]) + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "sample.html"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True, text=True)

            target.write_text(
                "\n".join([
                    "<div>",
                    "  <div>",
                    "    <div>value</div>",
                    "  </div>",
                    "</div>",
                ]) + "\n",
                encoding="utf-8",
            )

            try:
                diff2png.CONTEXT_LINES = 0
                diff2png.MERGE_THRESHOLD = 8
                diff2png.DIFF_MODE = "file"
                diff2png.ANALYSIS_SESSIONS.clear()

                client = diff2png.app.test_client()
                analyze_response = client.post("/api/analyze", json={"repo_path": str(repo), "source_mode": "worktree"})
                self.assertEqual(analyze_response.status_code, 200, analyze_response.get_data(as_text=True))
                analysis_id = analyze_response.get_json()["analysis_id"]

                preview_response = client.post(
                    "/api/preview/0",
                    json={"repo_path": str(repo), "analysis_id": analysis_id},
                )
            finally:
                diff2png.CONTEXT_LINES = original_config["CONTEXT_LINES"]
                diff2png.MERGE_THRESHOLD = original_config["MERGE_THRESHOLD"]
                diff2png.DIFF_MODE = original_config["DIFF_MODE"]
                diff2png.ANALYSIS_SESSIONS.clear()

        self.assertEqual(preview_response.status_code, 200, preview_response.get_data(as_text=True))
        html = preview_response.get_data(as_text=True)
        self.assertIn('<span class="inline-deleted">table</span>', html)
        self.assertIn('<span class="inline-deleted">td</span>', html)
        self.assertGreaterEqual(html.count('class="inline-added"'), 5)

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
