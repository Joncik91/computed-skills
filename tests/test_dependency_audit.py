from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from helpers import TempDirCase, load

da = load("dependency-audit")


def msgs(reports) -> str:
    return "\n".join(f"{i.severity}:{i.msg}" for r in reports for i in r.issues)


class NpmTests(TempDirCase):
    def pkg(self, deps=None, dev=None) -> None:
        self.write("package.json", json.dumps({"dependencies": deps or {}, "devDependencies": dev or {}}))

    def lock_v3(self, deps=None, dev=None) -> None:
        self.write("package-lock.json", json.dumps({
            "lockfileVersion": 3,
            "packages": {"": {"dependencies": deps or {}, "devDependencies": dev or {}}},
        }))

    def test_yarn_lock_counts_as_lock_file(self) -> None:
        # Regression: package.json matched "npm" first, so yarn.lock was never seen.
        self.pkg({"lodash": "^4.17.21", "@babel/core": "^7.0.0"})
        self.write("yarn.lock", '# yarn lockfile v1\n\nlodash@^4.17.21:\n  version "4.17.21"\n\n'
                                '"@babel/core@^7.0.0", "@babel/core@^7.1.0":\n  version "7.1.0"\n')
        (report,) = da.audit(self.dir)
        self.assertEqual(report.ecosystem, "yarn")
        self.assertEqual(report.issues, [])

    def test_yarn_berry_format(self) -> None:
        names = da.yarn_lock_names('__metadata:\n  version: 6\n\n"lodash@npm:^4.17.21":\n  version: 4\n'
                                   '"@scope/pkg@npm:^1.0.0, @scope/pkg@npm:^1.2.0":\n  version: 1\n')
        self.assertEqual(names, {"lodash", "@scope/pkg"})

    def test_yarn_lock_missing_dep(self) -> None:
        self.pkg({"lodash": "^4", "react": "^18"})
        self.write("yarn.lock", "lodash@^4:\n  version \"4.0.0\"\n")
        self.assertIn("missing from the lock (react)", msgs(da.audit(self.dir)))

    def test_npm_lock_in_sync_ignores_mtime(self) -> None:
        # Regression: an mtime comparison flagged every fresh clone as stale.
        self.pkg({"a": "^1.0.0"}, {"b": "~2.0.0"})
        self.lock_v3({"a": "^1.0.0"}, {"b": "~2.0.0"})
        past = time.time() - 3600
        os.utime(self.dir / "package-lock.json", (past, past))
        self.assertEqual(msgs(da.audit(self.dir)), "")

    def test_npm_lock_detects_changed_and_missing(self) -> None:
        self.pkg({"a": "^2.0.0", "c": "1.0.0"})
        self.lock_v3({"a": "^1.0.0"})
        out = msgs(da.audit(self.dir))
        self.assertIn("1 version range(s) changed (a)", out)
        self.assertIn("1 dependency(ies) not in the lock (c)", out)

    def test_npm_lock_v1(self) -> None:
        self.pkg({"a": "1", "b": "1"})
        self.write("package-lock.json", json.dumps({"lockfileVersion": 1, "dependencies": {"a": {}}}))
        self.assertIn("missing from the lock (b)", msgs(da.audit(self.dir)))

    def test_pnpm_heuristic(self) -> None:
        self.pkg({"react": "^18", "vue": "^3"})
        self.write("pnpm-lock.yaml", "importers:\n  .:\n    dependencies:\n      react:\n        specifier: ^18\n")
        out = msgs(da.audit(self.dir))
        self.assertIn("(vue)", out)
        self.assertNotIn("react", out)

    def test_wildcards_and_missing_lock(self) -> None:
        self.pkg({"a": "*", "b": "latest", "c": "^1"})
        out = msgs(da.audit(self.dir))
        self.assertIn("high:2 unpinned/wildcard dep(s): a, b", out)
        self.assertIn("medium:No lock file found", out)

    def test_multiple_lock_files(self) -> None:
        self.pkg({})
        self.lock_v3()
        self.write("yarn.lock", "")
        self.assertIn("Multiple package managers", msgs(da.audit(self.dir)))

    def test_malformed_package_json_is_high_not_crash(self) -> None:
        self.write("package.json", "{ not json")
        self.write("requirements.txt", "requests==2.0\n")
        reports = da.audit(self.dir)
        self.assertIn("high:Could not audit package.json: package.json is not valid JSON", msgs(reports))
        self.assertEqual([r.ecosystem for r in reports], ["package.json", "pip"])

    def test_wrong_shapes(self) -> None:
        self.write("package.json", '{"dependencies": null}')
        self.assertEqual(da.audit(self.dir)[0].deps, [])
        self.write("package.json", '{"dependencies": ["a"]}')
        self.assertIn("must be an object", msgs(da.audit(self.dir)))
        self.write("package.json", "[]")
        self.assertIn("must contain a JSON object", msgs(da.audit(self.dir)))


class PipTests(TempDirCase):
    def test_parse_requirements(self) -> None:
        self.write("requirements.txt", "\n".join([
            "# comment",
            "-r base.txt",
            "--index-url https://example.com",
            "requests==2.31.0  # pinned",
            "Django>=4,<5",
            "numpy < 2",
            "uvicorn[standard]==0.30",
            "attrs",
            "tomli; python_version < '3.11'",
            "mylib @ https://example.com/mylib.whl",
            "flask \\",
            "    ==3.0",
            "???",
        ]))
        deps, unparsed = da.parse_requirements(self.dir / "requirements.txt")
        specs = {d.name: d.spec for d in deps}
        self.assertEqual(specs, {
            "requests": "==2.31.0", "Django": ">=4,<5", "numpy": "<2", "uvicorn": "==0.30",
            "attrs": "", "tomli": "", "mylib": "url/path", "flask": "==3.0",
        })
        self.assertEqual(unparsed, ["???"])

    def test_issues(self) -> None:
        self.write("requirements.txt", "a\nb>=1\nc==1\n")
        out = msgs(da.audit(self.dir))
        self.assertIn("high:1 unpinned dep(s): a", out)
        self.assertIn("low:1 dep(s) use version ranges", out)


CARGO = """
[package]
name = "demo"
version = "0.1.0"

[dependencies]
serde = "1.0"
tokio = { version = "1", features = ["full"] }
anything = "*"
local = { path = "../local" }
renamed = { package = "real-name", version = "2" }

[dependencies.reqwest]
version = "0.12"
default-features = false

[dev-dependencies]
proptest = "1"

[target.'cfg(unix)'.dependencies]
nix = "0.29"

[workspace.dependencies]
shared = "3"
"""


class CargoTests(TempDirCase):
    EXPECTED = {
        ("serde", "1.0", "prod", "serde"), ("tokio", "1", "prod", "tokio"),
        ("anything", "*", "prod", "anything"), ("local", "git/path", "prod", "local"),
        ("renamed", "2", "prod", "real-name"), ("reqwest", "0.12", "prod", "reqwest"),
        ("proptest", "1", "dev", "proptest"), ("nix", "0.29", "prod", "nix"),
        ("shared", "3", "prod", "shared"),
    }

    def test_parser_and_fallback_agree(self) -> None:
        self.write("Cargo.toml", CARGO)
        self.assertEqual({tuple(d) for d in da.parse_cargo_toml(self.dir / "Cargo.toml")}, self.EXPECTED)
        self.assertEqual({tuple(d) for d in da._parse_cargo_fallback(CARGO)}, self.EXPECTED)

    def test_lock_check_uses_package_rename_and_skips_path_deps(self) -> None:
        self.write("Cargo.toml", CARGO)
        names = ["serde", "tokio", "anything", "real-name", "reqwest", "proptest", "nix"]
        self.write("Cargo.lock", "".join(f'[[package]]\nname = "{n}"\nversion = "1"\n\n' for n in names))
        out = msgs(da.audit(self.dir))
        self.assertIn("(shared)", out)  # only `shared` is genuinely missing
        self.assertNotIn("local", out)
        self.assertIn("1 unpinned/wildcard dep(s): anything", out)

    def test_invalid_toml(self) -> None:
        self.write("Cargo.toml", "[dependencies\nserde = ")
        if da._toml is not None:
            self.assertIn("not valid TOML", msgs(da.audit(self.dir)))


class GoTests(TempDirCase):
    GOMOD = ("module example.com/m\n\ngo 1.22\n\nrequire github.com/a/b v1.2.3\n\n"
             "require (\n\tgithub.com/c/d v0.1.0\n\tgolang.org/x/sys v0.20.0 // indirect\n)\n\n"
             "replace github.com/a/b => ../b\n")

    def test_parse(self) -> None:
        self.write("go.mod", self.GOMOD)
        deps = da.parse_go_mod(self.dir / "go.mod")
        self.assertEqual([(d.name, d.group) for d in deps], [
            ("github.com/a/b", "prod"), ("github.com/c/d", "prod"), ("golang.org/x/sys", "indirect")])

    def test_missing_go_sum(self) -> None:
        self.write("go.mod", self.GOMOD)
        self.assertIn("high:go.mod has requirements but go.sum is missing", msgs(da.audit(self.dir)))

    def test_go_sum_gap(self) -> None:
        self.write("go.mod", self.GOMOD)
        self.write("go.sum", "github.com/a/b v1.2.3 h1:x=\ngithub.com/a/b v1.2.3/go.mod h1:y=\n"
                             "golang.org/x/sys v0.20.0 h1:z=\n")
        self.assertIn("missing from the lock (github.com/c/d)", msgs(da.audit(self.dir)))


class ModeTests(TempDirCase):
    def healthy(self) -> None:
        self.write("requirements.txt", "requests==2.31.0\n")

    def broken(self) -> None:
        self.write("requirements.txt", "requests\n")

    def test_heartbeat_silent_when_healthy(self) -> None:
        self.healthy()
        self.assertEqual(da.render(self.dir, ["heartbeat"]), "")

    def test_heartbeat_speaks_on_high(self) -> None:
        self.broken()
        self.assertIn("[dependency-audit] 1 issue(s) found", da.render(self.dir, ["heartbeat"]))

    def test_default_is_never_empty(self) -> None:
        self.healthy()
        self.assertIn("No urgent issues", da.render(self.dir, []))
        self.broken()
        self.assertIn("Dependency alert", da.render(self.dir, []))

    def test_no_manifest(self) -> None:
        self.assertEqual(da.render(self.dir, ["heartbeat"]), "")
        self.assertIn("no supported manifest", da.render(self.dir, []))
        self.assertIn("No supported dependency manifest", da.render(self.dir, ["status"]))

    def test_status_covers_all_ecosystems(self) -> None:
        self.healthy()
        self.write("go.mod", "module m\n")
        out = da.render(self.dir, ["STATUS"])
        self.assertIn("## pip (requirements.txt)", out)
        self.assertIn("## go (go.mod)", out)

    def test_unknown_mode(self) -> None:
        self.healthy()
        self.assertIn("unknown mode `stauts`", da.render(self.dir, ["stauts"]))

    def test_heartbeat_crash_is_not_silent(self) -> None:
        with mock.patch.object(da, "render", side_effect=RuntimeError("boom")), \
                mock.patch("builtins.print") as printed:
            self.assertEqual(da.main(["heartbeat"]), 0)
        self.assertIn("generator error: RuntimeError: boom", printed.call_args[0][0])

    def test_invalid_project_dir_is_reported_even_in_heartbeat(self) -> None:
        gone = str(self.dir / "missing")
        for mode, expect in (("heartbeat", "project directory not found"), ("", "project directory not found")):
            with mock.patch("builtins.print") as printed:
                self.assertEqual(da.main(["--project-dir", gone, *([mode] if mode else [])]), 0)
            self.assertIn(expect, printed.call_args[0][0])

    def test_large_project(self) -> None:
        self.write("requirements.txt", "".join(f"p{i}==1\n" for i in range(201)))
        self.assertIn("low:201 total dependencies", msgs(da.audit(self.dir)))


if __name__ == "__main__":
    unittest.main()
