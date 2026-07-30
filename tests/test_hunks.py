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
    def test_source_selection_expands_to_contiguous_history_range(self):
        commits = [
            {"hash": "c3", "short": "c3", "subject": "third"},
            {"hash": "c2", "short": "c2", "subject": "second"},
            {"hash": "c1", "short": "c1", "subject": "first"},
        ]

        with (
            patch.object(diff2png, "list_commits", return_value=commits),
            patch.object(
                diff2png,
                "_commit_parent_or_empty_tree",
                return_value=("c1-parent", False),
            ),
        ):
            selection = diff2png.resolve_source_selection(
                ".",
                ["unstaged", "commit:c1"],
            )

        self.assertEqual(
            selection["keys"],
            ["unstaged", "staged", "commit:c3", "commit:c2", "commit:c1"],
        )
        self.assertEqual(selection["base_source"], {"type": "ref", "ref": "c1-parent"})
        self.assertEqual(selection["target_source"], {"type": "worktree"})
        self.assertEqual(selection["summary"], "3コミット + ステージ済み + 未ステージ")

    def test_combined_source_diff_uses_index_as_staged_target(self):
        selection = {
            "base_source": {"type": "ref", "ref": "base-ref"},
            "target_source": {"type": "index"},
        }
        result = subprocess.CompletedProcess([], 0, "diff text", "")

        with patch.object(diff2png, "run_git", return_value=result) as run_git:
            diff_text, label = diff2png.get_diff_for_source_selection(
                ".",
                selection,
                context_lines=4,
                merge_threshold=8,
            )

        self.assertEqual(diff_text, "diff text")
        self.assertEqual(label, "git diff --cached base-ref -U4 --inter-hunk-context=8")
        run_git.assert_called_once_with(
            ".",
            ["diff", "--cached", "base-ref", "-U4", "--inter-hunk-context=8"],
        )

    def test_analyze_combines_selected_history_and_current_changes(self):
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

            target = repo / "sample.txt"
            target.write_text("base\none\ntwo\nstage\nwork\n", encoding="utf-8")
            subprocess.run(["git", "add", "sample.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True, text=True)

            target.write_text("base\none changed\ntwo\nstage\nwork\n", encoding="utf-8")
            subprocess.run(["git", "add", "sample.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "first"], cwd=repo, check=True, capture_output=True, text=True)
            first_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            target.write_text("base\none changed\ntwo changed\nstage\nwork\n", encoding="utf-8")
            subprocess.run(["git", "add", "sample.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "second"], cwd=repo, check=True, capture_output=True, text=True)
            second_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            target.write_text("base\none changed\ntwo changed\nstage changed\nwork\n", encoding="utf-8")
            subprocess.run(["git", "add", "sample.txt"], cwd=repo, check=True)
            target.write_text(
                "base\none changed\ntwo changed\nstage changed\nwork changed\n",
                encoding="utf-8",
            )

            try:
                diff2png.CONTEXT_LINES = 0
                diff2png.MERGE_THRESHOLD = 8
                diff2png.DIFF_MODE = "file"
                diff2png.ANALYSIS_SESSIONS.clear()

                client = diff2png.app.test_client()
                response = client.post(
                    "/api/analyze",
                    json={
                        "repo_path": str(repo),
                        "source_keys": ["unstaged", f"commit:{first_commit}"],
                    },
                )
            finally:
                diff2png.CONTEXT_LINES = original_config["CONTEXT_LINES"]
                diff2png.MERGE_THRESHOLD = original_config["MERGE_THRESHOLD"]
                diff2png.DIFF_MODE = original_config["DIFF_MODE"]

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        self.assertEqual(
            data["source_selection"]["keys"],
            [
                "unstaged",
                "staged",
                f"commit:{second_commit}",
                f"commit:{first_commit}",
            ],
        )
        self.assertEqual(
            data["source_selection"]["summary"],
            "2コミット + ステージ済み + 未ステージ",
        )
        self.assertGreaterEqual(len(data["hunks"]), 1)
        analysis_id = data["analysis_id"]
        self.assertEqual(
            diff2png.ANALYSIS_SESSIONS[analysis_id]["content_source"],
            {"type": "worktree"},
        )
        diff2png.ANALYSIS_SESSIONS.clear()

    def test_configured_server_port_uses_default_and_validates_override(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(diff2png.configured_server_port(), 5127)

        self.assertEqual(diff2png.configured_server_port("6200"), 6200)
        for invalid in ("invalid", "0", "65536"):
            with self.assertRaises(ValueError):
                diff2png.configured_server_port(invalid)

    def test_find_available_server_port_uses_next_port_when_preferred_is_busy(self):
        with patch.object(
            diff2png,
            "can_bind_server_port",
            side_effect=lambda _host, port: port == 5129,
        ):
            port = diff2png.find_available_server_port("127.0.0.1", 5127, 5)

        self.assertEqual(port, 5129)

    def test_find_available_server_port_fails_when_scan_range_is_busy(self):
        with patch.object(diff2png, "can_bind_server_port", return_value=False):
            with self.assertRaises(RuntimeError):
                diff2png.find_available_server_port("127.0.0.1", 5127, 3)

    def test_common_indent_matches_tabs_and_spaces_by_visual_width(self):
        lines = ["\t\tchanged()", "        context()"]

        stripped, width = diff2png._strip_common_indent_from_lines(lines)

        self.assertEqual(stripped, ["changed()", "context()"])
        self.assertEqual(width, 8)

    def test_common_indent_removes_equivalent_mixed_indentation(self):
        stripped, width = diff2png._strip_common_indent_from_lines([
            "\t\tchanged()",
            "\t    context()",
        ])

        self.assertEqual(stripped, ["changed()", "context()"])
        self.assertEqual(width, 8)

    def test_common_indent_preserves_visual_remainder_when_cutting_through_tab(self):
        stripped, width = diff2png._strip_common_indent_from_lines([
            "\tchanged()",
            "  context()",
        ])

        self.assertEqual(stripped, ["  changed()", "context()"])
        self.assertEqual(width, 2)

    def test_common_indent_normalizes_remaining_multiple_tabs_by_visual_width(self):
        stripped, width = diff2png._strip_common_indent_from_lines([
            "\t\tchanged()",
            "  context()",
        ])

        self.assertEqual(stripped, ["      changed()", "context()"])
        self.assertEqual(width, 2)

    def test_normal_view_strips_visually_equal_mixed_indentation(self):
        hunk = {
            "filepath": "sample.py",
            "start": 1,
            "end": 2,
            "old_start": 1,
            "changed_lines": [1],
            "diff_lines": ["-\t\told_value()", "+\t\tnew_value()", "         context()"],
            "added_count": 1,
            "deleted_count": 1,
        }
        source_lines = ["\t\tnew_value()", "        context()"]

        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            html = diff2png.build_code_html(
                hunk,
                ".",
                1,
                1,
                "2026-07-21 00:00:00",
                {"type": "worktree"},
                {"diff_mode": "file", "html_width": 960, "background_mode": "normal"},
            )

        self.assertNotIn('<td class="code">\t', html)
        self.assertIn("new_value", html)
        self.assertIn("context()", html)

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

    def test_config_accepts_deleted_diff_mode(self):
        original = diff2png.DIFF_MODE
        try:
            client = diff2png.app.test_client()
            response = client.post("/api/config", json={"diff_mode": "deleted"})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

            get_response = client.get("/api/config")
            self.assertEqual(get_response.status_code, 200, get_response.get_data(as_text=True))
            self.assertEqual(get_response.get_json()["diff_mode"], "deleted")

            invalid_response = client.post("/api/config", json={"diff_mode": "unknown"})
            self.assertEqual(invalid_response.status_code, 400)
        finally:
            diff2png.DIFF_MODE = original

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

    def test_adjust_hunk_range_can_shrink_past_changes_to_one_line(self):
        source_lines = [f"line {i}" for i in range(1, 31)]
        shrink_up_hunk = {
            **_raw_hunk(8, 14),
            "default_start": 8,
            "default_end": 14,
            "orig_start": 10,
            "orig_end": 12,
            "changed_lines": [10, 12],
        }
        shrink_down_hunk = dict(shrink_up_hunk)

        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            shrink_up = diff2png.adjust_hunk_range(
                shrink_up_hunk,
                ".",
                {"type": "worktree"},
                "shrink_up",
                step=20,
            )
            shrink_down = diff2png.adjust_hunk_range(
                shrink_down_hunk,
                ".",
                {"type": "worktree"},
                "shrink_down",
                step=20,
            )

        self.assertEqual((shrink_up["start"], shrink_up["end"]), (14, 14))
        self.assertEqual((shrink_down["start"], shrink_down["end"]), (8, 8))

    def test_normal_view_hides_changes_and_deletion_markers_outside_adjusted_range(self):
        hunk = {
            "filepath": "sample.py",
            "start": 1,
            "end": 5,
            "default_start": 1,
            "default_end": 5,
            "old_start": 1,
            "changed_lines": [4],
            "diff_lines": [
                " first",
                "-removed",
                " second",
                " third",
                "+added",
                " fourth",
            ],
            "added_count": 1,
            "deleted_count": 1,
            "changed_count": 2,
        }
        source_lines = ["first", "second", "third", "added", "fourth"]

        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            diff2png.adjust_hunk_range(
                hunk,
                ".",
                {"type": "worktree"},
                "shrink_up",
                step=20,
            )
            html = diff2png.build_code_html(
                hunk,
                ".",
                1,
                1,
                "2026-07-30 00:00:00",
                {"type": "worktree"},
                {"diff_mode": "file", "html_width": 960, "background_mode": "normal"},
            )

        self.assertEqual((hunk["start"], hunk["end"]), (5, 5))
        self.assertIn('<td class="code">fourth</td>', html)
        self.assertNotIn('<td class="code">added</td>', html)
        self.assertNotIn("removed", html)
        self.assertNotIn('<tr class="deleted">', html)
        self.assertNotIn("lineno deletion-before", html)
        self.assertNotIn("lineno deletion-after", html)

    def test_deleted_only_hunk_hides_deletion_outside_adjusted_range(self):
        hunk = {
            "filepath": "sample.py",
            "start": 1,
            "end": 5,
            "default_start": 1,
            "default_end": 5,
            "orig_start": 2,
            "old_start": 2,
            "changed_lines": [],
            "diff_lines": ["-removed"],
            "diff_blocks": [
                {"start": 2, "old_start": 2, "diff_lines": ["-removed"]},
            ],
            "added_count": 0,
            "deleted_count": 1,
            "changed_count": 1,
        }
        source_lines = ["first", "second", "third", "fourth", "fifth"]

        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            diff2png.adjust_hunk_range(
                hunk,
                ".",
                {"type": "worktree"},
                "shrink_up",
                step=2,
            )
            html = diff2png.build_code_html(
                hunk,
                ".",
                1,
                1,
                "2026-07-30 00:00:00",
                {"type": "worktree"},
                {"diff_mode": "file", "html_width": 960, "background_mode": "normal"},
            )

        self.assertEqual((hunk["start"], hunk["end"]), (3, 5))
        self.assertNotIn("removed", html)
        self.assertNotIn('<tr class="deleted">', html)

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
        self.assertIn('<td class="lineno"></td><td class="marker">-</td>', html)
        self.assertIn("removed line", html)

    def test_deleted_only_merged_hunks_keep_each_deletion_at_its_anchor(self):
        hunk = {
            "filepath": "sample.py",
            "start": 1,
            "end": 8,
            "orig_start": 2,
            "orig_end": 7,
            "old_start": 3,
            "changed_lines": [],
            "diff_lines": ["-first removed", "-second removed"],
            "diff_blocks": [
                {"start": 2, "old_start": 3, "diff_lines": ["-first removed"]},
                {"start": 7, "old_start": 9, "diff_lines": ["-second removed"]},
            ],
            "added_count": 0,
            "deleted_count": 2,
        }
        source_lines = [f"line {i}" for i in range(1, 9)]

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

        self.assertLess(html.index("line 2"), html.index("first removed"))
        self.assertLess(html.index("first removed"), html.index("line 3"))
        self.assertLess(html.index("line 7"), html.index("second removed"))
        self.assertLess(html.index("second removed"), html.index("line 8"))
        self.assertEqual(
            html.count('<td class="lineno"></td><td class="marker">-</td>'),
            2,
        )
        self.assertNotIn('<td class="lineno">3</td><td class="marker">-</td>', html)
        self.assertNotIn('<td class="lineno">9</td><td class="marker">-</td>', html)

    def test_normal_view_marks_unpaired_line_deletion_before_following_line(self):
        hunk = {
            "filepath": "sample.py",
            "start": 1,
            "end": 4,
            "default_start": 1,
            "default_end": 4,
            "old_start": 1,
            "changed_lines": [3],
            "diff_lines": [
                " first",
                "-removed",
                " second",
                "+added",
                " third",
            ],
            "added_count": 1,
            "deleted_count": 1,
            "changed_count": 2,
        }
        source_lines = ["first", "second", "added", "third"]

        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            html = diff2png.build_code_html(
                hunk,
                ".",
                1,
                1,
                "2026-07-26 00:00:00",
                {"type": "worktree"},
                {"diff_mode": "file", "html_width": 960, "background_mode": "normal"},
            )

        self.assertIn('<td class="lineno deletion-before">2</td>', html)
        self.assertNotIn('<td class="lineno deletion-before">3</td>', html)
        self.assertNotIn("removed", html)
        self.assertIn("td.lineno.deletion-before::after", html)

    def test_normal_view_shows_two_isolated_deletions_in_inline_on(self):
        hunk = {
            "filepath": "sample.py",
            "start": 1,
            "end": 6,
            "default_start": 1,
            "default_end": 6,
            "old_start": 1,
            "changed_lines": [5],
            "diff_lines": [
                " first",
                "-removed one",
                "-removed two",
                " second",
                " third",
                " fourth",
                "+added",
                " fifth",
            ],
            "added_count": 1,
            "deleted_count": 2,
            "changed_count": 3,
            "inline_diff_mode": "full",
        }
        hunk["diff_blocks"] = [{
            "start": 1,
            "old_start": 1,
            "changed_lines": [5],
            "diff_lines": list(hunk["diff_lines"]),
        }]
        source_lines = ["first", "second", "third", "fourth", "added", "fifth"]

        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            html = diff2png.build_code_html(
                hunk,
                ".",
                1,
                1,
                "2026-07-30 00:00:00",
                {"type": "worktree"},
                {"diff_mode": "file", "html_width": 960, "background_mode": "normal"},
            )

        self.assertEqual(html.count('<tr class="deleted">'), 2)
        self.assertLess(html.index("first"), html.index("removed one"))
        self.assertLess(html.index("removed one"), html.index("removed two"))
        self.assertLess(html.index("removed two"), html.index("second"))
        self.assertEqual(
            html.count('<td class="lineno"></td><td class="marker">-</td>'),
            2,
        )
        self.assertNotIn('<td class="lineno">2</td><td class="marker">-</td>', html)
        self.assertNotIn('<td class="lineno">3</td><td class="marker">-</td>', html)
        self.assertNotIn('<td class="lineno deletion-before">', html)
        self.assertNotIn('<td class="lineno deletion-after">', html)

    def test_normal_view_shows_one_isolated_deletion_in_inline_on(self):
        hunk = {
            "filepath": "sample.py",
            "start": 1,
            "end": 6,
            "default_start": 1,
            "default_end": 6,
            "old_start": 1,
            "changed_lines": [5],
            "diff_lines": [
                " first",
                "-removed",
                " second",
                " third",
                " fourth",
                "+added",
                " fifth",
            ],
            "added_count": 1,
            "deleted_count": 1,
            "changed_count": 2,
            "inline_diff_mode": "full",
        }
        source_lines = ["first", "second", "third", "fourth", "added", "fifth"]

        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            html = diff2png.build_code_html(
                hunk,
                ".",
                1,
                1,
                "2026-07-30 00:00:00",
                {"type": "worktree"},
                {"diff_mode": "file", "html_width": 960, "background_mode": "normal"},
            )

        self.assertIn('<tr class="deleted">', html)
        self.assertIn('<td class="lineno"></td><td class="marker">-</td>', html)
        self.assertLess(html.index("first"), html.index("removed"))
        self.assertLess(html.index("removed"), html.index("second"))
        self.assertNotIn('<td class="lineno deletion-before">', html)

    def test_normal_view_keeps_isolated_deletion_as_marker_outside_inline_on(self):
        base_hunk = {
            "filepath": "sample.py",
            "start": 1,
            "end": 5,
            "default_start": 1,
            "default_end": 5,
            "old_start": 1,
            "changed_lines": [4],
            "diff_lines": [
                " first",
                "-removed",
                " second",
                " third",
                "+added",
                " fourth",
            ],
            "added_count": 1,
            "deleted_count": 1,
            "changed_count": 2,
        }
        source_lines = ["first", "second", "third", "added", "fourth"]

        for inline_diff_mode in ("new", "off"):
            with self.subTest(inline_diff_mode=inline_diff_mode):
                hunk = {**base_hunk, "inline_diff_mode": inline_diff_mode}
                with patch.object(diff2png, "read_source_lines", return_value=source_lines):
                    html = diff2png.build_code_html(
                        hunk,
                        ".",
                        1,
                        1,
                        "2026-07-30 00:00:00",
                        {"type": "worktree"},
                        {"diff_mode": "file", "html_width": 960, "background_mode": "normal"},
                    )

                self.assertIn('<td class="lineno deletion-before">2</td>', html)
                self.assertNotIn('<tr class="deleted">', html)
                self.assertNotIn("removed", html)

    def test_normal_view_keeps_three_isolated_deletions_as_marker(self):
        hunk = {
            "filepath": "sample.py",
            "start": 1,
            "end": 5,
            "default_start": 1,
            "default_end": 5,
            "old_start": 1,
            "changed_lines": [4],
            "diff_lines": [
                " first",
                "-removed one",
                "-removed two",
                "-removed three",
                " second",
                " third",
                "+added",
                " fourth",
            ],
            "added_count": 1,
            "deleted_count": 3,
            "changed_count": 4,
            "inline_diff_mode": "full",
        }
        source_lines = ["first", "second", "third", "added", "fourth"]

        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            html = diff2png.build_code_html(
                hunk,
                ".",
                1,
                1,
                "2026-07-30 00:00:00",
                {"type": "worktree"},
                {"diff_mode": "file", "html_width": 960, "background_mode": "normal"},
            )

        self.assertIn('<td class="lineno deletion-before">2</td>', html)
        self.assertNotIn('<tr class="deleted">', html)
        self.assertNotIn("removed one", html)

    def test_normal_view_marks_unpaired_eof_deletion_after_last_line(self):
        hunk = {
            "filepath": "sample.py",
            "start": 1,
            "end": 3,
            "default_start": 1,
            "default_end": 3,
            "old_start": 1,
            "changed_lines": [2],
            "diff_lines": [
                " first",
                "+added",
                " second",
                "-removed",
            ],
            "added_count": 1,
            "deleted_count": 1,
            "changed_count": 2,
        }
        source_lines = ["first", "added", "second"]

        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            html = diff2png.build_code_html(
                hunk,
                ".",
                1,
                1,
                "2026-07-26 00:00:00",
                {"type": "worktree"},
                {"diff_mode": "file", "html_width": 960, "background_mode": "normal"},
            )

        self.assertIn('<td class="lineno deletion-after">3</td>', html)

    def test_normal_view_does_not_mark_paired_replacement_as_line_deletion(self):
        hunk = {
            "filepath": "sample.py",
            "start": 1,
            "end": 1,
            "default_start": 1,
            "default_end": 1,
            "old_start": 1,
            "changed_lines": [1],
            "diff_lines": ["-old value", "+new value"],
            "added_count": 1,
            "deleted_count": 1,
            "changed_count": 2,
            "inline_diff_mode": "off",
        }

        with patch.object(diff2png, "read_source_lines", return_value=["new value"]):
            html = diff2png.build_code_html(
                hunk,
                ".",
                1,
                1,
                "2026-07-26 00:00:00",
                {"type": "worktree"},
                {"diff_mode": "file", "html_width": 960, "background_mode": "normal"},
            )

        self.assertNotIn('<td class="lineno deletion-before">', html)
        self.assertNotIn('<td class="lineno deletion-after">', html)

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

    def test_inline_diff_merges_changes_split_by_short_punctuation(self):
        html = diff2png._inline_diff_html("value = foo.bar", "value = baz.qux")

        self.assertIn(
            '<span class="inline-deleted">foo.bar</span><span class="inline-added">baz.qux</span>',
            html,
        )
        self.assertNotIn('</span>.<span', html)

    def test_inline_diff_merges_multiple_changes_separated_by_short_equal_text(self):
        html = diff2png._inline_diff_html(
            "size = old + left",
            "size = new + right",
        )

        self.assertIn(
            '<span class="inline-deleted">old + left</span>'
            '<span class="inline-added">new + right</span>',
            html,
        )
        self.assertEqual(html.count('class="inline-deleted"'), 1)
        self.assertEqual(html.count('class="inline-added"'), 1)

    def test_inline_diff_merges_changes_separated_by_twelve_equal_characters(self):
        html = diff2png._inline_diff_html(
            "value = old + middle + left",
            "value = new + middle + right",
        )

        self.assertIn(
            '<span class="inline-deleted">old + middle + left</span>'
            '<span class="inline-added">new + middle + right</span>',
            html,
        )
        self.assertEqual(html.count('class="inline-deleted"'), 1)
        self.assertEqual(html.count('class="inline-added"'), 1)

    def test_inline_diff_absorbs_leading_bang_into_larger_change_across_variable(self):
        html = diff2png._inline_diff_html(
            "isFeatureFlagEnabled && oldValue",
            "!isFeatureFlagEnabled && replacementValue",
        )

        self.assertIn(
            '<span class="inline-deleted">isFeatureFlagEnabled &amp;&amp; oldValue</span>'
            '<span class="inline-added">!isFeatureFlagEnabled &amp;&amp; replacementValue</span>',
            html,
        )
        self.assertEqual(html.count('class="inline-deleted"'), 1)
        self.assertEqual(html.count('class="inline-added"'), 1)

    def test_inline_diff_absorbs_bang_removal_from_parenthesized_expression(self):
        html = diff2png._inline_diff_html(
            "(!isFeatureFlagEnabled && oldValue",
            "(isFeatureFlagEnabled && replacementValue",
        )

        self.assertIn(
            '<span class="inline-deleted">(!isFeatureFlagEnabled &amp;&amp; oldValue</span>'
            '<span class="inline-added">(isFeatureFlagEnabled &amp;&amp; replacementValue</span>',
            html,
        )
        self.assertEqual(html.count('class="inline-deleted"'), 1)
        self.assertEqual(html.count('class="inline-added"'), 1)

    def test_inline_diff_keeps_if_condition_and_tail_changes_separate(self):
        html = diff2png._inline_diff_html(
            "if (!legacyCondition) return;",
            "if (newCondition) {",
        )

        self.assertIn(
            'if (<span class="inline-deleted">!legacyCondition</span>'
            '<span class="inline-added">newCondition</span>) ',
            html,
        )
        self.assertIn('<span class="inline-deleted">return;</span>', html)
        self.assertIn('<span class="inline-added">{</span>', html)
        self.assertEqual(html.count('class="inline-deleted"'), 2)
        self.assertEqual(html.count('class="inline-added"'), 2)

    def test_inline_diff_keeps_nested_if_condition_deletion_before_tail_change(self):
        html = diff2png._inline_diff_html(
            "if (!(enabled and legacyCondition)) return;",
            "if (enabled) {",
        )

        self.assertIn(
            'if (<span class="inline-deleted">!(enabled and legacyCondition)</span>'
            '<span class="inline-added">enabled</span>) ',
            html,
        )
        self.assertIn('<span class="inline-deleted">return;</span>', html)
        self.assertIn('<span class="inline-added">{</span>', html)
        self.assertEqual(html.count('class="inline-deleted"'), 2)
        self.assertEqual(html.count('class="inline-added"'), 2)

    def test_inline_diff_does_not_absorb_two_small_changes_across_long_equal_text(self):
        html = diff2png._inline_diff_html(
            "a + very_long_variable_name + b",
            "x + very_long_variable_name + y",
        )

        self.assertEqual(html.count('class="inline-deleted"'), 2)
        self.assertEqual(html.count('class="inline-added"'), 2)

    def test_inline_diff_keeps_deletions_for_two_distant_changes(self):
        html = diff2png._inline_diff_html(
            "old_value = unchanged_middle_section + left",
            "new_value = unchanged_middle_section + right",
        )

        self.assertIn('<span class="inline-deleted">old_value</span>', html)
        self.assertIn('<span class="inline-deleted">left</span>', html)
        self.assertIn('<span class="inline-added">new_value</span>', html)
        self.assertIn('<span class="inline-added">right</span>', html)
        self.assertIn("unchanged_middle_section", html)

    def test_inline_diff_keeps_deletions_when_three_distant_changes_remain(self):
        html = diff2png._inline_diff_html(
            "old_head = unchanged_section_one + old_middle + unchanged_section_two + old_tail",
            "new_head = unchanged_section_one + new_middle + unchanged_section_two + new_tail",
        )

        self.assertIn('<span class="inline-deleted">old_head</span>', html)
        self.assertIn('<span class="inline-deleted">old_middle</span>', html)
        self.assertIn('<span class="inline-deleted">old_tail</span>', html)
        self.assertIn('<span class="inline-added">new_head</span>', html)
        self.assertIn('<span class="inline-added">new_middle</span>', html)
        self.assertIn('<span class="inline-added">new_tail</span>', html)

    def test_inline_diff_keeps_single_character_change(self):
        html = diff2png._inline_diff_html("value = a", "value = b")

        self.assertIn('<span class="inline-deleted">a</span>', html)
        self.assertIn('<span class="inline-added">b</span>', html)

    def test_inline_diff_can_mute_added_highlight(self):
        html = diff2png._inline_diff_html(
            "value = old",
            "value = new",
            muted_added_keys={"10:0:new"},
            added_key_prefix="10",
        )

        self.assertIn('<span class="inline-added inline-added-muted">new</span>', html)

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
        self.assertIn(
            '<span class="inline-deleted">td&gt;value&lt;/td</span>'
            '<span class="inline-added">div&gt;value&lt;/div</span>',
            html,
        )
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

    def test_deleted_patch_mode_hides_added_rows(self):
        hunk = {
            "filepath": "sample.py",
            "start": 1,
            "end": 2,
            "default_start": 1,
            "default_end": 2,
            "old_start": 1,
            "changed_lines": [2],
            "diff_lines": [
                " context = True",
                '-status = "draft"',
                '+status = "published"',
            ],
            "added_count": 1,
            "deleted_count": 1,
            "changed_count": 2,
        }

        html = diff2png.build_code_html(
            hunk,
            ".",
            1,
            1,
            "2026-06-11 00:00:00",
            {"type": "worktree"},
            {"diff_mode": "deleted", "html_width": 960, "background_mode": "normal"},
        )

        self.assertIn('class="deleted"', html)
        self.assertIn('context = True', html)
        self.assertIn('status = &quot;draft&quot;', html)
        self.assertNotIn('class="added"', html)
        self.assertNotIn('class="inline-added"', html)
        self.assertNotIn('published', html)
        self.assertIn(
            '<td class="lineno old"></td><td class="lineno new"></td>'
            '<td class="marker">-</td>',
            html,
        )
        self.assertIn('| 削除', html)

    def test_patch_modes_shrink_context_and_reset(self):
        hunk = {
            "filepath": "sample.py",
            "start": 1,
            "end": 5,
            "default_start": 1,
            "default_end": 5,
            "old_start": 1,
            "changed_lines": [3],
            "diff_lines": [
                " context top",
                " context near top",
                '-status = "draft"',
                '+status = "published"',
                " context near bottom",
                " context bottom",
            ],
            "added_count": 1,
            "deleted_count": 1,
            "changed_count": 2,
        }

        with patch.object(diff2png, "read_source_lines", return_value=["line"] * 5):
            diff2png.adjust_hunk_range(
                hunk, ".", {"type": "worktree"}, "shrink_up"
            )
            diff2png.adjust_hunk_range(
                hunk, ".", {"type": "worktree"}, "shrink_down"
            )

        self.assertEqual((hunk["start"], hunk["end"]), (2, 4))
        for mode in ("patch", "deleted"):
            html = diff2png.build_code_html(
                hunk,
                ".",
                1,
                1,
                "2026-06-11 00:00:00",
                {"type": "worktree"},
                {"diff_mode": mode, "html_width": 960, "background_mode": "normal"},
            )
            self.assertNotIn("context top", html)
            self.assertNotIn("context bottom", html)
            self.assertIn("context near top", html)
            self.assertIn("context near bottom", html)
            self.assertIn("draft", html)
            if mode == "patch":
                self.assertIn("published", html)
            else:
                self.assertNotIn("published", html)

        with patch.object(diff2png, "read_source_lines", return_value=["line"] * 5):
            diff2png.adjust_hunk_range(hunk, ".", {"type": "worktree"}, "reset")
        self.assertEqual((hunk["start"], hunk["end"]), (1, 5))

    def test_patch_modes_hide_changes_outside_adjusted_range(self):
        hunk = {
            "filepath": "sample.py",
            "start": 1,
            "end": 5,
            "default_start": 1,
            "default_end": 5,
            "old_start": 1,
            "changed_lines": [3],
            "diff_lines": [
                " context top",
                " context near top",
                '-status = "draft"',
                '+status = "published"',
                " context near bottom",
                " context bottom",
            ],
            "added_count": 1,
            "deleted_count": 1,
            "changed_count": 2,
        }

        with patch.object(diff2png, "read_source_lines", return_value=["line"] * 5):
            diff2png.adjust_hunk_range(
                hunk,
                ".",
                {"type": "worktree"},
                "shrink_up",
                step=3,
            )

        self.assertEqual((hunk["start"], hunk["end"]), (4, 5))
        for mode in ("patch", "deleted"):
            html = diff2png.build_code_html(
                hunk,
                ".",
                1,
                1,
                "2026-07-30 00:00:00",
                {"type": "worktree"},
                {"diff_mode": mode, "html_width": 960, "background_mode": "normal"},
            )
            self.assertIn("context near bottom", html)
            self.assertIn("context bottom", html)
            self.assertNotIn("draft", html)
            self.assertNotIn("published", html)

    def test_patch_mode_range_endpoint_allows_shrink_and_reset_but_rejects_expand(self):
        if shutil.which("git") is None:
            self.skipTest("git is not available")

        hunk = {
            "filepath": "sample.py",
            "start": 1,
            "end": 5,
            "default_start": 1,
            "default_end": 5,
            "old_start": 1,
            "changed_lines": [3],
            "diff_lines": [" top", " near", "-old", "+new", " near bottom", " bottom"],
            "added_count": 1,
            "deleted_count": 1,
        }

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            (repo / "sample.py").write_text("\n".join(["top", "near", "new", "near bottom", "bottom"]), encoding="utf-8")
            try:
                diff2png.ANALYSIS_SESSIONS.clear()
                analysis_id = diff2png.create_analysis_session(
                    str(repo),
                    [hunk],
                    [hunk],
                    {"type": "worktree"},
                    {"diff_mode": "patch", "html_width": 960, "background_mode": "normal"},
                )
                client = diff2png.app.test_client()
                payload = {"repo_path": str(repo), "analysis_id": analysis_id}
                shrink = client.post("/api/hunk-range/0", json={**payload, "action": "shrink_up"})
                expand = client.post("/api/hunk-range/0", json={**payload, "action": "expand_up"})
                reset = client.post("/api/hunk-range/0", json={**payload, "action": "reset"})
            finally:
                diff2png.ANALYSIS_SESSIONS.clear()

        self.assertEqual(shrink.status_code, 200)
        self.assertEqual(shrink.get_json()["hunk"]["start"], 2)
        self.assertEqual(expand.status_code, 400)
        self.assertIn("拡大できません", expand.get_json()["error"])
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset.get_json()["hunk"]["start"], 1)

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

    def test_hunk_inline_added_mute_endpoint_updates_target_hunk(self):
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
                    "/api/hunk-inline-added-mute/0",
                    json={
                        "repo_path": str(repo),
                        "analysis_id": analysis_id,
                        "key": "1:0:new",
                        "muted": True,
                    },
                )
            finally:
                diff2png.ANALYSIS_SESSIONS.clear()

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()
        self.assertEqual(data["hunk"]["inline_added_mutes"], ["1:0:new"])
        self.assertEqual(hunks[0]["inline_added_mutes"], ["1:0:new"])

    def test_normal_view_renders_muted_added_highlight(self):
        hunk = {
            "filepath": "sample.py",
            "start": 1,
            "end": 1,
            "default_start": 1,
            "default_end": 1,
            "old_start": 1,
            "changed_lines": [1],
            "diff_lines": ['-value = "old"', '+value = "new"'],
            "added_count": 1,
            "deleted_count": 1,
            "changed_count": 2,
            "inline_diff_mode": "full",
            "inline_diff_enabled": True,
            "inline_added_mutes": ["1:0:new"],
        }
        source_lines = ['value = "new"']

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

        self.assertIn('<span class="inline-added inline-added-muted">new</span>', html)

    def test_normal_view_renders_manual_row_highlights(self):
        hunk = {
            "filepath": "sample.py",
            "start": 1,
            "end": 2,
            "default_start": 1,
            "default_end": 2,
            "old_start": 1,
            "changed_lines": [1],
            "diff_lines": ['+value = "new"'],
            "added_count": 1,
            "deleted_count": 0,
            "changed_count": 1,
            "inline_diff_mode": "full",
            "inline_diff_enabled": True,
            "manual_row_highlights": {"1": "yellow", "2": "green"},
        }
        source_lines = ['value = "new"', "unchanged"]

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

        self.assertIn('<tr class="added manual-row-yellow"><td class="lineno">1</td>', html)
        self.assertIn('<tr class="manual-row-green"><td class="lineno">2</td>', html)

    def test_hunk_row_highlight_endpoint_sets_and_clears_target_hunk(self):
        if shutil.which("git") is None:
            self.skipTest("git is not available")

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            hunks = [
                {
                    "filepath": "sample.py",
                    "start": 1,
                    "end": 2,
                    "default_start": 1,
                    "default_end": 2,
                    "old_start": 1,
                    "changed_lines": [1],
                    "diff_lines": ['+value = "new"'],
                    "added_count": 1,
                    "deleted_count": 0,
                    "changed_count": 1,
                    "inline_diff_mode": "full",
                    "inline_diff_enabled": True,
                    "manual_row_highlights": {},
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
                set_response = client.post(
                    "/api/hunk-row-highlight/0",
                    json={
                        "repo_path": str(repo),
                        "analysis_id": analysis_id,
                        "lineno": 2,
                        "color": "green",
                    },
                )
                clear_response = client.post(
                    "/api/hunk-row-highlight/0",
                    json={
                        "repo_path": str(repo),
                        "analysis_id": analysis_id,
                        "lineno": 2,
                        "color": "none",
                    },
                )
            finally:
                diff2png.ANALYSIS_SESSIONS.clear()

        self.assertEqual(set_response.status_code, 200, set_response.get_data(as_text=True))
        self.assertEqual(set_response.get_json()["hunk"]["manual_row_highlights"], {"2": "green"})
        self.assertEqual(clear_response.status_code, 200, clear_response.get_data(as_text=True))
        self.assertEqual(clear_response.get_json()["hunk"]["manual_row_highlights"], {})
        self.assertEqual(hunks[0]["manual_row_highlights"], {})

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

    def test_normal_view_new_added_line_uses_added_row_color(self):
        hunk = {
            "filepath": "sample.js",
            "start": 1,
            "end": 1,
            "default_start": 1,
            "default_end": 1,
            "old_start": 1,
            "changed_lines": [1],
            "diff_lines": ['+const value = "new"'],
            "added_count": 1,
            "deleted_count": 0,
            "changed_count": 1,
        }

        with patch.object(diff2png, "read_source_lines", return_value=['const value = "new"']):
            html = diff2png.build_code_html(
                hunk,
                ".",
                1,
                1,
                "2026-06-11 00:00:00",
                {"type": "worktree"},
                {"diff_mode": "file", "html_width": 960, "background_mode": "normal"},
            )

        self.assertIn('class="added"', html)
        self.assertNotIn('class="changed"', html)

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

    def test_normal_view_does_not_move_old_condition_diff_into_nested_return(self):
        hunk = {
            "filepath": "sample.php",
            "start": 1,
            "end": 3,
            "default_start": 1,
            "default_end": 3,
            "old_start": 1,
            "changed_lines": [1, 2, 3],
            "diff_lines": [
                "-if (!$legacyCondition) return;",
                "+if ($newCondition) {",
                "+    return;",
                "+}",
            ],
            "added_count": 3,
            "deleted_count": 1,
            "changed_count": 4,
        }
        source_lines = [
            "if ($newCondition) {",
            "    return;",
            "}",
        ]

        replacements = diff2png._line_replacements_by_new_lineno(hunk)

        self.assertIn(1, replacements)
        self.assertNotIn(2, replacements)
        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            html = diff2png.build_code_html(
                hunk,
                ".",
                1,
                1,
                "2026-07-23 00:00:00",
                {"type": "worktree"},
                {"diff_mode": "file", "html_width": 960, "background_mode": "normal"},
            )

        return_position = html.rindex("return;")
        return_row_start = html.rfind("<tr", 0, return_position)
        return_row_end = html.index("</tr>", return_position)
        return_row = html[return_row_start:return_row_end]
        self.assertNotIn("inline-deleted", return_row)
        self.assertNotIn("legacyCondition", return_row)

        condition_position = html.index("newCondition")
        condition_row_start = html.rfind("<tr", 0, condition_position)
        condition_row_end = html.index("</tr>", condition_position)
        condition_row = html[condition_row_start:condition_row_end]
        self.assertIn("inline-deleted", condition_row)
        self.assertIn("legacyCondition", condition_row)
        self.assertIn(
            'if (<span class="inline-deleted">!$legacyCondition</span>'
            '<span class="inline-added">$newCondition</span>) ',
            condition_row,
        )
        self.assertIn('<span class="inline-deleted">return;</span>', condition_row)
        self.assertIn('<span class="inline-added">{</span>', condition_row)

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
        self.assertIn(
            '<span class="inline-deleted">td&gt;value&lt;/td</span>'
            '<span class="inline-added">div&gt;value&lt;/div</span>',
            html,
        )
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
