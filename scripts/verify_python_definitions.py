"""Compare Python definition syntax nodes with an emitted analysis graph."""

from __future__ import annotations

import argparse
import ast
import json
import sys
import tokenize
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

PACKAGE_SRC = Path(__file__).resolve().parents[1] / "src"
if str(PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SRC))

from connection_map.config import AnalysisConfig, discover_python_files  # noqa: E402

DefinitionKey = tuple[str, str, int, int]


class DefinitionCollector(ast.NodeVisitor):
    """Collect definitions using the same lexical parent rules as the analyzer."""

    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.scope_kinds: list[str] = []
        self.definitions: list[DefinitionKey] = []

    @property
    def current_scope(self) -> str | None:
        return self.scope_kinds[-1] if self.scope_kinds else None

    def _record(self, node: ast.AST, kind: str) -> None:
        self.definitions.append((self.relative_path, kind, int(node.lineno), int(node.col_offset)))

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._record(node, "class")
        for decorator in node.decorator_list:
            self.visit(decorator)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self.scope_kinds.append("class")
        for statement in node.body:
            self.visit(statement)
        self.scope_kinds.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        kind = "method" if self.current_scope == "class" else "function"
        self._record(node, kind)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)
        if node.returns is not None:
            self.visit(node.returns)
        self.scope_kinds.append(kind)
        for statement in node.body:
            self.visit(statement)
        self.scope_kinds.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._record(node, "lambda")
        self.scope_kinds.append("lambda")
        self.visit(node.body)
        self.scope_kinds.pop()


def _expected_definitions(
    root: Path, files: Iterable[Path]
) -> tuple[Counter[DefinitionKey], list[dict[str, str]]]:
    expected: Counter[DefinitionKey] = Counter()
    parse_errors: list[dict[str, str]] = []
    for path in files:
        relative_path = path.relative_to(root).as_posix()
        try:
            with tokenize.open(path) as handle:
                tree = ast.parse(handle.read(), filename=relative_path, type_comments=True)
        except (OSError, SyntaxError, UnicodeError) as exc:
            parse_errors.append({"file": relative_path, "error": str(exc)})
            continue
        collector = DefinitionCollector(relative_path)
        collector.visit(tree)
        expected.update(collector.definitions)
    return expected, parse_errors


def _actual_definitions(path: Path) -> Counter[DefinitionKey]:
    document = json.loads(path.read_text(encoding="utf-8"))
    actual: Counter[DefinitionKey] = Counter()
    for node in document.get("nodes", []):
        if node.get("kind") not in {"class", "function", "method", "lambda"}:
            continue
        language = (node.get("extensions") or {}).get("language")
        if language is not None and language != "python":
            continue
        if not node.get("file") or not node.get("span"):
            continue
        span = node["span"]
        actual[(node["file"], node["kind"], int(span["start_line"]), int(span["start_col"]))] += 1
    return actual


def verify_definitions(root: Path, config_path: Path, analysis_path: Path) -> dict[str, object]:
    root = root.resolve()
    config = AnalysisConfig.from_toml(config_path.resolve())
    files, skipped = discover_python_files(root, config)
    expected, parse_errors = _expected_definitions(root, files)
    actual = _actual_definitions(analysis_path.resolve())
    missing = expected - actual
    extra = actual - expected
    return {
        "analysis": str(analysis_path.resolve()),
        "configured_include_tests": config.include_tests,
        "included_python_files": len(files),
        "skipped_files": len(skipped),
        "skipped_by_reason": dict(Counter(reason for _, reason in skipped)),
        "parse_errors": parse_errors,
        "expected_definitions": sum(expected.values()),
        "expected_by_kind": dict(Counter(kind for _, kind, _, _ in expected.elements())),
        "actual_definitions": sum(actual.values()),
        "actual_by_kind": dict(Counter(kind for _, kind, _, _ in actual.elements())),
        "missing_count": sum(missing.values()),
        "extra_count": sum(extra.values()),
        "missing": [list(key) + [count] for key, count in sorted(missing.items())],
        "extra": [list(key) + [count] for key, count in sorted(extra.items())],
        "complete": not missing and not extra and not parse_errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Python definition coverage in an analysis graph.")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--analysis", type=Path, required=True)
    args = parser.parse_args()
    result = verify_definitions(args.root, args.config, args.analysis)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
