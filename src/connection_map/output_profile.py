"""Path resolution and preflight for explicit external output profiles."""

from __future__ import annotations

import contextlib
import os
import stat
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PathSetting:
    """An effective path and the setting that selected it."""

    path: Path | None
    origin: str


def absolute_path(path: Path | str, *, base: Path | None = None) -> Path:
    """Return a lexical absolute path without resolving aliases or symlinks."""

    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = (base or Path.cwd()) / candidate
    return Path(os.path.abspath(os.fspath(candidate)))


def derived_path(external_dir: Path, child: str) -> Path:
    return Path(external_dir) / child


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError(f"cannot inspect external output path: {path}") from exc
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _path_components(path: Path) -> Iterator[Path]:
    current = Path(path.anchor)
    anchor_parts = Path(path.anchor).parts
    for part in path.parts[len(anchor_parts) :]:
        current /= part
        yield current


def _check_path_components(path: Path, *, kind: str) -> None:
    components = list(_path_components(path))
    for index, component in enumerate(components):
        if _is_link_or_reparse(component):
            raise ValueError(f"external {kind} path must not contain a symlink or reparse point: {component}")
        try:
            info = os.lstat(component)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(f"cannot inspect external {kind} path: {component}") from exc
        if index < len(components) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"external {kind} path has a non-directory ancestor: {component}")
    if components:
        try:
            info = os.lstat(components[-1])
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ValueError(f"cannot inspect external {kind} path: {components[-1]}") from exc
        if kind == "directory" and not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"external {kind} path is not a directory: {path}")
        if kind == "file" and stat.S_ISDIR(info.st_mode):
            raise ValueError(f"external {kind} path is a directory: {path}")


def _overlaps(left: Path, right: Path) -> bool:
    """Whether either path contains the other, using host path semantics."""

    left_text = os.path.normcase(os.path.abspath(os.fspath(left)))
    right_text = os.path.normcase(os.path.abspath(os.fspath(right)))
    try:
        common = os.path.commonpath((left_text, right_text))
    except ValueError:
        return False
    return common == left_text or common == right_text



def preflight_profile_layout(
    external_dir: Path,
    managed_stores: list[tuple[str, Path]],
    explicit_output: Path | None,
) -> None:
    """Keep explicit results from overwriting managed registry/cache data.

    The profile directory is a container, but its three standard storage trees
    stay reserved even when a command does not use them. Tool-managed analysis
    and cache files are checked separately and are allowed inside their store.
    """

    stores = [(label, absolute_path(path)) for label, path in managed_stores]
    for index, (label, path) in enumerate(stores):
        for other_label, other_path in stores[index + 1 :]:
            if _overlaps(path, other_path):
                raise ValueError(
                    f"external managed stores must not overlap: {label} ({path}) and {other_label} ({other_path})"
                )
    if explicit_output is None:
        return
    output = absolute_path(explicit_output)
    reserved = [
        ("profile workspace", absolute_path(derived_path(external_dir, "workspace"))),
        ("profile investigation cache", absolute_path(derived_path(external_dir, "investigation-cache"))),
        ("profile grammar cache", absolute_path(derived_path(external_dir, "grammar-cache"))),
    ]
    for label, path in stores + reserved:
        if _overlaps(output, path):
            raise ValueError(f"external output must be outside managed storage ({label}): {output}")


def _scan_storage_tree(root: Path, *, label: str) -> None:
    """Reject symlinks and reparse points anywhere under an existing store."""

    try:
        root_info = os.lstat(root)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError(f"cannot inspect {label} storage: {root}") from exc
    if stat.S_ISLNK(root_info.st_mode) or bool(getattr(root_info, "st_file_attributes", 0) & 0x400):
        raise ValueError(f"{label} storage must not be a symlink or reparse point: {root}")
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f"{label} storage path is not a directory: {root}")

    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                children = list(entries)
        except OSError as exc:
            raise ValueError(f"cannot inspect {label} storage descendants: {directory}") from exc
        for entry in children:
            child = Path(entry.path)
            if _is_link_or_reparse(child):
                raise ValueError(f"{label} storage must not contain a symlink or reparse point: {child}")
            try:
                info = os.lstat(child)
            except OSError as exc:
                raise ValueError(f"cannot inspect {label} storage descendant: {child}") from exc
            if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
                raise ValueError(f"{label} storage must not contain a symlink or reparse point: {child}")
            if stat.S_ISDIR(info.st_mode):
                pending.append(child)


def _check_raw_path_components(path: Path, *, kind: str) -> None:
    """Inspect components in caller order before lexical ``..`` cleanup."""

    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    anchor_parts = Path(candidate.anchor).parts
    current = Path(candidate.anchor)
    parts = candidate.parts[len(anchor_parts) :]
    for index, part in enumerate(parts):
        if part == "..":
            current = current.parent
            continue
        if part == ".":
            continue
        current /= part
        if _is_link_or_reparse(current):
            raise ValueError(f"external {kind} path must not contain a symlink or reparse point: {current}")
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(f"cannot inspect external {kind} path: {current}") from exc
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"external {kind} path has a non-directory ancestor: {current}")


def preflight_destinations(
    target_root: Path,
    destinations: list[tuple[str, Path, str]],
    *,
    storage_roots: list[tuple[str, Path]] | None = None,
) -> None:
    """Validate all external outputs before a command reads or writes them.

    ``target_root`` must already be a resolved existing source directory. The
    component checks intentionally happen before any ``Path.resolve`` call so
    an alias cannot disappear from the evidence. This is an operational guard,
    not protection against hostile concurrent replacement after validation.
    """

    root = absolute_path(target_root)
    for label, raw_path, kind in destinations:
        _check_raw_path_components(raw_path, kind=label)
        path = absolute_path(raw_path)
        _check_path_components(path, kind=kind)
        if _overlaps(root, path):
            raise ValueError(f"external {label} must be outside the target repository: {path}")
    for label, raw_path in storage_roots or []:
        _check_raw_path_components(raw_path, kind=label)
        path = absolute_path(raw_path)
        _check_path_components(path, kind="directory")
        _scan_storage_tree(path, label=label)


def current_grammar_cache() -> PathSetting:
    """Return the optional pack's effective cache path for path reporting."""

    try:
        import tree_sitter_language_pack as pack
    except ModuleNotFoundError as exc:
        if exc.name == "tree_sitter_language_pack":
            return PathSetting(None, "optional parser unavailable")
        return PathSetting(None, "optional parser configuration unavailable")
    getter = getattr(pack, "cache_dir", None)
    if not callable(getter):
        return PathSetting(None, "installed parser has no public cache_dir API")
    try:
        return PathSetting(Path(getter()), "ambient/default")
    except Exception:
        return PathSetting(None, "ambient/default unavailable")


def report_paths(settings: list[tuple[str, PathSetting | None]], *, stream=None) -> None:
    """Print effective path choices without contaminating JSON stdout."""

    destination = stream or sys.stderr
    for label, setting in settings:
        if setting is None:
            continue
        if setting.path is None:
            value = "stdout" if "stdout" in setting.origin else "unavailable"
        else:
            value = str(absolute_path(setting.path))
        print(f"{label}: {value} (from {setting.origin})", file=destination)


@contextlib.contextmanager
def configured_grammar_cache(path: Path | None) -> Iterator[bool]:
    """Temporarily redirect the optional public language-pack cache API.

    If the optional package is absent, Python-only analysis can continue. An
    installed package that cannot honor the public configure contract is an
    error when a redirect was requested. Its previous effective cache path is
    restored so in-process CLI calls do not leak test or caller configuration.
    """

    if path is None:
        yield False
        return
    try:
        import tree_sitter_language_pack as pack
    except ModuleNotFoundError as exc:
        if exc.name == "tree_sitter_language_pack":
            yield False
            return
        raise ValueError(f"installed tree-sitter-language-pack is unusable: {exc}") from exc

    pack_config = getattr(pack, "PackConfig", None)
    configure = getattr(pack, "configure", None)
    cache_dir = getattr(pack, "cache_dir", None)
    if not callable(configure) or not callable(cache_dir) or pack_config is None:
        raise ValueError("installed tree-sitter-language-pack lacks the public PackConfig/configure/cache_dir API")
    try:
        previous_cache = cache_dir()
        configure(pack_config(cache_dir=os.fspath(path)))
    except Exception as exc:
        raise ValueError(f"tree-sitter-language-pack cannot configure grammar cache {path}: {exc}") from exc
    try:
        yield True
    finally:
        try:
            configure(pack_config(cache_dir=previous_cache))
        except Exception as exc:
            raise ValueError(f"tree-sitter-language-pack could not restore its previous grammar cache: {exc}") from exc
