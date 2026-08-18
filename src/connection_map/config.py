"""Configuration and source-file selection for repository analysis."""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .language_registry import (
    concrete_languages,
    default_include_patterns,
    get_language_spec,
    language_for_path,
    supported_languages,
)

DEFAULT_EXCLUDE = [
    ".git/**",
    ".connection-map/**",
    "**/__pycache__/**",
    "**/.venv/**",
    "**/venv/**",
    "**/env/**",
    "**/build/**",
    "**/dist/**",
    "**/node_modules/**",
    "**/.next/**",
    "**/.cache/**",
    "**/*.g.cs",
    "**/*.designer.cs",
    "**/*.gen.go",
    "**/*.generated.rs",
    "**/*_generated.rs",
    "**/target/**",
    "**/vendor/**",
    "**/.build/**",
    "**/DerivedData/**",
    "**/Pods/**",
    "**/Carthage/**",
    "**/SourcePackages/**",
    "**/coverage/**",
    "**/*.generated.php",
    "**/*_generated.php",
    "**/*.generated.rb",
    "**/*_generated.rb",
    "**/*.generated.kt",
    "**/*_generated.kt",
    "**/*.generated.kts",
    "**/*_generated.kts",
    "**/*.generated.swift",
    "**/*_generated.swift",
    "**/*+Generated.swift",
    "**/*Generated.swift",
    "**/*.generated.scala",
    "**/*_generated.scala",
    "**/*.generated.vb",
    "**/*_generated.vb",
    "**/*.designer.vb",
    "**/*.generated.bas",
    "**/*_generated.bas",
    "**/*.generated.lua",
    "**/*_generated.lua",
    "**/*.generated.hs",
    "**/*_generated.hs",
    "**/*.generated.pl",
    "**/*_generated.pl",
    "**/*.generated.m",
    "**/*_generated.m",
    "**/*.generated.f90",
    "**/*_generated.f90",
    "**/*.generated.r",
    "**/*_generated.r",
    "**/*.generated.mm",
    "**/*_generated.mm",
    "**/*.generated.cu",
    "**/*_generated.cu",
    "**/*.generated.groovy",
    "**/*_generated.groovy",
    "**/*.generated.fs",
    "**/*_generated.fs",
    "**/*.generated.asm",
    "**/*_generated.asm",
    "**/*.generated.tf",
    "**/*_generated.tf",
    "**/*.generated.gd",
    "**/*_generated.gd",
    "**/*.generated.ex",
    "**/*_generated.ex",
    "**/*.generated.zig",
    "**/*_generated.zig",
    "**/*.generated.jl",
    "**/*_generated.jl",
    "**/*.generated.pas",
    "**/*_generated.pas",
    "**/*.generated.erl",
    "**/*_generated.erl",
]

DEFAULT_GENERATED = [
    "**/Generated/**",
    "**/generated/**",
    "**/__generated__/**",
]

DART_DEFAULT_EXCLUDE = [
    "**/*.generated.dart",
    "**/*_generated.dart",
    "**/*.g.dart",
    "**/*.freezed.dart",
    "**/*.mocks.dart",
    "**/*bindings.dart",
    "**/generated/**",
    "**/gen/**",
    "**/jni/**",
]

SWIFT_DEFAULT_EXCLUDE = [
    # Package.swift is executable Swift manifest code, but it describes the
    # package build rather than the application graph by default.
    "**/Package.swift",
]

DEFAULT_TEST_PATTERNS = [
    "tests/**",
    "test/**",
    "**/test_*.py",
    "**/*_test.py",
    "**/test_*.c",
    "**/test_*.h",
    "**/test_*.cc",
    "**/test_*.cpp",
    "**/test_*.hpp",
    "**/test_*.cxx",
    "**/*_test.c",
    "**/*_test.h",
    "**/*_test.cc",
    "**/*_test.cpp",
    "**/*_test.hpp",
    "**/*_test.cxx",
    "**/*Test.java",
    "**/Test*.java",
    "**/*Tests.java",
    "**/*Test.cs",
    "**/Test*.cs",
    "**/*Tests.cs",
    "**/*_test.go",
    "**/*_test.rs",
    "**/*Test.php",
    "**/*TestCase.php",
    "**/*_test.php",
    "**/spec/**/*.rb",
    "**/spec/*.rb",
    "**/test/**/*.rb",
    "**/test/*.rb",
    "**/tests/**/*.rb",
    "**/tests/*.rb",
    "**/test/**/*.rake",
    "**/test/*.rake",
    "**/tests/**/*.rake",
    "**/tests/*.rake",
    "**/test/**/*.gemspec",
    "**/test/*.gemspec",
    "**/tests/**/*.gemspec",
    "**/tests/*.gemspec",
    "**/test/**/*.ru",
    "**/test/*.ru",
    "**/tests/**/*.ru",
    "**/tests/*.ru",
    "**/*_spec.rb",
    "**/*_test.rb",
    "**/test_*.rb",
    "**/src/test/**/*.kt",
    "**/src/test/*.kt",
    "**/src/test/**/*.kts",
    "**/src/test/*.kts",
    "**/test/**/*.kt",
    "**/test/*.kt",
    "**/tests/**/*.kt",
    "**/tests/*.kt",
    "**/test/**/*.kts",
    "**/test/*.kts",
    "**/tests/**/*.kts",
    "**/tests/*.kts",
    "**/*Test.kt",
    "**/*Tests.kt",
    "**/*Test.kts",
    "**/*Tests.kts",
    "**/*Test.vb",
    "**/*Tests.vb",
    "**/Test*.vb",
    "**/*Test.bas",
    "**/*Tests.bas",
    "**/test/**/*.lua",
    "**/tests/**/*.lua",
    "**/*_test.lua",
    "**/test/**/*.hs",
    "**/tests/**/*.hs",
    "**/*_test.hs",
    "**/t/**/*.pl",
    "**/t/*.pl",
    "**/test/**/*.pl",
    "**/tests/**/*.pl",
    "**/*_test.pl",
    "**/test/**/*.m",
    "**/tests/**/*.m",
    "**/*_test.m",
    "**/test/**/*.f90",
    "**/tests/**/*.f90",
    "**/test/**/*.r",
    "**/tests/**/*.r",
    "**/test/**/*.mm",
    "**/tests/**/*.mm",
    "**/test/**/*.cu",
    "**/tests/**/*.cu",
    "**/test/**/*.groovy",
    "**/tests/**/*.groovy",
    "**/*Test.groovy",
    "**/test/**/*.fs",
    "**/tests/**/*.fs",
    "**/test/**/*.zig",
    "**/tests/**/*.zig",
    "**/test/**/*.jl",
    "**/tests/**/*.jl",
    "**/test/**/*.pas",
    "**/tests/**/*.pas",
    "**/test/**/*.erl",
    "**/tests/**/*.erl",
    "**/Tests/**/*.swift",
    "**/tests/**/*.swift",
    "**/test/**/*.swift",
    "**/*Test.swift",
    "**/*Tests.swift",
    "**/*TestSupport.swift",
    "**/*_test.sh",
    "**/test_*.sh",
    "**/*.bats",
    "**/test/**/*.ps1",
    "**/tests/**/*.ps1",
    "**/*Test.dart",
    "**/*_test.dart",
    "**/test/**/*.dart",
    "**/test/**/*.scala",
    "**/src/test/**/*.scala",
    "**/*Test.scala",
    "**/*Spec.scala",
    "**/__tests__/**",
    "**/__test__/**",
    "**/*.test.*",
    "**/*.spec.*",
]


@dataclass(slots=True)
class AnalysisConfig:
    language: str = "python"
    languages: list[str] = field(default_factory=list)
    include_tests: bool = False
    follow_symlinks: bool = False
    max_file_bytes: int = 2_000_000
    include: list[str] | None = None
    exclude: list[str] | None = None
    test_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_TEST_PATTERNS))
    generated: list[str] = field(default_factory=lambda: list(DEFAULT_GENERATED))
    # Build metadata is read-only input for analyzers.  It is intentionally
    # kept separate from repository execution: no compiler, build tool, JVM,
    # Go command, or Cargo command is started by the analyzer.
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.include is None:
            self.include = default_include_patterns(self.language, self.languages)
        if self.exclude is None:
            self.exclude = list(DEFAULT_EXCLUDE)
        # These profiles have language-specific generated files. Apply them
        # even when a repository supplies its own common exclude list.
        concrete = concrete_languages(self.language, self.languages)
        for pattern in (
            DART_DEFAULT_EXCLUDE if "dart" in concrete else []
        ) + (SWIFT_DEFAULT_EXCLUDE if "swift" in concrete else []):
            if pattern not in self.exclude:
                self.exclude.append(pattern)

    @classmethod
    def from_toml(cls, path: Path | None) -> AnalysisConfig:
        if path is None:
            return cls()
        if not path.exists():
            raise FileNotFoundError(path)
        try:
            import tomllib
        except ModuleNotFoundError as exc:  # pragma: no cover - Python requirement is >= 3.11
            raise RuntimeError("Python 3.11 or newer is required to read TOML") from exc
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        values = raw.get("analysis", raw)
        if not isinstance(values, dict):
            raise TypeError("[analysis] must be a TOML table")
        allowed_values = {
            "language",
            "languages",
            "include_tests",
            "follow_symlinks",
            "max_file_bytes",
            "include",
            "exclude",
            "test_patterns",
            "generated",
            "context",
        }
        unknown = sorted(set(values) - allowed_values)
        if unknown:
            raise ValueError(f"analysis contains unsupported keys: {', '.join(unknown)}")
        config_values: dict[str, Any] = {}
        for name in (
            "language",
            "languages",
            "include_tests",
            "follow_symlinks",
            "max_file_bytes",
            "include",
            "exclude",
            "test_patterns",
            "generated",
        ):
            if name in values:
                config_values[name] = values[name]
        if "context" in values:
            config_values["context"] = values["context"]
        config = cls(**config_values)
        config.validate()
        return config

    def validate(self) -> None:
        if self.language not in supported_languages():
            choices = ", ".join(supported_languages())
            raise ValueError(f"language must be one of: {choices}")
        if not isinstance(self.languages, list) or not all(
            isinstance(item, str) and item for item in self.languages
        ):
            raise ValueError("languages must be a list of non-empty strings")
        concrete = concrete_languages(self.language, self.languages)
        if self.language == "python" and concrete != ("python",):
            raise ValueError("language = 'python' can only analyze python")
        if not get_language_spec(self.language).is_preset and self.languages and concrete != (self.language,):
            raise ValueError(f"languages must be empty or ['{self.language}'] for a single-language analysis")
        if not isinstance(self.include_tests, bool):
            raise TypeError("include_tests must be boolean")
        if not isinstance(self.follow_symlinks, bool):
            raise TypeError("follow_symlinks must be boolean")
        if isinstance(self.max_file_bytes, bool) or not isinstance(self.max_file_bytes, int) or self.max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be a positive integer")
        for name in ("include", "exclude", "test_patterns", "generated"):
            values = getattr(self, name)
            if not isinstance(values, list) or not all(isinstance(item, str) and item for item in values):
                raise ValueError(f"{name} must be a list of non-empty strings")
        if not isinstance(self.context, dict):
            raise ValueError("context must be a TOML table")
        allowed_context = {
            "compile_commands",
            "classpath",
            "source_roots",
            "references",
            "go_tags",
            "go_os",
            "go_arch",
            "rust_features",
            "rust_target",
            "rust_all_cfg",
        }
        unknown = sorted(set(self.context) - allowed_context)
        if unknown:
            raise ValueError(f"context contains unsupported keys: {', '.join(unknown)}")
        for name in ("classpath", "source_roots", "references", "go_tags", "rust_features"):
            value = self.context.get(name, [])
            if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
                raise ValueError(f"context.{name} must be a list of non-empty strings")
        for name in ("compile_commands", "go_os", "go_arch", "rust_target"):
            value = self.context.get(name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"context.{name} must be a non-empty string or null")
        if "rust_all_cfg" in self.context and not isinstance(self.context["rust_all_cfg"], bool):
            raise ValueError("context.rust_all_cfg must be boolean")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_toml(self) -> str:
        """Serialize the effective configuration for central-mode replay."""

        def value_text(value: Any) -> str:
            return json.dumps(value, ensure_ascii=False)

        lines = [
            "[analysis]",
            f"language = {value_text(self.language)}",
            f"languages = {value_text(self.languages)}",
            f"include_tests = {value_text(self.include_tests)}",
            f"follow_symlinks = {value_text(self.follow_symlinks)}",
            f"max_file_bytes = {self.max_file_bytes}",
            f"include = {value_text(self.include)}",
            f"exclude = {value_text(self.exclude)}",
            f"test_patterns = {value_text(self.test_patterns)}",
            f"generated = {value_text(self.generated)}",
        ]
        if self.context:
            lines.append("")
            lines.append("[analysis.context]")
            for key, value in sorted(self.context.items()):
                if value is not None:
                    lines.append(f"{key} = {value_text(value)}")
        return "\n".join(lines) + "\n"

    def active_languages(self) -> tuple[str, ...]:
        """Return concrete source languages selected by this configuration."""

        self.validate()
        return concrete_languages(self.language, self.languages)

    def matches(self, relative_path: str, patterns: list[str]) -> bool:
        path = _normalize_match_path(relative_path)
        for pattern in patterns:
            normalized = _normalize_match_path(pattern)
            if fnmatch.fnmatchcase(path, normalized) or fnmatch.fnmatchcase(path.casefold(), normalized.casefold()):
                return True
            # Python's fnmatch does not give ``**/`` a path-aware meaning;
            # retry without that prefix so root-level files match too.
            if normalized.startswith("**/") and (
                fnmatch.fnmatchcase(path, normalized[3:])
                or fnmatch.fnmatchcase(path.casefold(), normalized[3:].casefold())
            ):
                return True
        return False

    def should_include(self, relative_path: str) -> tuple[bool, str | None]:
        path = relative_path.replace(os.sep, "/")
        if not self.matches(path, self.include):
            return False, "not_included"
        if self.matches(path, self.exclude):
            return False, "excluded"
        if not self.include_tests and self.matches(path, self.test_patterns):
            return False, "tests_excluded"
        if self.matches(path, self.generated):
            return False, "generated"
        return True, None


def repository_id(root: Path) -> str:
    """Return a portable identifier without embedding the absolute root path."""

    resolved = root.resolve()
    return resolved.name or "repository"


def ensure_repository_root(root: Path) -> Path:
    """Resolve and validate the repository root before starting an analysis."""

    resolved = Path(root).resolve()
    if not resolved.is_dir():
        raise ValueError(f"analysis root is not an existing directory: {resolved}")
    return resolved


def _normalize_match_path(value: str) -> str:
    """Normalize separators without erasing meaningful leading dots."""

    normalized = value.replace(os.sep, "/").replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _path_is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def discover_source_files(
    root: Path,
    config: AnalysisConfig,
    *,
    languages: set[str] | None = None,
) -> tuple[list[Path], list[tuple[str, str]]]:
    """Discover selected source files and return files plus (path, reason)."""

    config.validate()
    root = root.resolve()
    selected_languages = languages or set(config.active_languages())
    selected: list[Path] = []
    skipped: list[tuple[str, str]] = []
    visited_directories: set[Path] = set()
    for current, dirs, files in os.walk(root, followlinks=config.follow_symlinks):
        current_path = Path(current)
        resolved_current = current_path.resolve()
        if resolved_current in visited_directories:
            dirs[:] = []
            continue
        visited_directories.add(resolved_current)
        kept_dirs: list[str] = []
        for directory in dirs:
            candidate = current_path / directory
            if candidate.is_symlink():
                if not config.follow_symlinks:
                    continue
                try:
                    resolved_candidate = candidate.resolve(strict=True)
                except OSError:
                    continue
                if not _path_is_within(root, resolved_candidate) or resolved_candidate in visited_directories:
                    continue
            relative = candidate.relative_to(root).as_posix() + "/"
            if config.matches(relative, config.exclude):
                continue
            kept_dirs.append(directory)
        # Pruning here avoids both filesystem work and accidental traversal of
        # excluded generated/dependency trees.
        dirs[:] = kept_dirs
        for name in files:
            candidate = current_path / name
            if candidate.is_symlink():
                if not config.follow_symlinks:
                    skipped.append((candidate.relative_to(root).as_posix(), "symlink_excluded"))
                    continue
                try:
                    resolved_candidate = candidate.resolve(strict=True)
                except OSError:
                    skipped.append((candidate.relative_to(root).as_posix(), "symlink_unresolved"))
                    continue
                if not _path_is_within(root, resolved_candidate):
                    skipped.append((candidate.relative_to(root).as_posix(), "symlink_outside_root"))
                    continue
            source_language = language_for_path(candidate, selected_languages)
            if source_language not in selected_languages:
                continue
            relative = candidate.relative_to(root).as_posix()
            include, reason = config.should_include(relative)
            if not include:
                skipped.append((relative, reason or "excluded"))
                continue
            try:
                if candidate.stat().st_size > config.max_file_bytes:
                    skipped.append((relative, "file_too_large"))
                    continue
            except OSError:
                skipped.append((relative, "stat_failed"))
                continue
            selected.append(candidate)
    selected.sort(key=lambda item: item.relative_to(root).as_posix())
    skipped.sort()
    return selected, skipped


def discover_python_files(root: Path, config: AnalysisConfig) -> tuple[list[Path], list[tuple[str, str]]]:
    """Backward-compatible Python-only discovery wrapper."""

    return discover_source_files(root, config, languages={"python"})
