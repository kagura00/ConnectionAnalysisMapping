"""Read-only, repository-bounded TypeScript project metadata."""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .config import AnalysisConfig


def metadata_paths(root: Path, config: AnalysisConfig) -> tuple[list[Path], list[str]]:
    """Inventory context and unsupported styles without entering excluded trees."""
    projects, styles = [], []
    for current, dirs, files in os.walk(root):
        directory = Path(current)
        dirs[:] = [name for name in dirs if not (directory / name).is_symlink()
                   and not config.matches((directory / name).relative_to(root).as_posix() + '/', config.exclude)]
        for name in files:
            path = directory / name
            if path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            if config.matches(relative, config.exclude + config.generated):
                continue
            if name.startswith('tsconfig') and name.endswith('.json'):
                projects.append(path)
            if path.suffix.lower() in {'.scss', '.sass', '.less'}:
                styles.append(relative)
    explicit = config.context.get('tsconfig')
    if explicit:
        path = (root / explicit).resolve()
        if path.is_relative_to(root):
            projects.append(path)
    return sorted(set(projects)), sorted(styles)


def jsonc(text: str) -> dict:
    # Preserve strings (including URLs and escaped quotes) while removing comments.
    tokens = re.compile(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*[\s\S]*?\*/')
    cleaned = tokens.sub(lambda m: m[0] if m[0].startswith('"') else ' ', text)
    cleaned = re.sub(r'("(?:\\.|[^"\\])*")|,(\s*[}\]])', lambda m: m[1] or m[2], cleaned)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError('tsconfig must contain an object')
    return value


@dataclass
class Project:
    path: Path
    base_url: Path | None
    paths_base: Path
    paths: dict[str, list[str]]
    include: list[tuple[Path, str]]
    exclude: list[tuple[Path, str]]
    files: list[Path] | None
    inputs: set[Path]

    def includes(self, source: Path) -> bool:
        if self.files is not None and source in self.files:
            return True
        return any(matches(source, base, pattern) for base, pattern in self.include) and not any(
            matches(source, base, pattern) for base, pattern in self.exclude)


def matches(source: Path, base: Path, pattern: str) -> bool:
    try:
        relative = source.relative_to(base).as_posix()
    except ValueError:
        return False
    pattern = pattern.replace('\\', '/')
    if not Path(pattern).suffix and '*' not in pattern:
        pattern = pattern.rstrip('/') + '/**/*'
    variants = {pattern, pattern.replace('**/', '')}
    return any(fnmatch.fnmatchcase(relative, item) for item in variants)


class TypeScriptProjects:
    def __init__(self, root: Path, config: AnalysisConfig):
        self.root = root.resolve()
        self.projects: dict[Path, Project] = {}
        self.inputs: set[Path] = set()
        self.errors: list[tuple[str, str]] = []
        paths, self.unsupported_styles = metadata_paths(self.root, config)
        self.explicit = (self.root / config.context['tsconfig']).resolve() if config.context.get('tsconfig') else None
        if self.explicit and not self.explicit.is_relative_to(self.root):
            self.errors.append(('tsconfig', 'explicit tsconfig must be inside the repository'))
        # Project references are not inheritance. Load their projects after the
        # current extends chain has completed (a child often extends its parent
        # solution config, which references that same child).
        self.pending = list(paths)
        visited: set[Path] = set()
        while self.pending:
            path = self.pending.pop(0).resolve()
            if path not in visited:
                visited.add(path)
                self._load(path, set())

    def _load(self, path: Path, seen: set[Path]) -> Project | None:
        path = path.resolve()
        if not path.is_relative_to(self.root) or path in seen or len(seen) >= 32:
            self.errors.append((path.name, 'tsconfig extends cycle or path outside repository'))
            return None
        if path in self.projects:
            return self.projects[path]
        self.inputs.add(path)
        try:
            if path.stat().st_size > 2_000_000:
                raise ValueError('tsconfig exceeds 2 MB')
            data = jsonc(path.read_text(encoding='utf-8-sig'))
            parent = None
            if 'extends' in data:
                reference = data['extends']
                if not isinstance(reference, str) or not reference.startswith('.'):
                    raise ValueError('only a relative tsconfig extends path is supported')
                parent_path = (path.parent / reference)
                if not parent_path.suffix:
                    parent_path = parent_path.with_suffix('.json')
                parent = self._load(parent_path, seen | {path})
                if parent is None:
                    raise ValueError('base tsconfig could not be evaluated')
            options = data.get('compilerOptions', {})
            if not isinstance(options, dict):
                raise ValueError('compilerOptions must be an object')
            base = parent.base_url if parent else None
            if 'baseUrl' in options:
                if not isinstance(options['baseUrl'], str):
                    raise ValueError('baseUrl must be a string')
                base = (path.parent / options['baseUrl']).resolve()
            mappings = options.get('paths', parent.paths if parent else {})
            if not isinstance(mappings, dict) or any(not isinstance(k, str) or k.count('*') > 1
                    or not isinstance(v, list) or any(not isinstance(p, str) or p.count('*') > 1 for p in v)
                    for k, v in mappings.items()):
                raise ValueError('paths must map strings to arrays of paths with at most one wildcard')
            paths_base = (base or path.parent) if 'paths' in options else (parent.paths_base if parent else path.parent)
            def patterns(name: str, default: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
                if name not in data:
                    return default
                value = data[name]
                if not isinstance(value, list) or any(not isinstance(p, str) for p in value):
                    raise ValueError(f'{name} must be an array of paths')
                return [(path.parent, p) for p in value]
            files = parent.files if parent else None
            if 'files' in data:
                files = [(base_path / value).resolve() for base_path, value in patterns('files', [])]
            include = patterns('include', parent.include if parent else ([] if files is not None else [(path.parent, '**/*')]))
            exclude = patterns('exclude', parent.exclude if parent else [])
            project = Project(path, base, paths_base, mappings, include, exclude, files,
                              {path} | (parent.inputs if parent else set()))
            self.projects[path] = project
            for reference in data.get('references', []):
                ref = reference.get('path') if isinstance(reference, dict) else None
                if isinstance(ref, str):
                    target = path.parent / ref
                    self.pending.append(target if target.suffix == '.json' else target / 'tsconfig.json')
            return project
        except (OSError, ValueError, TypeError) as exc:
            self.errors.append((path.relative_to(self.root).as_posix(), str(exc)))
            return None

    @staticmethod
    def substitution(pattern: str, reference: str) -> str | None:
        if '*' not in pattern:
            return '' if pattern == reference else None
        before, after = pattern.split('*')
        if reference.startswith(before) and reference.endswith(after) and len(reference) >= len(before) + len(after):
            return reference[len(before):len(reference) - len(after) if after else None]
        return None

    def is_alias(self, reference: str) -> bool:
        return any(self.substitution(pattern, reference) is not None for p in self.projects.values() for pattern in p.paths)

    def candidates(self, source: Path, reference: str) -> list[list[str]]:
        selected = [p for p in self.projects.values() if p.path == self.explicit] if self.explicit else [
            p for p in self.projects.values() if p.includes(source)]
        if selected:
            selected = [p for p in selected if not any(p.path in other.inputs - {other.path} for other in selected)]
            depth = max(len(p.path.parent.parts) for p in selected)
            selected = [p for p in selected if len(p.path.parent.parts) == depth]
        groups = []
        for project in selected:
            matching = [(pattern, self.substitution(pattern, reference)) for pattern in project.paths]
            matching = [(pattern, value) for pattern, value in matching if value is not None]
            matching.sort(key=lambda item: (0 if '*' not in item[0] else 1, -len(item[0].split('*')[0])))
            if matching:
                pattern, value = matching[0]
                paths = [project.paths_base / target.replace('*', value) for target in project.paths[pattern]]
            elif project.base_url is not None:
                paths = [project.base_url / reference]
            else:
                continue
            groups.append([path.resolve().relative_to(self.root).as_posix() for path in paths
                           if path.resolve().is_relative_to(self.root)])
        return groups
