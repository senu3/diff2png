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

    def test_adjust_hunk_range_shifts_expands_and_resets(self):
        hunk = {
            **_raw_hunk(10, 14),
            "default_start": 10,
            "default_end": 14,
        }
        source_lines = [f"line {i}" for i in range(1, 31)]

        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            shifted = diff2png.adjust_hunk_range(hunk, ".", {"type": "worktree"}, "shift_down")
            expanded = diff2png.adjust_hunk_range(hunk, ".", {"type": "worktree"}, "expand")
            reset = diff2png.adjust_hunk_range(hunk, ".", {"type": "worktree"}, "reset")

        self.assertEqual((shifted["start"], shifted["end"]), (11, 15))
        self.assertTrue(shifted["range_adjusted"])
        self.assertEqual((expanded["start"], expanded["end"]), (10, 16))
        self.assertEqual((reset["start"], reset["end"]), (10, 14))
        self.assertFalse(reset["range_adjusted"])

    def test_adjust_hunk_range_clamps_to_file_bounds(self):
        hunk = {
            **_raw_hunk(28, 30),
            "default_start": 28,
            "default_end": 30,
        }
        source_lines = [f"line {i}" for i in range(1, 31)]

        with patch.object(diff2png, "read_source_lines", return_value=source_lines):
            shifted_down = diff2png.adjust_hunk_range(hunk, ".", {"type": "worktree"}, "shift_down")
            expanded = diff2png.adjust_hunk_range(hunk, ".", {"type": "worktree"}, "expand")

        self.assertEqual((shifted_down["start"], shifted_down["end"]), (28, 30))
        self.assertEqual((expanded["start"], expanded["end"]), (27, 30))

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
