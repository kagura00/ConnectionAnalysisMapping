"""Read-only build context loaders shared by language analyzers.

These helpers make build metadata visible to static analysis without treating
the metadata as permission to execute a build.  Paths are resolved for lookup
only, and malformed or unavailable optional context is reported to callers as
diagnostic-friendly state rather than aborting an otherwise useful analysis.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .config import AnalysisConfig

MAX_CONTEXT_ARCHIVE_SOURCE_BYTES = 8 * 1024 * 1024
MAX_CONTEXT_ARCHIVE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_CONTEXT_ARCHIVE_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CompilationCommand:
    file: Path
    directory: Path
    arguments: tuple[str, ...]
    include_dirs: tuple[Path, ...] = ()
    defines: tuple[str, ...] = ()
    standard: str | None = None


@dataclass(slots=True)
class CompilationDatabase:
    path: Path | None = None
    commands: dict[str, CompilationCommand] = field(default_factory=dict)
    error: str | None = None

    def for_file(self, path: Path, root: Path) -> CompilationCommand | None:
        key = path.resolve().as_posix().lower()
        command = self.commands.get(key)
        if command is not None:
            return command
        relative = path.resolve().relative_to(root.resolve()).as_posix().lower()
        return self.commands.get(relative)

    def include_dirs_for(self, path: Path, root: Path) -> tuple[Path, ...]:
        command = self.for_file(path, root)
        return command.include_dirs if command is not None else ()


@dataclass(slots=True)
class TypeIndex:
    entries: set[str] = field(default_factory=set)
    methods: set[str] = field(default_factory=set)
    sources: dict[str, str] = field(default_factory=dict)
    archives: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def has_type(self, name: str) -> bool:
        candidate = name.lstrip(".")
        return candidate in self.entries or candidate.rsplit(".", 1)[-1] in self.entries

    def has_method(self, owner: str, method: str) -> bool:
        short_owner = owner.rsplit(".", 1)[-1]
        suffix = f".{short_owner}.{method}"
        return any(candidate in {f"{owner}.{method}", f"{short_owner}.{method}"} or candidate.endswith(suffix) for candidate in self.methods)


@dataclass(frozen=True, slots=True)
class GoBuildProfile:
    tags: tuple[str, ...] = ()
    goos: str | None = None
    goarch: str | None = None


@dataclass(frozen=True, slots=True)
class RustBuildProfile:
    features: tuple[str, ...] = ()
    target: str | None = None
    all_cfg: bool = True


_GOOS_TAGS = {"aix", "android", "darwin", "dragonfly", "freebsd", "hurd", "illumos", "ios", "js", "linux", "netbsd", "openbsd", "plan9", "solaris", "wasip1", "windows"}
_GOARCH_TAGS = {"386", "amd64", "arm", "arm64", "loong64", "mips", "mips64", "mips64le", "mipsle", "ppc64", "ppc64le", "riscv64", "s390x", "wasm"}
_UNIX_GOOS = _GOOS_TAGS - {"android", "ios", "js", "wasip1", "windows"}


def _go_tag_values(profile: GoBuildProfile) -> set[str]:
    values = set(profile.tags)
    if profile.goos:
        values.add(profile.goos)
        if profile.goos in _UNIX_GOOS:
            values.add("unix")
    if profile.goarch:
        values.add(profile.goarch)
    return values


def _go_expr_matches(expression: str, tags: set[str]) -> bool | None:
    tokens = re.findall(r"&&|\|\||!|\(|\)|[A-Za-z0-9_./-]+", expression)
    if not tokens:
        return None
    position = 0

    def parse_or() -> bool:
        nonlocal position
        value = parse_and()
        while position < len(tokens) and tokens[position] == "||":
            position += 1
            # Always parse the right-hand side.  Short-circuiting here would
            # leave tokens unconsumed and turn a valid expression into an
            # unknown result.
            right = parse_and()
            value = value or right
        return value

    def parse_and() -> bool:
        nonlocal position
        value = parse_unary()
        while position < len(tokens) and tokens[position] == "&&":
            position += 1
            right = parse_unary()
            value = value and right
        return value

    def parse_unary() -> bool:
        nonlocal position
        if position >= len(tokens):
            raise ValueError("missing Go build tag expression")
        if tokens[position] == "!":
            position += 1
            return not parse_unary()
        if tokens[position] == "(":
            position += 1
            value = parse_or()
            if position >= len(tokens) or tokens[position] != ")":
                raise ValueError("unclosed Go build tag expression")
            position += 1
            return value
        token = tokens[position]
        position += 1
        return token in tags

    try:
        result = parse_or()
        if position != len(tokens):
            return None
        return result
    except ValueError:
        return None


def go_file_matches_build(relative_path: str, source: bytes, profile: GoBuildProfile) -> bool:
    """Apply Go filename and build-tag selection without invoking the Go toolchain."""

    configured = bool(profile.tags or profile.goos or profile.goarch)
    if not configured:
        return True
    tags = _go_tag_values(profile)
    stem_parts = Path(relative_path).stem.split("_")
    if profile.goos and any(part in _GOOS_TAGS and part != profile.goos for part in stem_parts):
        return False
    if profile.goarch and any(part in _GOARCH_TAGS and part != profile.goarch for part in stem_parts):
        return False
    text = source.decode("utf-8", errors="replace")
    modern = [line.strip()[len("//go:build") :].strip() for line in text.splitlines() if line.strip().startswith("//go:build")]
    if modern:
        return all(_go_expr_matches(expression, tags) is not False for expression in modern)
    legacy = [line.strip()[len("// +build") :].strip() for line in text.splitlines() if line.strip().startswith("// +build")]
    for expression in legacy:
        alternatives = expression.split()
        if not any(all(tag.lstrip("!") in tags if not tag.startswith("!") else tag[1:] not in tags for tag in alternative.split(",")) for alternative in alternatives):
            return False
    return True


def _rust_target_values(profile: RustBuildProfile) -> set[str]:
    values = set(profile.features)
    values.update(f"feature={feature}" for feature in profile.features)
    parts = profile.target.split("-") if profile.target else []
    if parts and parts[0]:
        values.add(f"target_arch={parts[0]}")
    for operating_system in ("windows", "linux", "darwin", "macos", "freebsd", "openbsd", "netbsd", "android", "ios"):
        if operating_system in parts:
            values.add(operating_system)
            values.add(f"target_os={operating_system}")
            if operating_system not in {"windows", "android", "ios"}:
                values.add("unix")
                values.add("target_family=unix")
            else:
                values.add(f"target_family={operating_system}")
            break
    if "windows" in values:
        values.add("target_os=windows")
    return values


_RUST_TARGET_KEYS = {
    "target_arch",
    "target_endian",
    "target_env",
    "target_family",
    "target_feature",
    "target_os",
    "target_pointer_width",
    "target_vendor",
    "unix",
    "windows",
}


def _rust_cfg_atom(name: str, value: str | None, profile: RustBuildProfile, values: set[str]) -> bool | None:
    if value is not None:
        if name == "feature":
            return value in profile.features
        if name.startswith("target_") or name in _RUST_TARGET_KEYS:
            return None if profile.target is None else f"{name}={value}" in values
        return None
    if name in {"true", "all_cfg"}:
        return True
    if name == "false":
        return False
    if name in _RUST_TARGET_KEYS:
        return None if profile.target is None else name in values
    if name in profile.features:
        return True
    # Unknown cfg keys (for example `test` or a target feature) are kept in
    # the graph when the profile cannot prove them false.  This avoids
    # silently dropping definitions merely because the build metadata is
    # incomplete.
    return None


def rust_cfg_expression_matches(expression: str, profile: RustBuildProfile) -> bool:
    """Evaluate the common Rust cfg forms used to select definitions."""

    if profile.all_cfg:
        return True
    tokens = re.findall(r"\s*(?:[(),=]|\"[^\"]*\"|[A-Za-z_][\w.-]*)", expression)
    if not tokens:
        return True
    tokens = [token.strip() for token in tokens]
    position = 0
    values = _rust_target_values(profile)

    def parse_value() -> bool | None:
        nonlocal position
        if position >= len(tokens):
            return None
        name = tokens[position]
        position += 1
        if name in {"all", "any", "not"} and position < len(tokens) and tokens[position] == "(":
            position += 1
            children: list[bool | None] = []
            while position < len(tokens) and tokens[position] != ")":
                child = parse_value()
                children.append(child)
                if position < len(tokens) and tokens[position] == ",":
                    position += 1
                elif position < len(tokens) and tokens[position] != ")":
                    return None
            if position >= len(tokens) or tokens[position] != ")":
                return None
            position += 1
            if name == "all":
                if any(child is False for child in children):
                    return False
                return None if any(child is None for child in children) else True
            if name == "any":
                if any(child is True for child in children):
                    return True
                return None if any(child is None for child in children) else False
            if len(children) != 1:
                return None
            return None if children[0] is None else not children[0]
        if position < len(tokens) and tokens[position] == "=":
            position += 1
            if position >= len(tokens):
                return None
            value = tokens[position].strip('"')
            position += 1
            return _rust_cfg_atom(name, value, profile, values)
        return _rust_cfg_atom(name, None, profile, values)

    result = parse_value()
    if result is None or position != len(tokens):
        return True
    return result


@dataclass(slots=True)
class AnalysisContext:
    compilation_database: CompilationDatabase = field(default_factory=CompilationDatabase)
    type_index: TypeIndex = field(default_factory=TypeIndex)
    go: GoBuildProfile = field(default_factory=GoBuildProfile)
    rust: RustBuildProfile = field(default_factory=RustBuildProfile)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "compile_commands": str(self.compilation_database.path) if self.compilation_database.path else None,
            "compile_command_files": len(self.compilation_database.commands),
            "classpath_types": len(self.type_index.entries),
            "classpath_methods": len(self.type_index.methods),
            "classpath_archives": list(self.type_index.archives),
            "references": list(self.type_index.references),
            "go": {"tags": list(self.go.tags), "goos": self.go.goos, "goarch": self.go.goarch},
            "rust": {"features": list(self.rust.features), "target": self.rust.target, "all_cfg": self.rust.all_cfg},
            "diagnostics": list(self.diagnostics),
        }


def _context_path(root: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()


def _command_arguments(entry: dict[str, Any]) -> tuple[str, ...]:
    arguments = entry.get("arguments")
    if isinstance(arguments, list) and all(isinstance(item, str) for item in arguments):
        return tuple(arguments)
    command = entry.get("command")
    if isinstance(command, str):
        try:
            if os.name == "nt":
                return _split_windows_command(command)
            return tuple(shlex.split(command, posix=True))
        except ValueError:
            return ()
    return ()


def _split_windows_command(command: str) -> tuple[str, ...]:
    """Split a Windows command line while preserving quoted paths.

    ``shlex``'s POSIX and non-POSIX modes both differ from the CRT parsing
    rules used by compile_commands producers.  This small parser handles the
    quoting cases needed for compiler flags, including ``/I"C:\\Program
    Files\\..."`` and escaped quotes, without executing the command.
    """

    arguments: list[str] = []
    current: list[str] = []
    in_quotes = False
    started = False
    index = 0
    while index < len(command):
        character = command[index]
        if character in " \t\r\n" and not in_quotes:
            if started:
                arguments.append("".join(current))
                current = []
                started = False
            index += 1
            continue
        if character == "\\":
            slash_start = index
            while index < len(command) and command[index] == "\\":
                index += 1
            slash_count = index - slash_start
            if index < len(command) and command[index] == '"':
                current.extend("\\" * (slash_count // 2))
                started = True
                if slash_count % 2:
                    current.append('"')
                    index += 1
                elif in_quotes and index + 1 < len(command) and command[index + 1] == '"':
                    current.append('"')
                    index += 2
                else:
                    in_quotes = not in_quotes
                    index += 1
            else:
                current.extend("\\" * slash_count)
                started = True
            continue
        if character == '"':
            started = True
            if in_quotes and index + 1 < len(command) and command[index + 1] == '"':
                current.append('"')
                index += 2
            else:
                in_quotes = not in_quotes
                index += 1
            continue
        current.append(character)
        started = True
        index += 1
    if started:
        arguments.append("".join(current))
    return tuple(arguments)


def _flag_values(arguments: tuple[str, ...], *flags: str) -> list[str]:
    values: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in flags and index + 1 < len(arguments):
            values.append(arguments[index + 1].strip('"'))
            index += 2
            continue
        for flag in flags:
            if argument.startswith(flag) and argument != flag:
                values.append(argument[len(flag) :].strip('"'))
                break
        index += 1
    return values


def load_compilation_database(root: Path, config: AnalysisConfig) -> CompilationDatabase:
    configured = config.context.get("compile_commands")
    candidate = _context_path(root, configured) if isinstance(configured, str) else root / "compile_commands.json"
    if not candidate.is_file():
        if configured:
            return CompilationDatabase(path=candidate, error=f"compile_commands.json not found: {candidate}")
        return CompilationDatabase()
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return CompilationDatabase(path=candidate, error=f"compile_commands.json could not be read: {exc}")
    if not isinstance(payload, list):
        return CompilationDatabase(path=candidate, error="compile_commands.json must contain an array")
    commands: dict[str, CompilationCommand] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        arguments = _command_arguments(entry)
        file_value = entry.get("file")
        if not isinstance(file_value, str) or not arguments:
            continue
        directory = _context_path(root, entry.get("directory", ".")) if isinstance(entry.get("directory", "."), str) else root
        file_path = _context_path(directory, file_value)
        include_dirs = tuple(
            _context_path(directory, value) if Path(value).is_absolute() else (directory / value).resolve()
            for value in _flag_values(arguments, "-I", "/I", "-isystem")
        )
        defines = tuple(_flag_values(arguments, "-D", "/D"))
        standard_values = _flag_values(arguments, "-std=", "/std:")
        command = CompilationCommand(
            file=file_path,
            directory=directory,
            arguments=arguments,
            include_dirs=include_dirs,
            defines=defines,
            standard=standard_values[0] if standard_values else None,
        )
        commands[file_path.as_posix().lower()] = command
        try:
            relative = file_path.relative_to(root.resolve()).as_posix().lower()
            commands[relative] = command
        except ValueError:
            pass
    return CompilationDatabase(path=candidate, commands=commands)


_PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z_][\w.]*)", re.MULTILINE)
_TYPE_RE = re.compile(r"\b(?:class|interface|enum|object|record|struct|trait)\s+([A-Za-z_]\w*)")
_METHOD_RE = re.compile(r"\b(?:fun|def|void|public|private|protected|internal|static|async|[A-Za-z_]\w*[<>,\[\]. ]*)\s+([A-Za-z_]\w*)\s*\(")


def _index_source_file(
    path: Path,
    index: TypeIndex,
    package_hint: str | None = None,
    *,
    max_file_bytes: int,
) -> None:
    try:
        if path.stat().st_size > max_file_bytes:
            index.errors.append(f"classpath source skipped as too large: {path}")
            return
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        index.errors.append(f"classpath source unreadable: {path}: {exc}")
        return
    _index_source_text(text, str(path), index, package_hint=package_hint)


def _index_source_text(text: str, display_path: str, index: TypeIndex, package_hint: str | None = None) -> None:
    package_match = _PACKAGE_RE.search(text)
    package = package_match.group(1) if package_match else package_hint
    for match in _TYPE_RE.finditer(text):
        name = match.group(1)
        qualified = f"{package}.{name}" if package else name
        index.entries.add(qualified)
        index.entries.add(name)
        index.sources[qualified] = display_path
        for method in _METHOD_RE.finditer(text):
            index.methods.add(f"{qualified}.{method.group(1)}")


def _index_archive(
    path: Path,
    index: TypeIndex,
    *,
    source_limit: int,
    total_limit: int = MAX_CONTEXT_ARCHIVE_TOTAL_BYTES,
    source_suffixes: set[str] | None = None,
    include_class: bool = True,
) -> None:
    source_bytes = 0
    try:
        if path.stat().st_size > MAX_CONTEXT_ARCHIVE_BYTES:
            index.errors.append(f"classpath archive skipped as too large: {path}")
            return
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                name = info.filename
                parts = PurePosixPath(name).parts
                if (
                    not name
                    or name.startswith(("/", "\\"))
                    or "\\" in name
                    or any(part in {"", ".", ".."} for part in parts)
                ):
                    index.errors.append(f"classpath archive member skipped as unsafe: {path}!{name}")
                    continue
                if include_class and name.endswith(".class") and not name.endswith("module-info.class"):
                    qualified = name[:-6].replace("/", ".")
                    index.entries.add(qualified)
                    index.entries.add(qualified.rsplit(".", 1)[-1])
                elif source_suffixes and Path(name).suffix.lower() in source_suffixes:
                    if info.file_size > source_limit:
                        index.errors.append(f"classpath archive source skipped as too large: {path}!{name}")
                        continue
                    if source_bytes + info.file_size > total_limit:
                        index.errors.append(f"classpath archive source limit reached: {path}")
                        break
                    package_hint = ".".join(Path(name).with_suffix("").parts[:-1]) or None
                    content = archive.read(info).decode("utf-8")
                    source_bytes += info.file_size
                    _index_source_text(content, f"{path}!{name}", index, package_hint=package_hint)
    except UnicodeError as exc:
        index.errors.append(f"classpath archive source is not UTF-8: {path}: {exc}")
    except KeyError as exc:
        index.errors.append(f"classpath archive member is unavailable: {path}: {exc}")
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        index.errors.append(f"classpath archive unreadable: {path}: {exc}")


def _classpath_source_suffixes(language: str) -> set[str]:
    if language == "csharp":
        return {".cs"}
    if language in {"java", "kotlin"}:
        return {".java", ".kt", ".kts"}
    return {".java", ".kt", ".kts", ".cs"}


def load_type_index(root: Path, config: AnalysisConfig, *, language: str | None = None) -> TypeIndex:
    index = TypeIndex()
    active_language = language or config.language
    source_suffixes = _classpath_source_suffixes(active_language)
    include_class = active_language in {"java", "kotlin"}
    entries = [*config.context.get("classpath", []), *config.context.get("source_roots", [])]
    for raw in entries:
        candidate = _context_path(root, raw)
        if not candidate.exists():
            index.errors.append(f"classpath entry not found: {candidate}")
            continue
        if candidate.is_file() and candidate.suffix.lower() in {".jar", ".zip"}:
            index.archives.append(str(candidate))
            _index_archive(
                candidate,
                index,
                source_limit=min(config.max_file_bytes, MAX_CONTEXT_ARCHIVE_SOURCE_BYTES),
                source_suffixes=source_suffixes,
                include_class=include_class,
            )
            continue
        if candidate.is_file() and candidate.suffix.lower() in source_suffixes:
            _index_source_file(candidate, index, max_file_bytes=config.max_file_bytes)
            continue
        if candidate.is_dir():
            for source in sorted(candidate.rglob("*")):
                if source.is_symlink() or not source.is_file():
                    continue
                if source.suffix.lower() in source_suffixes:
                    _index_source_file(source, index, max_file_bytes=config.max_file_bytes)
                elif include_class and source.suffix.lower() == ".class":
                    relative = source.relative_to(candidate).with_suffix("").as_posix().replace("/", ".")
                    index.entries.update({relative, relative.rsplit(".", 1)[-1]})
    index.references.extend(str(_context_path(root, value)) for value in config.context.get("references", []))
    return index


def load_analysis_context(root: Path, config: AnalysisConfig) -> AnalysisContext:
    context = AnalysisContext(
        compilation_database=load_compilation_database(root, config),
        type_index=load_type_index(root, config, language=config.language),
        go=GoBuildProfile(
            tags=tuple(config.context.get("go_tags", [])),
            goos=config.context.get("go_os"),
            goarch=config.context.get("go_arch"),
        ),
        rust=RustBuildProfile(
            features=tuple(config.context.get("rust_features", [])),
            target=config.context.get("rust_target"),
            all_cfg=config.context.get("rust_all_cfg", True),
        ),
    )
    if context.compilation_database.error:
        context.diagnostics.append({"severity": "warning", "code": "compile_context_unavailable", "message": context.compilation_database.error})
    for error in context.type_index.errors:
        context.diagnostics.append({"severity": "warning", "code": "classpath_context_unavailable", "message": error})
    return context


def build_context_extensions(context: AnalysisContext) -> dict[str, Any]:
    """Return a compact per-node-safe context summary for analyzer metadata."""

    return {
        "compile_commands": context.compilation_database.path is not None,
        "classpath": bool(context.type_index.entries or context.type_index.archives),
        "go_build_profile": {"tags": list(context.go.tags), "goos": context.go.goos, "goarch": context.go.goarch},
        "rust_build_profile": {"features": list(context.rust.features), "target": context.rust.target, "all_cfg": context.rust.all_cfg},
    }
