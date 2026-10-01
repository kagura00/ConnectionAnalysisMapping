"""Declared Python import roots, without importing or running the project."""

from __future__ import annotations

import tomllib
from pathlib import Path, PurePosixPath

from .config import AnalysisConfig


class PythonProjects:
    def __init__(self, root: Path, config: AnalysisConfig) -> None:
        self.inputs: set[Path] = set()
        self.limitations: list[str] = []
        declared = config.context.get("python_source_roots")
        if declared is not None:
            self.roots = self._roots(root, declared)
            return
        self.roots = ["."]
        path = root / "pyproject.toml"
        if not path.exists():
            return
        if not path.resolve().is_relative_to(root):
            self.limitations.append("Python pyproject.toml resolves outside the repository; ignored")
            return
        self.inputs.add(path)
        try:
            if path.stat().st_size > config.max_file_bytes:
                raise ValueError("file exceeds metadata size limit")
            data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
            setuptools = data.get("tool", {}).get("setuptools", {})
            packages = setuptools.get("packages", {})
            where = packages.get("find", {}).get("where") if isinstance(packages, dict) else None
            package_dir = setuptools.get("package-dir", {}).get("")
            if where is not None:
                self.roots = self._roots(root, where)
            elif package_dir is not None:
                self.roots = self._roots(root, [package_dir])
        except (OSError, UnicodeError, ValueError, TypeError, AttributeError) as exc:
            self.limitations.append(f"Python import roots from pyproject.toml unavailable: {exc}")

    @staticmethod
    def _roots(root: Path, values: list[str]) -> list[str]:
        if not isinstance(values, list) or not values or not all(isinstance(s, str) and s for s in values):
            raise ValueError("Python source roots must be a non-empty list of repository-relative paths")
        normalized = []
        for value in values:
            path = PurePosixPath(value.replace("\\", "/"))
            if path.is_absolute() or any(p.casefold() in {"..", ".git"} or ":" in p for p in path.parts):
                raise ValueError("Python source roots must stay inside the repository outside .git")
            if not (root / Path(*path.parts)).resolve().is_relative_to(root):
                raise ValueError("Python source root resolves outside the repository")
            normalized.append(path.as_posix())
        return sorted(set(normalized), key=lambda s: (-len(PurePosixPath(s).parts), s))

    def module_path(self, relative: str) -> str:
        path = PurePosixPath(relative)
        for root in self.roots:
            prefix = PurePosixPath(root)
            if path.is_relative_to(prefix):
                return path.relative_to(prefix).as_posix()
        return relative
