from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import GitRepoCase, load

sr = load("smart-review")


class ClassifyTests(unittest.TestCase):
    def tags(self, path: str) -> set:
        return sr.classify_file(path)

    def test_whole_word_matching_avoids_substring_false_positives(self) -> None:
        for path in ("src/oracle.py", "lib/tokenizer.py", "docs/author.md", "app/escapement.c"):
            self.assertNotIn("security", self.tags(path), path)
        for path in ("src/latest.py", "inspector.py", "contest/main.go"):
            self.assertNotIn("test", self.tags(path), path)

    def test_security_paths(self) -> None:
        for path in ("src/auth/views.py", "OAuthHandler.java", "jwt_utils.ts",
                     "certs/server.pem", "config/.env", ".env.production"):
            self.assertIn("security", self.tags(path), path)

    def test_env_files_are_config(self) -> None:
        # os.path.splitext(".env") has no extension; must still count as config.
        self.assertIn("config", self.tags(".env"))
        self.assertIn("config", self.tags("deploy/.env.staging"))

    def test_test_paths(self) -> None:
        for path in ("tests/foo.py", "test_foo.py", "foo_test.go", "Foo.spec.ts",
                     "src/__tests__/a.js", "FooTest.java", "conftest.py"):
            self.assertIn("test", self.tags(path), path)

    def test_strategy_thresholds(self) -> None:
        self.assertEqual(sr.detect_strategy(["auth.py", "a.py", "b.py"])[0], "security")
        self.assertEqual(sr.detect_strategy(["a.yml", "b.json", "c.py"])[0], "config-audit")
        self.assertEqual(sr.detect_strategy(["tests/a.py", "tests/b.py", "c.py"])[0], "test-quality")
        self.assertEqual(sr.detect_strategy([f"f{i}.py" for i in range(20)])[0], "architecture")
        self.assertEqual(sr.detect_strategy(["a/x.py", "b/x.py", "c/x.py", "d/x.py"])[0], "architecture")
        self.assertEqual(sr.detect_strategy(["a.py"])[0], "correctness")


class ArgsTests(unittest.TestCase):
    def test_parse_args_never_exits(self) -> None:
        self.assertEqual(sr.parse_args(["--project-dir"]), sr.Args(None, ["--project-dir"]))
        self.assertEqual(sr.parse_args(["--project-dir", "", "x"]), sr.Args(None, ["x"]))
        self.assertEqual(sr.parse_args(["--project-dir=/p", "security"]), sr.Args("/p", ["security"]))


class RenderTests(GitRepoCase):
    def test_untracked_files_are_reviewed(self) -> None:
        self.write("README.md")
        self.commit()
        self.write("src/auth/new_login.py")  # never git-added
        out = sr.render(self.dir, [])
        self.assertIn("Security Strategy", out)
        self.assertIn("src/auth/new_login.py", out)
        self.assertIn("1 untracked file(s) are not in that diff", out)

    def test_non_ascii_and_spaced_filenames_survive(self) -> None:
        self.write("README.md")
        self.commit()
        self.write("café dir/naïve file.py")
        out = sr.render(self.dir, [])
        self.assertIn("café dir/naïve file.py", out)

    def test_clean_feature_branch_reviews_commits_against_base(self) -> None:
        self.write("README.md")
        self.commit()
        self.git("checkout", "-q", "-b", "feature")
        self.write("src/thing.py")
        self.commit()
        out = sr.render(self.dir, [])
        self.assertIn("git diff main...HEAD", out)
        self.assertIn("src/thing.py", out)

    def test_nothing_to_review(self) -> None:
        self.write("README.md")
        self.commit()
        out = sr.render(self.dir, [])
        self.assertIn("nothing to review", out)
        self.assertIn("No pending changes. Ready for new work.", out)

    def test_unborn_branch_with_staged_file(self) -> None:
        self.write("a.py")
        self.git("add", "a.py")
        out = sr.render(self.dir, [])
        self.assertIn("a.py", out)
        self.assertIn("git diff --cached", out)

    def test_forced_and_unknown_strategy(self) -> None:
        self.write("a.py")
        self.assertIn("Security Strategy", sr.render(self.dir, ["SECURITY"]))
        out = sr.render(self.dir, ["bogus"])
        self.assertIn("unknown strategy `bogus` ignored", out)
        self.assertIn("Correctness Strategy", out)

    def test_large_diff_stat_is_truncated(self) -> None:
        for i in range(120):
            self.write(f"pkg/f{i}.py")
        self.commit()
        for i in range(120):
            self.write(f"pkg/f{i}.py", "changed\n")
        out = sr.render(self.dir, [])
        self.assertIn("more lines", out)
        self.assertIn("and 95 more", out)
        self.assertIn("120 files changed", out)

    def test_not_a_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as plain:
            out = sr.render(Path(plain), [])
        self.assertIn("not inside a git repository", out)

    def test_project_dir_beats_cwd(self) -> None:
        self.write("src/auth.py")
        with tempfile.TemporaryDirectory() as elsewhere:
            proc = self.run_script("smart-review", "--project-dir", str(self.dir), cwd=Path(elsewhere))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("src/auth.py", proc.stdout)

    def test_invalid_project_dir_does_not_fall_back_to_cwd(self) -> None:
        self.write("src/auth.py")
        proc = self.run_script("smart-review", "--project-dir", str(self.dir / "missing"))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("project directory not found", proc.stdout)
        self.assertNotIn("auth.py", proc.stdout)

    def test_crash_prints_fallback_and_exits_zero(self) -> None:
        with mock.patch.object(sr, "render", side_effect=RuntimeError("boom")), \
                mock.patch("builtins.print") as printed:
            self.assertEqual(sr.main([]), 0)
        text = printed.call_args[0][0]
        self.assertIn("fallback instructions", text)
        self.assertIn("RuntimeError: boom", text)


if __name__ == "__main__":
    unittest.main()
