from __future__ import annotations

from pathlib import Path

import pytest

from connection_map.config import AnalysisConfig, discover_source_files
from connection_map.scaffold import initialize_target


def test_source_matching_preserves_hidden_directory_names(tmp_path: Path) -> None:
    (tmp_path / "visible.py").write_text("def visible():\n    return 1\n", encoding="utf-8")
    hidden = tmp_path / ".venv"
    hidden.mkdir()
    (hidden / "hidden.py").write_text("def hidden():\n    return 1\n", encoding="utf-8")

    selected, skipped = discover_source_files(tmp_path, AnalysisConfig(), languages={"python"})

    assert [path.relative_to(tmp_path).as_posix() for path in selected] == ["visible.py"]
    assert not any(path == ".venv/hidden.py" for path, _ in skipped)


def test_web_source_matching_is_case_insensitive_and_excludes_common_test_artifacts(tmp_path: Path) -> None:
    (tmp_path / "APP.TS").write_text("export const app = 1;\n", encoding="utf-8")
    (tmp_path / "feature.test.ts").write_text("export const test = 1;\n", encoding="utf-8")
    tests_dir = tmp_path / "__tests__"
    tests_dir.mkdir()
    (tests_dir / "feature.ts").write_text("export const test = 1;\n", encoding="utf-8")
    generated_dir = tmp_path / "generated"
    generated_dir.mkdir()
    (generated_dir / "code.js").write_text("generated();\n", encoding="utf-8")

    selected, skipped = discover_source_files(
        tmp_path,
        AnalysisConfig(language="mixed", languages=["javascript", "typescript"]),
    )

    assert [path.name for path in selected] == ["APP.TS"]
    assert {path for path, _ in skipped} >= {"feature.test.ts", "__tests__/feature.ts"}


def test_scaffold_config_keeps_generated_directories_out_of_analysis(tmp_path: Path) -> None:
    initialize_target(tmp_path)

    config = AnalysisConfig.from_toml(tmp_path / ".connection-map" / "config.toml")

    assert config.should_include("generated/auto.py") == (False, "generated")
    assert config.should_include("vendor/library.py") == (False, "excluded")


def test_custom_scaffold_directory_contains_its_own_generated_data_ignore(tmp_path: Path) -> None:
    initialize_target(tmp_path, "analysis-tool")

    assert (tmp_path / "analysis-tool" / ".gitignore").read_text(encoding="utf-8") == (
        "core/\nbackups/\nsnapshots/\nweb/\n"
    )


def test_max_file_bytes_rejects_boolean_values() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        AnalysisConfig(max_file_bytes=True).validate()


def test_language_specific_excludes_apply_to_custom_exclude_lists() -> None:
    dart = AnalysisConfig(language="dart", exclude=[".git/**"])
    swift = AnalysisConfig(language="swift", exclude=[".git/**"])

    assert dart.matches("lib/generated/native.g.dart", dart.exclude)
    assert swift.matches("Package.swift", swift.exclude)


def test_symlink_files_are_excluded_or_confined_to_root(tmp_path: Path) -> None:
    (tmp_path / "inside.py").write_text("def inside():\n    return 1\n", encoding="utf-8")
    outside = tmp_path.parent / "outside.py"
    outside.write_text("def outside():\n    return 1\n", encoding="utf-8")
    inside_link = tmp_path / "inside-link.py"
    outside_link = tmp_path / "outside-link.py"
    try:
        inside_link.symlink_to(tmp_path / "inside.py")
        outside_link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    config = AnalysisConfig()
    selected, skipped = discover_source_files(tmp_path, config, languages={"python"})
    selected_names = {path.name for path in selected}
    assert "inside-link.py" not in selected_names
    assert any(path == "inside-link.py" and reason == "symlink_excluded" for path, reason in skipped)

    config.follow_symlinks = True
    selected, skipped = discover_source_files(tmp_path, config, languages={"python"})
    selected_names = {path.name for path in selected}
    assert "inside-link.py" in selected_names
    assert "outside-link.py" not in selected_names
    assert any(path == "outside-link.py" and reason == "symlink_outside_root" for path, reason in skipped)


def test_scaffold_rejects_missing_or_symlink_target(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        initialize_target(tmp_path / "missing")

    symlink_target = tmp_path / "target"
    symlink_target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(symlink_target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    with pytest.raises(ValueError, match="symlink"):
        initialize_target(tmp_path, "link")

    with pytest.raises(ValueError, match="child directory"):
        initialize_target(tmp_path, ".")


def test_scaffold_force_does_not_follow_child_symlinks(tmp_path: Path) -> None:
    base = tmp_path / ".connection-map"
    base.mkdir()
    outside = tmp_path.parent / "scaffold-outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    child = base / "config.toml"
    try:
        child.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="symlink"):
        initialize_target(tmp_path, force=True)

    assert outside.read_text(encoding="utf-8") == "keep\n"


def test_sql_and_shell_presets_accept_explicit_languages() -> None:
    AnalysisConfig(language="sql", languages=["postgresql"]).validate()
    AnalysisConfig(language="shell", languages=["posix-shell"]).validate()


def test_toml_unknown_analysis_keys_are_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[analysis]\nlangauge = \"python\"\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported keys: langauge"):
        AnalysisConfig.from_toml(config_path)


@pytest.mark.parametrize("language", ("python", "html", "css", "javascript", "typescript"))
def test_v10_languages_can_be_selected_individually(language: str) -> None:
    config = AnalysisConfig(language=language)

    assert config.active_languages() == (language,)
