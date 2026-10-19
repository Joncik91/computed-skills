from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import GitRepoCase, load

dc = load("deploy-checklist")


def keys(assessment) -> list:
    return [r.rule.key for r in assessment.risks]


class AssessTests(unittest.TestCase):
    def test_migration_alone_is_high_and_rendered(self) -> None:
        # Regression: scored 3 -> MEDIUM, and the MEDIUM branch had no
        # migration section, so the migration was silently hidden.
        a = dc.assess(["db/migrations/0042_drop_users.py"])
        self.assertEqual(a.level, "HIGH")
        out = dc.render_checklist(a, "scope", [])
        self.assertIn("Database Migration", out)
        self.assertIn("0042_drop_users.py", out)

    def test_auth_alone_is_high(self) -> None:
        self.assertEqual(dc.assess(["src/auth/session.py"]).level, "HIGH")

    def test_every_risk_is_rendered_at_high(self) -> None:
        # Regression: HIGH used to drop config, CI/CD and dependency sections.
        a = dc.assess(["src/auth.py", "package.json", "settings.yaml", ".github/workflows/ci.yml"])
        self.assertEqual(a.level, "HIGH")
        out = dc.render_checklist(a, "scope", [])
        for title in ("Authentication", "CI/CD", "Dependency", "Configuration"):
            self.assertIn(title, out)

    def test_ci_workflow_is_not_double_counted_as_config(self) -> None:
        a = dc.assess([".github/workflows/ci.yml"])
        self.assertEqual(keys(a), ["CICD_CHANGE"])
        self.assertEqual(a.level, "MEDIUM")

    def test_pyproject_is_dependency_not_config(self) -> None:
        self.assertEqual(keys(dc.assess(["pyproject.toml"])), ["DEPENDENCY_CHANGE"])

    def test_env_file_is_secrets(self) -> None:
        self.assertEqual(keys(dc.assess([".env.production"])), ["AUTH_SECURITY"])

    def test_test_files_do_not_raise_risk(self) -> None:
        a = dc.assess(["tests/test_auth.py", "spec/migration_spec.rb"])
        self.assertEqual((a.level, a.risks), ("LOW", []))

    def test_camel_case_names(self) -> None:
        self.assertEqual(keys(dc.assess(["db/AddUsersMigration.rb"])), ["DB_MIGRATION"])
        self.assertEqual(dc.assess(["src/AuthServiceTest.java"]).risks, [])

    def test_no_false_positive_matches(self) -> None:
        a = dc.assess(["src/db/pool.py", "lib/tokenizer.py", "app/oracle.py", "graphql/types.py"])
        self.assertEqual((a.level, a.risks), ("LOW", []))

    def test_dockerfile_variants(self) -> None:
        for path in ("Dockerfile", "docker/Dockerfile.prod", "api.dockerfile", "docker-compose.override.yml"):
            self.assertEqual(keys(dc.assess([path])), ["CICD_CHANGE"], path)

    def test_changeset_size(self) -> None:
        a = dc.assess([f"src/f{i}.py" for i in range(16)])
        self.assertEqual(a.level, "LOW")
        self.assertIn("Large Changeset (16 files)", dc.render_checklist(a, "s", []))
        self.assertEqual(dc.assess([f"src/f{i}.py" for i in range(31)]).level, "MEDIUM")

    def test_file_lists_are_capped(self) -> None:
        a = dc.assess([f"migrations/{i}.sql" for i in range(40)])
        self.assertIn("and 25 more", dc.render_checklist(a, "s", []))


class RenderTests(GitRepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.write("README.md")
        self.commit()

    def test_feature_branch_with_uncommitted_warning(self) -> None:
        self.git("checkout", "-q", "-b", "feature")
        self.write("migrations/001.sql")
        self.commit()
        self.write("scratch.txt")
        out = dc.render(self.dir, [])
        self.assertIn("HIGH RISK", out)
        self.assertIn("against `main`", out)
        self.assertIn("1 uncommitted file(s) are NOT part of this deploy", out)

    def test_uncommitted_only_is_labelled(self) -> None:
        self.write("src/auth.py")
        out = dc.render(self.dir, [])
        self.assertIn("No committed changes", out)
        self.assertIn("commit them before deploying", out)

    def test_clean_base_branch(self) -> None:
        out = dc.render(self.dir, [])
        self.assertIn("LOW RISK", out)
        self.assertIn("No file changes detected", out)

    def test_explicit_base_ref(self) -> None:
        self.git("tag", "v1")
        self.write("config/settings.yaml")
        self.commit()
        out = dc.render(self.dir, ["v1"])
        self.assertIn("against `v1`", out)
        self.assertIn("Configuration Changes", out)

    def test_option_like_ref_is_rejected(self) -> None:
        out = dc.render(self.dir, ["--output=/tmp/pwned"])
        self.assertIn("is not a valid git ref", out)
        self.assertFalse(Path("/tmp/pwned").exists())

    def test_renamed_file_in_porcelain(self) -> None:
        self.git("mv", "README.md", "docs.md")
        self.assertEqual(dc.uncommitted_changes(self.dir), ["docs.md"])

    def test_no_base_branch(self) -> None:
        self.git("branch", "-m", "trunk")
        out = dc.render(self.dir, [])
        self.assertIn("No base branch found", out)

    def test_not_a_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as plain:
            self.assertIn("not inside a git repository", dc.render(Path(plain), []))

    def test_invalid_project_dir_does_not_fall_back_to_cwd(self) -> None:
        self.write("migrations/002.sql")
        proc = self.run_script("deploy-checklist", "--project-dir", str(self.dir / "missing"))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("UNKNOWN RISK", proc.stdout)
        self.assertNotIn("002.sql", proc.stdout)

    def test_crash_prints_fallback_and_exits_zero(self) -> None:
        with mock.patch.object(dc, "render", side_effect=KeyError("x")), \
                mock.patch("builtins.print") as printed:
            self.assertEqual(dc.main([]), 0)
        self.assertIn("UNKNOWN RISK", printed.call_args[0][0])

    def test_script_runs_from_another_cwd(self) -> None:
        self.write("migrations/002.sql")
        with tempfile.TemporaryDirectory() as elsewhere:
            proc = self.run_script("deploy-checklist", "--project-dir", str(self.dir), cwd=Path(elsewhere))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("migrations/002.sql", proc.stdout)


if __name__ == "__main__":
    unittest.main()
