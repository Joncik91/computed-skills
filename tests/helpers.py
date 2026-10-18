"""Shared test helpers: load example generators by path, build temp git repos."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


def load(example: str) -> ModuleType:
    """Import examples/<example>/scripts/generate.py under a unique name."""
    path = EXAMPLES / example / "scripts" / "generate.py"
    name = "gen_" + example.replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def script(example: str) -> Path:
    return EXAMPLES / example / "scripts" / "generate.py"


GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, rel: str, content: str = "x\n") -> Path:
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


class GitRepoCase(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        env = dict(os.environ, **GIT_ENV)
        return subprocess.run(["git", *args], cwd=self.dir, env=env, check=True,
                              capture_output=True, text=True).stdout

    def commit(self, message: str = "c") -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def run_script(self, example: str, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ, **GIT_ENV)
        env.pop("CLAUDE_PROJECT_DIR", None)
        return subprocess.run([sys.executable, str(script(example)), *args],
                              cwd=str(cwd or self.dir), env=env, capture_output=True, text=True)
