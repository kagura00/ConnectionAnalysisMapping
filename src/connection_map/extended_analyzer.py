"""Syntax-level analyzers for the additional v1 language set.

The existing analyzers remain intentionally language-specific where a mature
grammar and relationship model already exist.  This module provides a shared
adapter for the additional languages requested for v1: each language has its
own extension/profile entry, while the contract, diagnostics, stable IDs,
imports, containment, inheritance and basic call extraction are shared.

The adapter never imports, builds, executes, or evaluates repository code.  It
uses Tree-sitter to validate syntax and report parser availability, while the
cross-language relationship extraction uses masked, profile-specific lexical
patterns.  This keeps the connection map useful across many languages without
claiming AST-complete type or runtime semantics.  Uncertain relationships are
represented as unresolved edges.
"""

from __future__ import annotations

import hashlib
import importlib
import posixpath
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import AnalysisConfig, discover_source_files
from .contract import validate_document
from .model import GraphBuilder
from .phase3_common import (
    add_relation,
    add_skipped_diagnostics,
    diagnostic,
    external_node,
    finish_document,
    unique_id,
)

ANALYZER_NAME = "connection-map-extended-tree-sitter"
ANALYZER_VERSION = "0.1.0"


@dataclass(frozen=True, slots=True)
class DeclarationPattern:
    pattern: str
    kind: str
    container: bool = False
    name_group: str = "name"
    combine_groups: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LanguageProfile:
    grammar: str | None
    declarations: tuple[DeclarationPattern, ...]
    imports: tuple[str, ...] = ()
    inheritance: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    call_pattern: str | None = "__default_call_pattern__"
    comment_prefixes: tuple[str, ...] = ("//", "#", "--")
    case_insensitive: bool = False
    end_keywords: tuple[str, ...] = ()


_NAME = r"[A-Za-z_][A-Za-z0-9_.$'!?-]*"
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_CI = re.IGNORECASE | re.MULTILINE
_ML = re.MULTILINE
_DEFAULT_CALL_PATTERN = r"\b(?P<name>[A-Za-z_][A-Za-z0-9_.$']*)\s*\("


def _profile(
    grammar: str | None,
    declarations: tuple[DeclarationPattern, ...],
    *,
    imports: tuple[str, ...] = (),
    inheritance: tuple[str, ...] = (),
    references: tuple[str, ...] = (),
    call_pattern: str | None = _DEFAULT_CALL_PATTERN,
    comments: tuple[str, ...] = ("//", "#", "--"),
    case_insensitive: bool = False,
    end_keywords: tuple[str, ...] = (),
) -> LanguageProfile:
    return LanguageProfile(
        grammar=grammar,
        declarations=declarations,
        imports=imports,
        inheritance=inheritance,
        references=references,
        call_pattern=call_pattern,
        comment_prefixes=comments,
        case_insensitive=case_insensitive,
        end_keywords=end_keywords,
    )


LANGUAGE_PROFILES: dict[str, LanguageProfile] = {
    "vbnet": _profile(
        "vbnet",
        (
            DeclarationPattern(r"^\s*(?:Namespace)\s+(?P<name>[\w.]+)", "namespace", True),
            DeclarationPattern(r"^\s*(?:(?:Public|Private|Friend|Partial|NotInheritable|MustInherit)\s+)*(?:Class|Module|Structure)\s+(?P<name>\w+)", "class", True),
            DeclarationPattern(r"^\s*(?:(?:Public|Private|Friend|Partial)\s+)*Interface\s+(?P<name>\w+)", "interface", True),
            DeclarationPattern(r"^\s*(?:(?:Public|Private|Friend)\s+)*Enum\s+(?P<name>\w+)", "type", True),
            DeclarationPattern(
                r"^\s*(?:(?:Public|Private|Protected|Friend|Shared|Overloads|Overrides|Async|Partial)\s+)*(?:Sub|Function|Property)\s+(?P<name>\w+)",
                "function",
            ),
        ),
        imports=(r"^\s*Imports\s+(?P<ref>[\w.]+)",),
        inheritance=(
            r"^\s*Inherits\s+(?P<base>[\w.]+)",
            r"^\s*Implements\s+(?P<base>[\w., ]+)",
            r"^\s*(?:Class|Structure)\s+(?P<owner>\w+).*?\b(?:Inherits|Implements)\s+(?P<base>[\w.]+)",
        ),
        case_insensitive=True,
        comments=("'",),
        end_keywords=("End Namespace", "End Class", "End Module", "End Structure", "End Interface"),
    ),
    "vba": _profile(
        None,
        (
            DeclarationPattern(r"^\s*Attribute\s+VB_Name\s*=\s*\"(?P<name>[^\"]+)\"", "namespace", True),
            DeclarationPattern(r"^\s*(?:Public|Private|Friend|Static)?\s*(?:Sub|Function)\s+(?P<name>\w+)", "function"),
            DeclarationPattern(r"^\s*Property\s+(?:Get|Let|Set)\s+(?P<name>\w+)", "function"),
            DeclarationPattern(r"^\s*(?:Public|Private)?\s*(?:Type|Enum)\s+(?P<name>\w+)", "type", True),
        ),
        imports=(r"^\s*(?:Declare|#If).*?\b(?P<ref>\w+)",),
        inheritance=(r"^\s*Implements\s+(?P<base>[\w., ]+)",),
        case_insensitive=True,
        comments=("'",),
        end_keywords=(),
    ),
    "lua": _profile(
        "lua",
        (
            DeclarationPattern(r"^\s*(?:local\s+)?function\s+(?P<name>[\w.]+)\s*\(", "function"),
        ),
        imports=(r"\brequire\s*\(\s*[\"'](?P<ref>[^\"']+)",),
        comments=("--", "{-"),
    ),
    "haskell": _profile(
        "haskell",
        (
            DeclarationPattern(r"^\s*(?:data|newtype|type)\s+(?P<name>\w+)", "type", True),
            DeclarationPattern(r"^\s*class\s+(?:\([^)]*\)\s*=>\s*)?(?P<name>\w+)", "interface", True),
            DeclarationPattern(r"^\s*(?P<name>[a-z_][\w']*)\s*(?:\([^\n]*\)|[A-Za-z_][^=\n]*)?\s*=", "function"),
        ),
        imports=(r"^\s*import\s+(?:qualified\s+)?(?P<ref>[A-Za-z_][\w.]*)",),
        inheritance=(r"^\s*class\s+\((?P<base>[^)]+)\)\s*=>\s*(?P<owner>\w+)",),
        # Haskell applications may take identifiers, constructors, numeric
        # literals, lists, tuples, or parenthesized expressions as the first
        # argument.  The declaration-range check below prevents the left-hand
        # side of a definition from being reported as a call.
        call_pattern=r"\b(?P<name>[a-z_][\w']*)\s+(?=[A-Za-z0-9_(\[])",
        comments=("--",),
    ),
    "perl": _profile(
        "perl",
        (
            DeclarationPattern(r"^\s*package\s+(?P<name>[\w:]+)", "namespace", True),
            DeclarationPattern(r"^\s*sub\s+(?P<name>\w+)", "function"),
        ),
        imports=(r"^\s*(?:use|require)\s+(?:q\w*\s*)?[\"']?(?P<ref>[\w:./-]+)",),
        comments=("#",),
    ),
    "matlab": _profile(
        "matlab",
        (
            DeclarationPattern(
                r"^\s*function\s+(?:(?:\[[^]]+\]|[A-Za-z_]\w*)\s*=\s*)?(?P<name>[A-Za-z_]\w*)\s*(?:\([^\n]*\))?",
                "function",
            ),
            DeclarationPattern(r"^\s*classdef\s+(?P<name>\w+)", "class", True),
        ),
        imports=(r"^\s*import\s+(?P<ref>[\w.]+)",),
        comments=("%",),
        end_keywords=("end",),
    ),
    "cobol": _profile(
        "cobol",
        (
            DeclarationPattern(r"^\s*PROGRAM-ID\.\s*(?P<name>[A-Z0-9-]+)", "namespace", True),
            DeclarationPattern(r"^\s*(?P<name>[A-Z][A-Z0-9-]+)\s+SECTION\.", "namespace", True),
            DeclarationPattern(r"^\s*(?P<name>[A-Z][A-Z0-9-]+)\.", "function"),
        ),
        imports=(r"^\s*COPY\s+(?P<ref>[A-Z0-9-]+)",),
        call_pattern=r"\bPERFORM\s+(?P<name>[A-Z][A-Z0-9-]*)",
        comments=("*", "*>"),
        case_insensitive=True,
    ),
    "fortran": _profile(
        "fortran",
        (
            DeclarationPattern(r"^\s*(?:module)\s+(?P<name>\w+)", "namespace", True),
            DeclarationPattern(r"^\s*(?:program)\s+(?P<name>\w+)", "namespace", True),
            DeclarationPattern(r"^\s*(?:subroutine|function)\s+(?P<name>\w+)", "function"),
            DeclarationPattern(r"^\s*(?:type)\s*(?:::)?\s*(?P<name>\w+)", "type", True),
        ),
        imports=(r"^\s*use\s+(?P<ref>\w+)",),
        call_pattern=r"\bcall\s+(?P<name>\w+)",
        comments=("!",),
        case_insensitive=True,
        end_keywords=("end", "end module", "end subroutine", "end function", "end type"),
    ),
    "r": _profile(
        "r",
        (
            DeclarationPattern(r"^\s*(?P<name>[A-Za-z.][\w.]*)\s*(?:<-|=)\s*function\s*\(", "function"),
        ),
        imports=(r"\b(?:library|require)\s*\(\s*[\"']?(?P<ref>[\w.]+)",),
        comments=("#",),
        end_keywords=("}",),
    ),
        "objective-c": _profile(
        "objc",
        (
            DeclarationPattern(r"^\s*@interface\s+(?P<name>\w+)", "class", True),
            DeclarationPattern(r"^\s*@implementation\s+(?P<name>\w+)", "class", True),
            DeclarationPattern(r"^\s*[-+]\s*\([^)]*\)\s*(?P<name>[A-Za-z_]\w*(?::[^\n{;]*)?)", "method"),
        ),
        imports=(r"^\s*#import\s+[<\"](?P<ref>[^>\"]+)",),
        inheritance=(r"^\s*@interface\s+(?P<owner>\w+)\s*:\s*(?P<base>\w+)",),
        call_pattern=(
            r"\[\s*[A-Za-z_]\w*\s+(?P<name>[A-Za-z_]\w*(?::[^\]\n]*)?)\]"
            r"|\b(?P<simple_name>[A-Za-z_]\w*)\s*(?:\([^)]*\)|(?=\]))"
        ),
        comments=("//", "/*"),
        end_keywords=("@end",),
    ),
    "cuda": _profile(
        "cuda",
        (
            DeclarationPattern(r"^\s*(?:struct|class)\s+(?P<name>\w+)", "type", True),
            DeclarationPattern(
                r"^\s*(?:(?:__\w+|static|inline|extern|const|unsigned|signed|long|short|void|int|float|double|char|auto|struct|class)\s+)+(?P<name>\w+)\s*\([^;{}]*\)\s*\{",
                "function",
            ),
        ),
        imports=(r"^\s*#include\s*[<\"](?P<ref>[^>\"]+)",),
        comments=("//", "/*"),
    ),
    "groovy": _profile(
        "groovy",
        (
            DeclarationPattern(r"^\s*package\s+(?P<name>[\w.]+)", "namespace", True),
            DeclarationPattern(r"^\s*(?:class|interface|trait|enum)\s+(?P<name>\w+)", "class", True),
            DeclarationPattern(r"^\s*def\s+(?P<name>\w+)\s*\(", "function"),
            DeclarationPattern(r"^\s*(?:public|private|protected|static|final|void|def|\w+)\s+(?P<name>\w+)\s*\([^;{}]*\)\s*\{", "function"),
        ),
        imports=(r"^\s*import\s+(?P<ref>[\w.]+)",),
        comments=("//", "/*"),
    ),
    "fsharp": _profile(
        "fsharp",
        (
            DeclarationPattern(r"^\s*(?:namespace|module)\s+(?P<name>[\w.]+)", "namespace", True),
            DeclarationPattern(r"^\s*type\s+(?P<name>\w+)", "type", True),
            DeclarationPattern(r"^\s*let\s+(?:rec\s+)?(?P<name>\w+)", "function"),
            DeclarationPattern(r"^\s*member\s+[^.]+\.(?P<name>\w+)", "method"),
        ),
        imports=(r"^\s*open\s+(?P<ref>[\w.]+)",),
        inheritance=(r"^\s*type\s+(?P<owner>\w+).*?=\s*inherit\s+(?P<base>\w+)",),
        comments=("//", "(*"),
    ),
    "assembly": _profile(
        "asm",
        (
            DeclarationPattern(r"^\s*(?![.])(?P<name>[A-Za-z_.$?][\w.$?@]*)\s*:\s*$", "function"),
        ),
        call_pattern=r"\b(?:call|bl|jal|jsr)\s+(?P<name>[A-Za-z_.$?][\w.$?@]*)",
        comments=(";", "#"),
    ),
    "hcl": _profile(
        "hcl",
        (
            DeclarationPattern(r"^\s*(?:resource|data)\s+\"(?P<resource>[^\"]+)\"\s+\"(?P<name>[^\"]+)\"", "type", True, combine_groups=("resource", "name")),
            DeclarationPattern(r"^\s*(?:module|variable|locals|output|provider|terraform)\s+\"?(?P<name>[\w.-]+)\"?", "type", True),
        ),
        imports=(r"\bsource\s*=\s*\"(?P<ref>[^\"]+)\"",),
        references=(r"\b(?:var|local|module)\.(?P<ref>[A-Za-z_][\w-]*)",),
        call_pattern=None,
        comments=("#", "//", "/*"),
    ),
    "gdscript": _profile(
        "gdscript",
        (
            DeclarationPattern(r"^\s*class_name\s+(?P<name>\w+)", "class", True),
            DeclarationPattern(r"^\s*class\s+(?P<name>\w+)", "class", True),
            DeclarationPattern(r"^\s*func\s+(?P<name>\w+)", "function"),
        ),
        imports=(r"^\s*extends\s+(?P<ref>\w+)",),
        inheritance=(r"^\s*extends\s+(?P<base>\w+)",),
        comments=("#",),
    ),
    "elixir": _profile(
        "elixir",
        (
            DeclarationPattern(r"^\s*defmodule\s+(?P<name>[\w.]+)", "namespace", True),
            DeclarationPattern(r"^\s*defprotocol\s+(?P<name>[\w.]+)", "interface", True),
            DeclarationPattern(r"^\s*defimpl\s+(?P<name>[\w.]+)", "class", True),
            DeclarationPattern(r"^\s*def(?:p|macro|macrop)?\s+(?P<name>\w+)", "function"),
        ),
        imports=(r"^\s*(?:alias|import|require|use)\s+(?P<ref>[\w.]+)",),
        call_pattern=r"\b(?P<name>[a-z_][\w!?]*)\s*\(",
        comments=("#",),
        end_keywords=("end",),
    ),
    "zig": _profile(
        "zig",
        (
            DeclarationPattern(r"^\s*(?:pub\s+)?fn\s+(?P<name>\w+)", "function"),
            DeclarationPattern(r"^\s*const\s+(?P<name>\w+)\s*=\s*(?:struct|enum|union)", "type", True),
            DeclarationPattern(r"^\s*test\s+\"(?P<name>[^\"]+)\"", "function"),
        ),
        imports=(r"@import\s*\(\s*\"(?P<ref>[^\"]+)\"",),
        comments=("//",),
    ),
    "julia": _profile(
        "julia",
        (
            DeclarationPattern(r"^\s*module\s+(?P<name>\w+)", "namespace", True),
            DeclarationPattern(r"^\s*(?:mutable\s+)?struct\s+(?P<name>\w+)", "type", True),
            DeclarationPattern(r"^\s*abstract\s+type\s+(?P<name>\w+)", "type", True),
            DeclarationPattern(r"^\s*function\s+(?P<name>\w+)", "function"),
            DeclarationPattern(r"^\s*(?P<name>\w+)\s*\([^\n]*\)\s*=", "function"),
        ),
        imports=(r"^\s*(?:using|import)\s+(?P<ref>[\w.]+)",),
        comments=("#",),
        end_keywords=("end",),
    ),
    "pascal": _profile(
        "pascal",
        (
            DeclarationPattern(r"^\s*unit\s+(?P<name>\w+)", "namespace", True),
            DeclarationPattern(r"^\s*program\s+(?P<name>\w+)", "namespace", True),
            DeclarationPattern(r"^\s*(?:type\s+)?(?P<name>\w+)\s*=\s*(?:class|record|object)", "class", True),
            DeclarationPattern(r"^\s*(?:procedure|function|constructor|destructor)\s+(?P<name>[\w.]+)", "function"),
        ),
        imports=(r"^\s*uses\s+(?P<ref>[\w.]+)",),
        call_pattern=r"\b(?P<name>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s*(?:\([^;]*\))?\s*;",
        comments=("//", "{", "(*"),
        end_keywords=("end;",),
    ),
    "erlang": _profile(
        "erlang",
        (
            DeclarationPattern(r"^\s*-module\s*\(\s*(?P<name>[\w@]+)", "namespace", True),
            DeclarationPattern(r"^\s*(?P<name>[a-z][\w@]*)\s*\([^\n]*\)\s*->", "function"),
        ),
        imports=(r"-include\s*\(\s*\"(?P<ref>[^\"]+)\"", r"-import\s*\(\s*(?P<ref>[^)]+)"),
        call_pattern=r"\b(?P<name>[a-z][\w@]*)\s*\(",
        comments=("%",),
    ),
}


@dataclass(slots=True)
class ExtendedFile:
    path: Path
    relative_path: str
    language: str
    grammar: str | None
    source: bytes
    text: str
    comment_text: str
    code_text: str
    tree: Any | None
    module_id: str


@dataclass(slots=True)
class Declaration:
    language: str
    file_path: str
    name: str
    kind: str
    start: int
    end: int
    container: bool
    raw_kind: str
    node_id: str | None = None
    parent_id: str | None = None
    qualified_name: str | None = None
    end_scope: int | None = None


@lru_cache(maxsize=64)
def _parser_for(grammar: str) -> Any:
    if grammar == "vbnet":
        module = importlib.import_module("tree_sitter_vbnet")
        tree_sitter = importlib.import_module("tree_sitter")
        return tree_sitter.Parser(tree_sitter.Language(module.language()))
    from tree_sitter_language_pack import get_parser

    return get_parser(grammar)


def analyze_repository(
    root: Path,
    config: AnalysisConfig | None = None,
    *,
    deterministic: bool = False,
    commit_sha: str | None = None,
) -> dict[str, Any]:
    active_config = config or AnalysisConfig(language="mixed", languages=list(LANGUAGE_PROFILES))
    active_config.validate()
    if active_config.language != "mixed" and active_config.language not in LANGUAGE_PROFILES:
        raise ValueError("Additional-language analyzer requires a concrete language or language = 'mixed'")

    selected = active_config.active_languages()
    root = root.resolve()
    builder = GraphBuilder()
    files: list[ExtendedFile] = []
    skipped: list[tuple[str, str]] = []
    grammar_names: set[str] = set()
    parser_messages: dict[str, str] = {}

    for language in selected:
        profile = LANGUAGE_PROFILES[language]
        language_files, language_skipped = discover_source_files(root, active_config, languages={language})
        skipped.extend(language_skipped)
        grammar = profile.grammar
        if grammar:
            grammar_names.add(grammar)
            try:
                _parser_for(grammar)
            except Exception as exc:  # pragma: no cover - depends on optional grammar installation
                parser_messages[language] = str(exc)
        for path in language_files:
            relative = path.relative_to(root).as_posix()
            try:
                source = path.read_bytes()
            except (OSError, UnicodeError):
                skipped.append((relative, "read_failed"))
                continue
            text = source.decode("utf-8", errors="replace")
            tree = None
            if grammar and language not in parser_messages:
                try:
                    tree = _parser_for(grammar).parse(source)
                except Exception as exc:  # pragma: no cover - parser-specific
                    parser_messages[language] = str(exc)
            files.append(
                ExtendedFile(
                    path=path,
                    relative_path=relative,
                    language=language,
                    grammar=grammar,
                    source=source,
                    text=text,
                    comment_text=_mask_source(text, profile, mask_strings=False),
                    code_text=_mask_source(text, profile, mask_strings=True),
                    tree=tree,
                    module_id=f"{language}:{relative}:module",
                )
            )

    files.sort(key=lambda item: (item.language, item.relative_path))
    for item in files:
        _add_module(builder, item)
    add_skipped_diagnostics(builder, sorted(set(skipped)))

    declarations_by_file: dict[tuple[str, str], list[Declaration]] = {}
    symbols_by_name: dict[tuple[str, str], list[Declaration]] = {}
    for item in files:
        declarations = _collect_declarations(item, LANGUAGE_PROFILES[item.language])
        declarations_by_file[(item.language, item.relative_path)] = declarations
        _add_declarations(builder, item, declarations)
        for declaration in declarations:
            if declaration.node_id:
                symbols_by_name.setdefault((item.language, _symbol_key(item.language, declaration.name)), []).append(
                    declaration
                )

    external_cache: dict[str, str] = {}
    files_by_language_path: dict[str, dict[str, ExtendedFile]] = {}
    for item in files:
        files_by_language_path.setdefault(item.language, {})[item.relative_path] = item
    for item in files:
        profile = LANGUAGE_PROFILES[item.language]
        declarations = declarations_by_file[(item.language, item.relative_path)]
        _collect_imports(
            builder,
            item,
            profile,
            files_by_language_path,
            external_cache,
        )
        _collect_inheritance(builder, item, profile, declarations, symbols_by_name, external_cache)
        _collect_references(builder, item, profile, declarations, symbols_by_name, external_cache)
        _collect_calls(builder, item, profile, declarations, symbols_by_name, external_cache)
        _add_parser_diagnostics(builder, item, profile, parser_messages.get(item.language))

    grammar_label = next(iter(sorted(grammar_names)), "regex")
    document = finish_document(
        builder,
        root=root,
        language=active_config.language,
        languages=list(selected),
        analyzer_name=ANALYZER_NAME,
        analyzer_version=ANALYZER_VERSION,
        config=active_config,
        deterministic=deterministic,
        commit_sha=commit_sha,
        grammar=grammar_label,
        extra_runtime={"grammars": sorted(grammar_names) or ["regex"]},
        extensions={
            "profile_languages": list(selected),
            "regex_fallback": bool(parser_messages or "vba" in selected),
            "relationship_extraction": "profile-regex",
        },
    )
    validate_document(document)
    return document


def _add_module(builder: GraphBuilder, item: ExtendedFile) -> None:
    end_line, end_col = _line_col(item.text, len(item.text))
    builder.add_node(
        {
            "id": item.module_id,
            "kind": "module",
            "qualified_name": item.relative_path,
            "display_name": item.relative_path,
            "file": item.relative_path,
            "span": {
                "start_line": 1,
                "start_col": 0,
                "end_line": max(1, end_line),
                "end_col": max(0, end_col),
            },
            "parent_id": None,
            "visibility": "public",
            "extensions": {
                "language": item.language,
                "grammar": item.grammar or "regex",
                "parser_mode": "tree-sitter" if item.tree is not None else "regex-fallback",
                "extraction_mode": "profile-regex",
            },
        }
    )


def _collect_declarations(item: ExtendedFile, profile: LanguageProfile) -> list[Declaration]:
    flags = _flags(profile)
    found: dict[tuple[int, str, str], Declaration] = {}
    for spec in profile.declarations:
        for match in re.finditer(spec.pattern, item.comment_text, flags):
            if not _match_starts_in_code(item, match.start()):
                continue
            line_start = item.text.rfind("\n", 0, match.start()) + 1
            if _is_comment_line(item.text[line_start:match.end()], profile.comment_prefixes):
                continue
            groups = match.groupdict()
            name = groups.get(spec.name_group) or ""
            if spec.combine_groups:
                name = ".".join(groups.get(group, "") for group in spec.combine_groups if groups.get(group))
            name = _clean_objective_c_name(name) if item.language == "objective-c" else _clean_name(name)
            if not name:
                continue
            key = (match.start(), spec.kind, name.casefold() if profile.case_insensitive else name)
            found[key] = Declaration(
                language=item.language,
                file_path=item.relative_path,
                name=name,
                kind=spec.kind,
                start=match.start(),
                end=match.end(),
                container=spec.container,
                raw_kind=spec.kind,
            )
    declarations = sorted(found.values(), key=lambda value: (value.start, value.end, value.kind, value.name))
    merged: list[Declaration] = []
    for declaration in declarations:
        existing = next(
            (
                value
                for value in merged
                if (
                    (value.container and declaration.container)
                    or (item.language == "objective-c" and value.kind == "method" and declaration.kind == "method")
                )
                and value.kind == declaration.kind
                and _symbol_key(item.language, value.name) == _symbol_key(item.language, declaration.name)
            ),
            None,
        )
        if existing is not None and item.language == "objective-c":
            existing.end = max(existing.end, declaration.end)
            if declaration.kind == "method" and "{" in item.text[declaration.start : min(len(item.text), declaration.end + 200)]:
                existing.start = declaration.start
                existing.end = declaration.end
            continue
        merged.append(declaration)
    declarations = merged
    for declaration_index, declaration in enumerate(declarations):
        if not (declaration.container or declaration.kind in {"function", "method"}):
            continue
        declaration.end_scope = _scope_end(item.code_text, declaration.end, profile, declaration.kind)
        if not declaration.container and declaration.end_scope >= len(item.text):
            next_declaration = next(
                (
                    candidate.start
                    for candidate in declarations[declaration_index + 1 :]
                    if candidate.start > declaration.start
                ),
                None,
            )
            declaration.end_scope = next_declaration or _next_top_level_boundary(item.code_text, declaration.end)
    return declarations


def _add_declarations(builder: GraphBuilder, item: ExtendedFile, declarations: list[Declaration]) -> None:
    containers: list[Declaration] = []
    for declaration in declarations:
        if item.language == "cobol" and declaration.name.upper() in {
            "PROGRAM-ID",
            "IDENTIFICATION",
            "DIVISION",
            "PROCEDURE",
            "GOBACK",
        }:
            continue
        parent = _nearest_container(declaration, containers)
        parent_id = parent.node_id if parent else item.module_id
        if declaration.kind == "function" and parent is not None:
            declaration.kind = "method"
        qualified_parent = parent.qualified_name if parent else item.relative_path
        qualified_name = f"{qualified_parent}.{declaration.name}"
        node_id = unique_id(
            builder.nodes,
            f"{item.language}:{item.relative_path}:{qualified_name}:{declaration.kind}",
            declaration.start,
        )
        declaration.node_id = node_id
        declaration.parent_id = parent_id
        declaration.qualified_name = qualified_name
        signature = _first_line(item.text[declaration.start : declaration.end])
        node: dict[str, Any] = {
            "id": node_id,
            "kind": declaration.kind,
            "qualified_name": qualified_name,
            "display_name": declaration.name,
            "file": item.relative_path,
            "span": _span(item.text, declaration.start, declaration.end),
            "parent_id": parent_id,
            "visibility": "private" if declaration.name.startswith(("_", "-")) else "public",
            "signature": signature,
            "extensions": {
                "language": item.language,
                "grammar": item.grammar or "regex",
                "declaration_kind": declaration.raw_kind,
                "member": declaration.kind == "method",
            },
        }
        if declaration.kind in {"function", "method"}:
            body = item.text[declaration.end : declaration.end_scope or min(len(item.text), declaration.end + 3000)]
            node["return_behavior"] = _return_behavior(body)
            node["execution_kind"] = "async" if re.search(r"\basync\b", signature, re.IGNORECASE) else "sync"
        builder.add_node(node)
        add_relation(
            builder,
            source_id=parent_id,
            target_id=node_id,
            relation_type="contains",
            source_span=_span(item.text, declaration.start, declaration.end),
            detail={"kind": "lexical_definition", "declaration_kind": declaration.raw_kind},
            edge_prefix=f"extended-{item.language}",
            provenance="unknown",
        )
        if declaration.container:
            containers.append(declaration)
            containers.sort(key=lambda value: (value.start, -(value.end_scope or len(item.text))))


def _collect_imports(
    builder: GraphBuilder,
    item: ExtendedFile,
    profile: LanguageProfile,
    files: dict[str, dict[str, ExtendedFile]],
    external_cache: dict[str, str],
) -> None:
    for pattern in profile.imports:
        for match in re.finditer(pattern, item.comment_text, _flags(profile)):
            if not _match_starts_in_code(item, match.start()):
                continue
            reference = _clean_reference(match.groupdict().get("ref", ""))
            if not reference:
                continue
            target = _resolve_file(item, reference, files)
            if target is not None:
                target_id = target.module_id
                status = "resolved"
                confidence = 1.0
            else:
                target_id = external_node(
                    builder,
                    external_cache,
                    node_id=f"extended:{item.language}:import:{hashlib.sha1(reference.encode()).hexdigest()[:16]}",
                    qualified_name=reference,
                    display_name=reference,
                    language=item.language,
                    extensions={"reference_kind": "import"},
                )
                status = "external"
                confidence = 0.65
            add_relation(
                builder,
                source_id=item.module_id,
                target_id=target_id,
                relation_type="imports",
                source_span=_span(item.text, match.start(), match.end()),
                detail={"reference": reference, "kind": "static_import"},
                resolution_status=status,
                confidence=confidence,
                edge_prefix=f"extended-{item.language}",
                provenance="unknown",
            )


def _collect_inheritance(
    builder: GraphBuilder,
    item: ExtendedFile,
    profile: LanguageProfile,
    declarations: list[Declaration],
    symbols: dict[tuple[str, str], list[Declaration]],
    external_cache: dict[str, str],
) -> None:
    for pattern in profile.inheritance:
        for match in re.finditer(pattern, item.code_text, _flags(profile)):
            if not _match_starts_in_code(item, match.start()):
                continue
            owner_name = _clean_name(match.groupdict().get("owner", ""))
            owner = _scope_for_offset(declarations, match.start(), owner_name, item.language)
            bases = [part.strip() for part in re.split(r"[, ]+", match.groupdict().get("base", "")) if part.strip()]
            for base_name in bases:
                base_name = _clean_name(base_name)
                if not base_name or base_name.casefold() in {"public", "private", "protected"}:
                    continue
                source_id = owner.node_id if owner and owner.node_id else item.module_id
                target = _resolve_symbol(symbols, item.language, base_name)
                status = "resolved" if target and target.node_id else "unresolved"
                target_id = target.node_id if target and target.node_id else external_node(
                    builder,
                    external_cache,
                    node_id=f"extended:{item.language}:inherit:{_symbol_key(item.language, base_name)}",
                    qualified_name=base_name,
                    display_name=base_name,
                    language=item.language,
                    kind="unknown",
                )
                add_relation(
                    builder,
                    source_id=source_id,
                    target_id=target_id,
                    relation_type="inherits",
                    source_span=_span(item.text, match.start(), match.end()),
                    detail={"base": base_name, "kind": "static_inheritance"},
                    resolution_status=status,
                    confidence=1.0 if status == "resolved" else 0.45,
                    edge_prefix=f"extended-{item.language}",
                    provenance="unknown",
                )


def _collect_references(
    builder: GraphBuilder,
    item: ExtendedFile,
    profile: LanguageProfile,
    declarations: list[Declaration],
    symbols: dict[tuple[str, str], list[Declaration]],
    external_cache: dict[str, str],
) -> None:
    for pattern in profile.references:
        for match in re.finditer(pattern, item.code_text, _flags(profile)):
            if not _match_starts_in_code(item, match.start()):
                continue
            reference = _clean_name(match.groupdict().get("ref", ""))
            if not reference:
                continue
            source = _scope_for_offset(declarations, match.start(), "", item.language)
            source_id = source.node_id if source and source.node_id else item.module_id
            target = _resolve_symbol(symbols, item.language, reference)
            status = "resolved" if target and target.node_id else "unresolved"
            target_id = target.node_id if target and target.node_id else external_node(
                builder,
                external_cache,
                node_id=f"extended:{item.language}:reference:{_symbol_key(item.language, reference)}",
                qualified_name=reference,
                display_name=reference,
                language=item.language,
                kind="unknown",
            )
            add_relation(
                builder,
                source_id=source_id,
                target_id=target_id,
                relation_type="references",
                source_span=_span(item.text, match.start(), match.end()),
                detail={"reference": reference, "kind": "syntax_reference"},
                resolution_status=status,
                confidence=1.0 if status == "resolved" else 0.45,
                edge_prefix=f"extended-{item.language}",
                provenance="unknown",
            )


def _collect_calls(
    builder: GraphBuilder,
    item: ExtendedFile,
    profile: LanguageProfile,
    declarations: list[Declaration],
    symbols: dict[tuple[str, str], list[Declaration]],
    external_cache: dict[str, str],
) -> None:
    if not profile.call_pattern:
        return
    ignored = {
        "if", "for", "while", "switch", "catch", "function", "class", "struct", "return",
        "sizeof", "typeof", "new", "module", "def", "sub", "call", "select", "case", "with",
        "export", "import", "include", "uses", "interface", "end", "self", "let", "in", "where",
        "do", "of", "then", "else", "instance", "type", "data", "newtype",
        "qualified",
    }
    for match in re.finditer(profile.call_pattern, item.code_text, _flags(profile)):
        if not _match_starts_in_code(item, match.start()):
            continue
        groups = match.groupdict()
        raw_name = groups.get("name") or groups.get("simple_name", "")
        name = _clean_objective_c_name(raw_name) if item.language == "objective-c" else _clean_name(raw_name)
        if not name or name.casefold() in ignored:
            continue
        line_start = item.text.rfind("\n", 0, match.start()) + 1
        line_end = item.text.find("\n", match.start())
        if line_end < 0:
            line_end = len(item.text)
        line_text = item.text[line_start:line_end].strip()
        if item.language == "pascal" and re.match(
            r"^(?:unit|interface|implementation|uses)\b", line_text, re.IGNORECASE
        ):
            continue
        if any(declaration.start <= match.start() <= declaration.end for declaration in declarations):
            continue
        source = _scope_for_offset(declarations, match.start(), "", item.language)
        source_id = source.node_id if source and source.node_id else item.module_id
        target = _resolve_symbol(symbols, item.language, name)
        status = "resolved" if target and target.node_id else "unresolved"
        target_id = target.node_id if target and target.node_id else external_node(
            builder,
            external_cache,
            node_id=f"extended:{item.language}:call:{_symbol_key(item.language, name)}",
            qualified_name=name,
            display_name=name,
            language=item.language,
            extensions={"reference_kind": "call"},
        )
        add_relation(
            builder,
            source_id=source_id,
            target_id=target_id,
            relation_type="calls",
            source_span=_span(item.text, match.start(), match.end()),
            detail={"expression": f"{name}(...)", "call_kind": "direct"},
            resolution_status=status,
            confidence=0.9 if status == "resolved" else 0.4,
            edge_prefix=f"extended-{item.language}",
            provenance="unknown",
        )


def _add_parser_diagnostics(builder: GraphBuilder, item: ExtendedFile, profile: LanguageProfile, parser_message: str | None) -> None:
    if parser_message:
        diagnostic(
            builder,
            code="parser_unavailable",
            message=f"Tree-sitter grammar is unavailable; lexical fallback used for {item.language}",
            file=item.relative_path,
            span=_span(item.text, 0, len(item.text)),
            details={"grammar": profile.grammar, "error": parser_message},
        )
    elif item.tree is not None and item.tree.root_node.has_error:
        diagnostic(
            builder,
            code="parse_error",
            message=f"Tree-sitter reported a {item.language} syntax error; extracted nodes are partial",
            file=item.relative_path,
            span=_span(item.text, 0, len(item.text)),
            details={"grammar": profile.grammar},
        )
    if item.language == "vba":
        diagnostic(
            builder,
            code="unsupported_construct",
            message="VBA uses the conservative lexical adapter; Excel/COM semantics are not resolved",
            file=item.relative_path,
            span=_span(item.text, 0, len(item.text)),
            severity="info",
            details={"grammar": "regex", "semantic_analysis": False},
        )


def _flags(profile: LanguageProfile) -> int:
    return _ML | (re.IGNORECASE if profile.case_insensitive else 0)


def _match_starts_in_code(item: ExtendedFile, offset: int) -> bool:
    """Return whether a regex match begins outside a masked literal/comment."""

    if offset >= len(item.text):
        return False
    return item.code_text[offset] != " " or item.text[offset] in " \t\r\n"


def _mask_source(text: str, profile: LanguageProfile, *, mask_strings: bool) -> str:
    """Blank comments and optionally strings while preserving source offsets.

    The extended adapter still uses language profiles rather than full AST
    extraction.  Keeping offsets stable lets regex matches point into the
    original source while preventing comments and literals from becoming
    declarations or relationships.
    """

    chars = list(text)
    line_prefixes = tuple(sorted(
        (prefix for prefix in profile.comment_prefixes if prefix not in {"/*", "(*", "{", "{-"}),
        key=len,
        reverse=True,
    ))
    block_comments = []
    if "/*" in profile.comment_prefixes:
        block_comments.append(("/*", "*/"))
    if "(*" in profile.comment_prefixes:
        block_comments.append(("(*", "*)"))
    if "{" in profile.comment_prefixes:
        block_comments.append(("{", "}"))
    if "{-" in profile.comment_prefixes:
        block_comments.append(("{-", "-}"))
    block_comments.sort(key=lambda value: len(value[0]), reverse=True)
    string_delimiters = ('"', "`", "'")
    if "'" in profile.comment_prefixes:
        string_delimiters = tuple(value for value in string_delimiters if value != "'")

    def blank(start: int, end: int) -> None:
        for index in range(start, end):
            if text[index] not in "\r\n":
                chars[index] = " "

    index = 0
    string_delimiter: str | None = None
    block_end: str | None = None
    while index < len(text):
        if block_end is not None:
            if text.startswith(block_end, index):
                blank(index, index + len(block_end))
                index += len(block_end)
                block_end = None
            else:
                blank(index, index + 1)
                index += 1
            continue
        if string_delimiter is not None:
            if text[index] == "\\" and index + 1 < len(text):
                if mask_strings:
                    blank(index, index + 2)
                index += 2
                continue
            if text[index] == string_delimiter:
                if mask_strings:
                    blank(index, index + 1)
                index += 1
                string_delimiter = None
                continue
            if mask_strings:
                blank(index, index + 1)
            index += 1
            continue

        block = next((value for value in block_comments if text.startswith(value[0], index)), None)
        if block is not None:
            blank(index, index + len(block[0]))
            index += len(block[0])
            block_end = block[1]
            continue
        line_prefix = next((value for value in line_prefixes if text.startswith(value, index)), None)
        # In fixed-format COBOL, `*` is a comment marker only in the
        # indicator column.  Treating every asterisk as a line comment would
        # hide multiplication and string contents from the fallback parser.
        if line_prefix == "*":
            line_start = text.rfind("\n", 0, index) + 1
            prefix_text = text[line_start:index]
            fixed_format_indicator = len(prefix_text) == 6 and all(
                character.isdigit() or character == " " for character in prefix_text
            )
            if prefix_text.strip() and not fixed_format_indicator:
                line_prefix = None
        if line_prefix is not None:
            line_end = text.find("\n", index)
            if line_end < 0:
                line_end = len(text)
            blank(index, line_end)
            index = line_end
            continue
        if text[index] in string_delimiters:
            string_delimiter = text[index]
            if mask_strings:
                blank(index, index + 1)
            index += 1
            continue
        index += 1
    return "".join(chars)


def _is_comment_line(value: str, prefixes: tuple[str, ...]) -> bool:
    stripped = value.lstrip()
    return any(stripped.startswith(prefix) for prefix in prefixes)


def _clean_name(value: str) -> str:
    return value.strip().strip("'\"`[](){};:,").strip()


def _clean_objective_c_name(value: str) -> str:
    value = value.strip().strip("'\"`[](){};,").strip()
    selectors = re.findall(r"([A-Za-z_]\w*):", value)
    return "".join(f"{selector}:" for selector in selectors) if selectors else value


def _clean_reference(value: str) -> str:
    return _clean_name(value).replace("\\", "/")


def _first_line(value: str, limit: int = 240) -> str:
    return (value.splitlines()[0].strip() if value.splitlines() else value.strip())[:limit]


def _line_col(text: str, offset: int) -> tuple[int, int]:
    line = text.count("\n", 0, offset) + 1
    last_newline = text.rfind("\n", 0, offset)
    line_start = 0 if last_newline < 0 else last_newline + 1
    # Contract v1 columns are UTF-8 byte offsets, matching Tree-sitter and
    # Python's AST col_offset semantics.  Regex offsets are character based,
    # so convert only the portion inside the current line.
    return line, len(text[line_start:offset].encode("utf-8"))


def _span(text: str, start: int, end: int) -> dict[str, int]:
    start_line, start_col = _line_col(text, start)
    end_line, end_col = _line_col(text, end)
    return {
        "start_line": start_line,
        "start_col": start_col,
        "end_line": end_line,
        "end_col": end_col,
    }


def _next_top_level_boundary(text: str, after: int) -> int:
    """Find the next unindented statement for indentation-based languages."""

    cursor = text.find("\n", after)
    cursor = len(text) if cursor < 0 else cursor + 1
    while cursor < len(text):
        line_end = text.find("\n", cursor)
        if line_end < 0:
            line_end = len(text)
        line = text[cursor:line_end]
        if line.strip() and not line[:1].isspace():
            return cursor
        cursor = line_end + 1
    return len(text)


def _scope_end(text: str, after: int, profile: LanguageProfile, kind: str) -> int:
    if profile.grammar != "objc":
        brace = text.find("{", after, min(len(text), after + 400))
        if brace >= 0:
            depth = 0
            for index in range(brace, len(text)):
                if text[index] == "{":
                    depth += 1
                elif text[index] == "}":
                    depth -= 1
                    if depth == 0:
                        return index + 1
    if profile.grammar == "pascal" and kind == "namespace":
        match = re.search(r"^\s*end\.\s*$", text[after:], re.IGNORECASE | re.MULTILINE)
        if match:
            return after + match.start()
    if profile.end_keywords and profile.grammar not in {"julia", "matlab"}:
        line_start = text.rfind("\n", 0, after) + 1
        header = text[line_start:after]
        keywords = profile.end_keywords
        if profile.grammar == "fortran":
            if re.search(r"\bmodule\b", header, re.IGNORECASE):
                keywords = ("end module",)
            elif re.search(r"\btype\b", header, re.IGNORECASE):
                keywords = ("end type",)
        elif profile.grammar == "vbnet":
            if re.search(r"\bnamespace\b", header, re.IGNORECASE):
                keywords = ("End Namespace",)
            elif re.search(r"\bclass\b", header, re.IGNORECASE):
                keywords = ("End Class",)
            elif re.search(r"\bmodule\b", header, re.IGNORECASE):
                keywords = ("End Module",)
            elif re.search(r"\bstructure\b", header, re.IGNORECASE):
                keywords = ("End Structure",)
            elif re.search(r"\binterface\b", header, re.IGNORECASE):
                keywords = ("End Interface",)
        keyword_re = r"^\s*(?:" + "|".join(re.escape(value) for value in keywords) + r")(?:\b|(?=\s|[.;]))"
        match = re.search(keyword_re, text[after:], _flags(profile))
        if match:
            return after + match.start()
    if profile.grammar in {"julia", "matlab"}:
        depth = 1
        block_re = re.compile(r"^\s*(?:module|function|struct|mutable\s+struct|if|for|while|let|begin|try|macro|classdef)\b|^\s*end\b", re.IGNORECASE | re.MULTILINE)
        for match in block_re.finditer(text, after):
            line = match.group(0).strip().lower()
            if line.startswith("end"):
                depth -= 1
                if depth == 0:
                    return match.start()
            else:
                depth += 1
    return len(text)


def _nearest_container(declaration: Declaration, containers: list[Declaration]) -> Declaration | None:
    candidates = [
        value
        for value in containers
        if value.start < declaration.start and (value.end_scope is None or value.end_scope > declaration.start)
    ]
    return max(candidates, key=lambda value: value.start) if candidates else None


def _scope_for_offset(
    declarations: list[Declaration],
    offset: int,
    owner_name: str,
    language: str,
) -> Declaration | None:
    if owner_name:
        wanted = _symbol_key(language, owner_name)
        named = [value for value in declarations if _symbol_key(language, value.name) == wanted]
        if named:
            return min(named, key=lambda value: abs(value.start - offset))
    candidates = [
        value
        for value in declarations
        if value.node_id
        and value.start <= offset
        and (value.end_scope is None or value.end_scope > offset)
    ]
    return max(candidates, key=lambda value: value.start) if candidates else None


def _symbol_key(language: str, name: str) -> str:
    return name.casefold() if language in {"vbnet", "vba", "fortran", "pascal", "cobol"} else name


def _resolve_symbol(
    symbols: dict[tuple[str, str], list[Declaration]],
    language: str,
    name: str,
) -> Declaration | None:
    candidates = symbols.get((language, _symbol_key(language, name)), [])
    if len(candidates) == 1:
        return candidates[0]
    return None


def _resolve_file(
    item: ExtendedFile,
    reference: str,
    files: dict[str, dict[str, ExtendedFile]],
) -> ExtendedFile | None:
    reference = reference.split("#", 1)[0].split("?", 1)[0]
    if not reference or "://" in reference:
        return None
    clean = reference.replace("\\", "/")
    is_relative = clean.startswith("./") or clean.startswith("../")
    if is_relative:
        candidate = posixpath.normpath(posixpath.join(posixpath.dirname(item.relative_path), clean))
    else:
        candidate = posixpath.normpath(clean.lstrip("/"))
    if candidate == ".." or candidate.startswith("../"):
        return None

    candidates = [candidate]
    if not is_relative and "/" not in candidate and "." in candidate:
        # Keep the literal filename candidate (for example Foo.h) and add a
        # dotted-module spelling (for example Data.List) separately.
        candidates.append(candidate.replace(".", "/"))
    language_files = files.get(item.language, {})
    extensions = sorted({Path(path).suffix for path in language_files if Path(path).suffix})
    # Resolve against extensions actually present in the selected language.
    # This covers TS-style `service.js` -> `service.ts` and module names such
    # as Haskell's `Data.List` -> `Data/List.hs` without a fixed extension list.
    for base in list(candidates):
        suffix = Path(base).suffix.lower()
        if suffix:
            stem = str(Path(base).with_suffix(""))
            candidates.extend(stem + extension for extension in extensions)
        else:
            candidates.extend(base + extension for extension in extensions)
            candidates.extend(posixpath.join(base, "index" + extension) for extension in extensions)
    for candidate_path in candidates:
        target = language_files.get(candidate_path)
        if target is not None:
            return target
    return None


def _return_behavior(body: str) -> str:
    returns = re.findall(r"\breturn\b([^\n;}]*)", body, re.IGNORECASE)
    if not returns:
        return "no_explicit_return"
    has_value = any(value.strip() for value in returns)
    has_none = any(not value.strip() for value in returns)
    if has_value and has_none:
        return "mixed"
    return "returns_value" if has_value else "returns_none"
