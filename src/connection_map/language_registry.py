"""Built-in language and preset registration.

The registry is the single source of truth for source suffixes and default
patterns. Adding a language should add a specification here and a matching
analyzer adapter, rather than spreading suffix checks through the CLI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LanguageSpec:
    """Describe one concrete source language or a configuration preset."""

    name: str
    extensions: tuple[str, ...]
    default_include: tuple[str, ...]
    analyzer: str
    preset_languages: tuple[str, ...] = ()

    @property
    def is_preset(self) -> bool:
        return bool(self.preset_languages)


LANGUAGE_SPECS: dict[str, LanguageSpec] = {
    "python": LanguageSpec(
        name="python",
        extensions=(".py",),
        default_include=("**/*.py",),
        analyzer="python",
    ),
    "html": LanguageSpec(
        name="html",
        extensions=(".html", ".htm", ".xhtml"),
        default_include=("**/*.html", "**/*.htm", "**/*.xhtml"),
        analyzer="web",
    ),
    "css": LanguageSpec(
        name="css",
        extensions=(".css",),
        default_include=("**/*.css",),
        analyzer="web",
    ),
    "javascript": LanguageSpec(
        name="javascript",
        extensions=(".js", ".jsx", ".mjs", ".cjs"),
        default_include=("**/*.js", "**/*.jsx", "**/*.mjs", "**/*.cjs"),
        analyzer="web",
    ),
    "typescript": LanguageSpec(
        name="typescript",
        extensions=(".ts", ".mts", ".cts", ".tsx"),
        default_include=("**/*.ts", "**/*.mts", "**/*.cts", "**/*.tsx"),
        analyzer="web",
    ),
    "vbnet": LanguageSpec(
        name="vbnet",
        extensions=(".vb", ".vbhtml"),
        default_include=("**/*.vb", "**/*.vbhtml"),
        analyzer="extended",
    ),
    "vba": LanguageSpec(
        name="vba",
        extensions=(".bas", ".cls", ".frm", ".vba"),
        default_include=("**/*.bas", "**/*.cls", "**/*.frm", "**/*.vba"),
        analyzer="extended",
    ),
    "lua": LanguageSpec(
        name="lua",
        extensions=(".lua",),
        default_include=("**/*.lua",),
        analyzer="extended",
    ),
    "haskell": LanguageSpec(
        name="haskell",
        extensions=(".hs", ".lhs"),
        default_include=("**/*.hs", "**/*.lhs"),
        analyzer="extended",
    ),
    "perl": LanguageSpec(
        name="perl",
        extensions=(".pl", ".pm", ".t"),
        default_include=("**/*.pl", "**/*.pm", "**/*.t"),
        analyzer="extended",
    ),
    "matlab": LanguageSpec(
        name="matlab",
        extensions=(".m", ".mlx"),
        default_include=("**/*.m", "**/*.mlx"),
        analyzer="extended",
    ),
    "cobol": LanguageSpec(
        name="cobol",
        extensions=(".cob", ".cbl", ".cpy"),
        default_include=("**/*.cob", "**/*.cbl", "**/*.cpy"),
        analyzer="extended",
    ),
    "fortran": LanguageSpec(
        name="fortran",
        extensions=(".f", ".for", ".f77", ".f90", ".f95", ".f03", ".f08"),
        default_include=(
            "**/*.f",
            "**/*.for",
            "**/*.f77",
            "**/*.f90",
            "**/*.f95",
            "**/*.f03",
            "**/*.f08",
        ),
        analyzer="extended",
    ),
    "r": LanguageSpec(
        name="r",
        extensions=(".r", ".rmd"),
        default_include=("**/*.r", "**/*.rmd"),
        analyzer="extended",
    ),
    "objective-c": LanguageSpec(
        name="objective-c",
        extensions=(".m", ".mm", ".h"),
        default_include=("**/*.m", "**/*.mm", "**/*.h"),
        analyzer="extended",
    ),
    "cuda": LanguageSpec(
        name="cuda",
        extensions=(".cu", ".cuh"),
        default_include=("**/*.cu", "**/*.cuh"),
        analyzer="extended",
    ),
    "groovy": LanguageSpec(
        name="groovy",
        extensions=(".groovy", ".gradle", ".gvy", ".gy", ".gsh"),
        default_include=("**/*.groovy", "**/*.gradle", "**/*.gvy", "**/*.gy", "**/*.gsh"),
        analyzer="extended",
    ),
    "fsharp": LanguageSpec(
        name="fsharp",
        extensions=(".fs", ".fsi", ".fsx", ".fsscript"),
        default_include=("**/*.fs", "**/*.fsi", "**/*.fsx", "**/*.fsscript"),
        analyzer="extended",
    ),
    "assembly": LanguageSpec(
        name="assembly",
        extensions=(".asm", ".s"),
        default_include=("**/*.asm", "**/*.s"),
        analyzer="extended",
    ),
    "hcl": LanguageSpec(
        name="hcl",
        extensions=(".hcl", ".tf", ".tfvars"),
        default_include=("**/*.hcl", "**/*.tf", "**/*.tfvars"),
        analyzer="extended",
    ),
    "gdscript": LanguageSpec(
        name="gdscript",
        extensions=(".gd",),
        default_include=("**/*.gd",),
        analyzer="extended",
    ),
    "elixir": LanguageSpec(
        name="elixir",
        extensions=(".ex", ".exs"),
        default_include=("**/*.ex", "**/*.exs"),
        analyzer="extended",
    ),
    "zig": LanguageSpec(
        name="zig",
        extensions=(".zig",),
        default_include=("**/*.zig",),
        analyzer="extended",
    ),
    "julia": LanguageSpec(
        name="julia",
        extensions=(".jl",),
        default_include=("**/*.jl",),
        analyzer="extended",
    ),
    "pascal": LanguageSpec(
        name="pascal",
        extensions=(".pas", ".pp", ".p"),
        default_include=("**/*.pas", "**/*.pp", "**/*.p"),
        analyzer="extended",
    ),
    "erlang": LanguageSpec(
        name="erlang",
        extensions=(".erl", ".hrl", ".escript"),
        default_include=("**/*.erl", "**/*.hrl", "**/*.escript"),
        analyzer="extended",
    ),
    "c": LanguageSpec(
        name="c",
        extensions=(".c", ".h", ".inc"),
        default_include=("**/*.c", "**/*.h", "**/*.inc"),
        analyzer="c_family",
    ),
    "cpp": LanguageSpec(
        name="cpp",
        extensions=(
            ".cc",
            ".cpp",
            ".cxx",
            ".c++",
            ".hh",
            ".hpp",
            ".hxx",
            ".h++",
            ".ipp",
            ".inl",
            ".h",
            ".inc",
        ),
        default_include=(
            "**/*.cc",
            "**/*.cpp",
            "**/*.cxx",
            "**/*.c++",
            "**/*.hh",
            "**/*.hpp",
            "**/*.hxx",
            "**/*.h++",
            "**/*.ipp",
            "**/*.inl",
            "**/*.h",
            "**/*.inc",
        ),
        analyzer="c_family",
    ),
    "java": LanguageSpec(
        name="java",
        extensions=(".java",),
        default_include=("**/*.java",),
        analyzer="java",
    ),
    "csharp": LanguageSpec(
        name="csharp",
        extensions=(".cs",),
        default_include=("**/*.cs",),
        analyzer="csharp",
    ),
    "go": LanguageSpec(
        name="go",
        extensions=(".go",),
        default_include=("**/*.go",),
        analyzer="go",
    ),
    "rust": LanguageSpec(
        name="rust",
        extensions=(".rs",),
        default_include=("**/*.rs",),
        analyzer="rust",
    ),
    "php": LanguageSpec(
        name="php",
        extensions=(".php", ".phtml"),
        default_include=("**/*.php", "**/*.phtml"),
        analyzer="php",
    ),
    "ruby": LanguageSpec(
        name="ruby",
        extensions=(".rb", ".rake", ".gemspec", ".ru"),
        default_include=("**/*.rb", "**/*.rake", "**/*.gemspec", "**/*.ru"),
        analyzer="ruby",
    ),
    "kotlin": LanguageSpec(
        name="kotlin",
        extensions=(".kt", ".kts"),
        default_include=("**/*.kt", "**/*.kts"),
        analyzer="kotlin",
    ),
    "swift": LanguageSpec(
        name="swift",
        extensions=(".swift",),
        default_include=("**/*.swift",),
        analyzer="swift",
    ),
    "bash": LanguageSpec(
        name="bash",
        extensions=(".sh", ".bash", ".bats", ".command"),
        default_include=("**/*.sh", "**/*.bash", "**/*.bats", "**/*.command"),
        analyzer="bash",
    ),
    "posix-shell": LanguageSpec(
        name="posix-shell",
        extensions=(".sh", ".command"),
        default_include=("**/*.sh", "**/*.command"),
        analyzer="shell",
    ),
    "powershell": LanguageSpec(
        name="powershell",
        extensions=(".ps1", ".psm1", ".psd1"),
        default_include=("**/*.ps1", "**/*.psm1", "**/*.psd1"),
        analyzer="powershell",
    ),
    "dart": LanguageSpec(
        name="dart",
        extensions=(".dart",),
        default_include=("**/*.dart",),
        analyzer="dart",
    ),
    "scala": LanguageSpec(
        name="scala",
        extensions=(".scala", ".sc"),
        default_include=("**/*.scala", "**/*.sc"),
        analyzer="scala",
    ),
    "mysql": LanguageSpec(
        name="mysql",
        extensions=(".sql", ".mysql.sql"),
        default_include=("**/*.sql", "**/*.mysql.sql"),
        analyzer="mysql",
    ),
    "postgresql": LanguageSpec(
        name="postgresql",
        extensions=(".sql", ".pgsql", ".psql", ".postgres.sql"),
        default_include=("**/*.sql", "**/*.pgsql", "**/*.psql", "**/*.postgres.sql"),
        analyzer="postgresql",
    ),
    "sqlite": LanguageSpec(
        name="sqlite",
        extensions=(".sql", ".sqlite.sql"),
        default_include=("**/*.sql", "**/*.sqlite.sql"),
        analyzer="sqlite",
    ),
    "sqlserver": LanguageSpec(
        name="sqlserver",
        extensions=(".sql", ".tsql", ".sqlserver.sql"),
        default_include=("**/*.sql", "**/*.tsql", "**/*.sqlserver.sql"),
        analyzer="sqlserver",
    ),
    "oracle": LanguageSpec(
        name="oracle",
        extensions=(".sql", ".oracle.sql"),
        default_include=("**/*.sql", "**/*.oracle.sql"),
        analyzer="oracle",
    ),
    "shell": LanguageSpec(
        name="shell",
        extensions=(),
        default_include=(),
        analyzer="shell",
        preset_languages=("bash", "posix-shell"),
    ),
    "sql": LanguageSpec(
        name="sql",
        extensions=(),
        default_include=(),
        analyzer="sql",
        preset_languages=("mysql", "postgresql", "sqlite", "sqlserver", "oracle"),
    ),
    "c-family": LanguageSpec(
        name="c-family",
        extensions=(),
        default_include=(),
        analyzer="c_family",
        preset_languages=("c", "cpp"),
    ),
    "web": LanguageSpec(
        name="web",
        extensions=(),
        default_include=(),
        analyzer="web",
        preset_languages=("html", "css", "javascript", "typescript"),
    ),
    "mixed": LanguageSpec(
        name="mixed",
        extensions=(),
        default_include=(),
        analyzer="mixed",
        preset_languages=("python", "html", "css", "javascript", "typescript"),
    ),
}

# Keep the whole-repository preset derived from the concrete registrations so
# adding a language does not require a second list to be updated.
LANGUAGE_SPECS["all"] = LanguageSpec(
    name="all",
    extensions=(),
    default_include=(),
    analyzer="mixed",
    preset_languages=tuple(name for name, spec in LANGUAGE_SPECS.items() if not spec.is_preset),
)


def supported_languages() -> tuple[str, ...]:
    return tuple(LANGUAGE_SPECS)


def get_language_spec(language: str) -> LanguageSpec:
    try:
        return LANGUAGE_SPECS[language]
    except KeyError as exc:
        choices = ", ".join(supported_languages())
        raise ValueError(f"language must be one of: {choices}") from exc


def concrete_languages(language: str, languages: list[str] | None = None) -> tuple[str, ...]:
    """Resolve a config language/preset into concrete source languages."""

    spec = get_language_spec(language)
    selected = tuple(languages or spec.preset_languages or (language,))
    if len(set(selected)) != len(selected):
        raise ValueError("languages must not contain duplicates")
    for selected_language in selected:
        selected_spec = get_language_spec(selected_language)
        if selected_spec.is_preset:
            raise ValueError(f"languages must contain concrete languages, not preset {selected_language!r}")
        if spec.is_preset and language != "mixed" and selected_language not in spec.preset_languages:
            allowed = ", ".join(spec.preset_languages)
            raise ValueError(f"language = {language!r} only supports: {allowed}")
    return selected


def default_include_patterns(language: str, languages: list[str] | None = None) -> list[str]:
    patterns: list[str] = []
    selected = concrete_languages(language, languages)
    for selected_language in selected:
        for pattern in LANGUAGE_SPECS[selected_language].default_include:
            if pattern not in patterns:
                patterns.append(pattern)
    return patterns


def language_for_path(
    path: str | Path,
    preferred_languages: set[str] | tuple[str, ...] | list[str] | None = None,
) -> str | None:
    return language_for_path_for_languages(path, preferred_languages)


def language_for_path_for_languages(
    path: str | Path,
    preferred_languages: set[str] | tuple[str, ...] | list[str] | None = None,
) -> str | None:
    """Return the best registered language for a path.

    C and C++ intentionally share common header suffixes. When a selected
    language set is available, prefer C++ for ambiguous headers if it is part
    of that set; the C++ grammar is a practical superset for mixed header
    trees, while an explicit C-only analysis still uses the C grammar.
    """

    path_name = Path(path).name.lower()
    suffix = Path(path).suffix.lower()
    matched_extensions = {
        language: max(
            (len(extension) for extension in spec.extensions if path_name.endswith(extension.lower())),
            default=0,
        )
        for language, spec in LANGUAGE_SPECS.items()
        if not spec.is_preset
    }
    most_specific = max(matched_extensions.values(), default=0)
    candidates = [
        language
        for language, specificity in matched_extensions.items()
        if specificity == most_specific and specificity > 0
    ]
    if not candidates:
        return None
    preferred = set(preferred_languages or ())
    if suffix == ".m" and "matlab" in candidates and "objective-c" in candidates:
        # ``.m`` is shared by two languages; content markers are used only to
        # disambiguate the selected candidates, never to inspect dependencies.
        try:
            sample = Path(path).read_bytes()[:8192].decode("utf-8", errors="replace")
        except OSError:
            sample = ""
        is_objective_c = bool(re.search(r"@(?:interface|implementation)|^\s*#import\b", sample, re.MULTILINE))
        is_matlab = bool(re.search(r"^\s*(?:function|classdef)\b|<-\s*function\b", sample, re.MULTILINE))
        if is_objective_c:
            return "objective-c" if not preferred_languages or "objective-c" in preferred else None
        if is_matlab:
            return "matlab" if not preferred_languages or "matlab" in preferred else None
    if preferred_languages:
        preferred = set(preferred_languages)
        selected = [language for language in candidates if language in preferred]
        if selected:
            if suffix == ".h" and "objective-c" in candidates and "objective-c" in preferred:
                try:
                    sample = Path(path).read_bytes()[:8192].decode("utf-8", errors="replace")
                except OSError:
                    sample = ""
                if re.search(r"@(?:interface|implementation)|^\s*#import\b", sample, re.MULTILINE):
                    return "objective-c"
            if suffix in {".h", ".inc"} and "cpp" in selected:
                return "cpp"
            return selected[0]
    return candidates[0]


def analyzer_for_language(language: str) -> str:
    return get_language_spec(language).analyzer
