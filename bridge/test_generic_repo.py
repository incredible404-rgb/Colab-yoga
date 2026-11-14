"""Colab-yoga is the general tool: one person's models, folders and prompts live outside it."""
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Split so this file does not match itself.
PERSONAL = ("nov" + "el", "小" + "说", "Colab" + "创作")


class GenericRepoTests(unittest.TestCase):
    def test_no_tracked_file_names_a_personal_use_case(self) -> None:
        try:
            listed = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True)
        except OSError:
            self.skipTest("git is not available")
        if listed.returncode != 0:
            self.skipTest("not a git checkout")
        hits = []
        for name in listed.stdout.decode("utf-8").split("\0"):
            path = REPO / name
            if not name or not path.is_file() or path.suffix in (".png", ".gif", ".ico", ".icns"):
                continue
            text = path.read_bytes().decode("utf-8", errors="ignore").lower()
            hits += [f"{name}: {word}" for word in PERSONAL if word.lower() in text or word.lower() in name.lower()]
        self.assertEqual(hits, [], "personal models, presets and folders belong in settings or the runners file")


if __name__ == "__main__":
    unittest.main()
