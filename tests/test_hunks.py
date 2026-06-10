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
