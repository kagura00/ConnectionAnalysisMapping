from __future__ import annotations

import json
from pathlib import Path

import pytest

from connection_map import cli as cli_module
from connection_map.analyzer import analyze_repository
from connection_map.bundle import BundleError
from connection_map.cli import main
from connection_map.config import AnalysisConfig
from connection_map.contract import canonical_sha256
from connection_map.workspace import Workspace, WorkspaceError


def test_central_analyze_registers_repositories_and_keeps_local_mode(tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / "data"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "main.py").write_text("def first():\n    return 1\n", encoding="utf-8")
    (second / "main.py").write_text("def second():\n    return 2\n", encoding="utf-8")
    monkeypatch.setenv("CONNECTION_MAP_WORKSPACE", str(workspace_root))

    assert main(["analyze", "--root", str(first), "--deterministic"]) == 0
    assert main(["analyze", "--root", str(second), "--deterministic"]) == 0

    workspace = Workspace(workspace_root)
    records = workspace.records()
    assert len(records) == 2
    assert {record.display_name for record in records} == {"first", "second"}
    for record in records:
        assert workspace.path_for(record, record.analysis_path).is_file()
        assert (workspace.path_for(record, record.bundle_path) / "index.json").is_file()
        manifest = json.loads((workspace.path_for(record, record.data_path) / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["repository_id"] == record.repository_id

    local_output = tmp_path / "local-analysis.json"
    monkeypatch.delenv("CONNECTION_MAP_WORKSPACE")
    assert main(["analyze", "--root", first.as_posix(), "--output", str(local_output), "--deterministic"]) == 0
    assert local_output.is_file()


def test_central_cli_override_is_replayed_by_saved_configuration(tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / "data"
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (repository / "test_main.py").write_text("def test_main():\n    return 1\n", encoding="utf-8")
    monkeypatch.setenv("CONNECTION_MAP_WORKSPACE", str(workspace_root))

    assert main(["analyze", "--root", str(repository), "--include-tests", "--deterministic"]) == 0

    record = Workspace(workspace_root).records()[0]
    saved = AnalysisConfig.from_toml(Workspace(workspace_root).path_for(record, record.config_path))
    assert saved.include_tests is True


def test_invalid_configuration_does_not_register_new_repository(tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / "data"
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    invalid_config = tmp_path / "invalid.toml"
    invalid_config.write_text("[analysis\n", encoding="utf-8")
    monkeypatch.setenv("CONNECTION_MAP_WORKSPACE", str(workspace_root))

    assert main(["analyze", "--root", str(repository), "--config", str(invalid_config)]) == 2
    assert Workspace(workspace_root).records() == []


def test_failed_analysis_removes_new_central_repository_record(tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / "data"
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    monkeypatch.setenv("CONNECTION_MAP_WORKSPACE", str(workspace_root))

    def fail_analysis(*args, **kwargs):
        raise ValueError("simulated analysis failure")

    monkeypatch.setattr(cli_module, "analyze_repository", fail_analysis)

    assert main(["analyze", "--root", str(repository)]) == 2
    workspace = Workspace(workspace_root)
    assert workspace.records() == []
    assert not (workspace_root / "repositories").exists() or not list((workspace_root / "repositories").iterdir())


def test_repository_move_reuses_record_when_git_origin_is_unambiguous(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "data")
    original = tmp_path / "original"
    original.mkdir()
    (original / ".git").mkdir()
    (original / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://example.test/project.git\n',
        encoding="utf-8",
    )

    record = workspace.register(original)
    moved = tmp_path / "moved"
    original.rename(moved)

    assert workspace.find(moved).repository_id == record.repository_id
    rebound = workspace.register(moved)
    assert rebound.repository_id == record.repository_id
    assert rebound.absolute_path == str(moved.resolve())
    assert workspace.records()[0].normalized_path == rebound.normalized_path


def test_reused_path_with_a_different_git_origin_creates_a_new_record(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "data")
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    git_config = repository / ".git" / "config"
    git_config.write_text('[remote "origin"]\n\turl = https://example.test/first.git\n', encoding="utf-8")

    first = workspace.register(repository)
    git_config.write_text('[remote "origin"]\n\turl = https://example.test/second.git\n', encoding="utf-8")

    second = workspace.register(repository)

    assert second.repository_id != first.repository_id
    assert len(workspace.records()) == 2


def test_register_preparation_failure_does_not_publish_a_record(tmp_path: Path, monkeypatch) -> None:
    workspace = Workspace(tmp_path / "data")
    repository = tmp_path / "repository"
    repository.mkdir()

    def fail_preparation(record):
        raise WorkspaceError("simulated data path failure")

    monkeypatch.setattr(workspace, "ensure_data_paths", fail_preparation)

    with pytest.raises(WorkspaceError, match="simulated data path failure"):
        workspace.register(repository)

    assert workspace.records() == []


def test_validation_status_update_keeps_active_repository(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "data")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    first_record = workspace.register(first)
    second_record = workspace.register(second)

    workspace.set_validation(first_record.repository_id, "valid")

    state = workspace.load()
    assert state["active_repository_id"] == second_record.repository_id
    assert workspace.get(first_record.repository_id).validation_status == "valid"


def test_workspace_migrates_legacy_registry_without_touching_repository_data(tmp_path: Path) -> None:
    workspace_root = tmp_path / "data"
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    workspace = Workspace(workspace_root)
    assert main(["analyze", "--root", str(repository), "--workspace", str(workspace_root), "--deterministic"]) == 0

    record = workspace.records()[0]
    analysis_path = workspace.path_for(record, record.analysis_path)
    analysis_before = analysis_path.read_bytes()
    legacy = workspace.load()
    legacy["schema_version"] = "1.0"
    legacy_record = legacy["repositories"][0].to_dict()
    for field in ("layout_path", "validation_status", "last_analysis_sha256", "git_remote", "config_sha256"):
        legacy_record.pop(field, None)
    legacy["repositories"] = [legacy_record]
    workspace.registry_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    migrated = Workspace(workspace_root)
    migrated_record = migrated.records()[0]

    assert migrated.load()["schema_version"] == "1.1"
    assert migrated_record.layout_path == f"{record.data_path}/layout.json"
    assert migrated_record.validation_status == "pending"
    assert analysis_path.read_bytes() == analysis_before
    assert list((workspace_root / "backups").glob("registry-1.0-to-1.1-*.json"))


def test_workspace_migration_relocates_legacy_repository_data(tmp_path: Path) -> None:
    workspace_root = tmp_path / "data"
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    workspace = Workspace(workspace_root)
    assert main(["analyze", "--root", str(repository), "--workspace", str(workspace_root), "--deterministic"]) == 0

    record = workspace.records()[0]
    canonical_data = workspace.path_for(record, record.data_path)
    analysis_before = workspace.path_for(record, record.analysis_path).read_bytes()
    legacy_data = workspace_root / "repositories" / "legacy-storage"
    canonical_data.rename(legacy_data)
    legacy = workspace.load()
    legacy["schema_version"] = "1.0"
    legacy_record = legacy["repositories"][0].to_dict()
    legacy_record["data_path"] = "repositories/legacy-storage"
    legacy_record["analysis_path"] = "repositories/legacy-storage/analysis.json"
    legacy_record["bundle_path"] = "repositories/legacy-storage/bundle"
    legacy_record["config_path"] = "repositories/legacy-storage/config.toml"
    legacy_record["layout_path"] = "repositories/legacy-storage/layout.json"
    for field in ("layout_path", "validation_status", "last_analysis_sha256", "git_remote", "config_sha256"):
        legacy_record.pop(field, None)
    legacy["repositories"] = [legacy_record]
    workspace.registry_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    migrated = Workspace(workspace_root)
    migrated_record = migrated.records()[0]
    assert migrated_record.data_path == f"repositories/{record.repository_id}"
    assert migrated.path_for(migrated_record, migrated_record.analysis_path).read_bytes() == analysis_before
    assert not legacy_data.exists()


def test_workspace_migration_rolls_back_prior_moves_when_a_later_move_conflicts(tmp_path: Path) -> None:
    workspace_root = tmp_path / "data"
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    workspace = Workspace(workspace_root)
    first = workspace.register(first_root)
    second = workspace.register(second_root)

    first_legacy = workspace_root / "repositories" / "legacy-first"
    second_legacy = workspace_root / "repositories" / "legacy-second"
    workspace.path_for(first, first.data_path).rename(first_legacy)
    workspace.path_for(second, second.data_path).rename(second_legacy)
    # Leave the second canonical destination occupied so the first move must
    # be restored when the second move cannot be completed.
    second_canonical = workspace_root / "repositories" / second.repository_id
    second_canonical.mkdir()

    legacy = workspace.load()
    legacy["schema_version"] = "1.0"
    legacy_records = []
    for record, legacy_name in zip(legacy["repositories"], ("legacy-first", "legacy-second"), strict=True):
        entry = record.to_dict()
        legacy_data = f"repositories/{legacy_name}"
        entry.update(
            {
                "data_path": legacy_data,
                "analysis_path": f"{legacy_data}/analysis.json",
                "bundle_path": f"{legacy_data}/bundle",
                "config_path": f"{legacy_data}/config.toml",
                "layout_path": f"{legacy_data}/layout.json",
            }
        )
        for field in ("layout_path", "validation_status", "last_analysis_sha256", "git_remote", "config_sha256"):
            if field == "layout_path":
                continue
            entry.pop(field, None)
        legacy_records.append(entry)
    legacy["repositories"] = legacy_records
    workspace.registry_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(WorkspaceError, match="both"):
        Workspace(workspace_root).records()

    assert first_legacy.exists()
    assert second_legacy.exists()
    assert not (workspace_root / "repositories" / first.repository_id).exists()


def test_context_table_is_validated_and_serialized() -> None:
    config = AnalysisConfig(
        language="mixed",
        languages=["cpp", "java", "go", "rust"],
        context={
            "compile_commands": "build/compile_commands.json",
            "classpath": ["build/classes"],
            "go_tags": ["integration"],
            "rust_features": ["serde"],
            "rust_all_cfg": True,
        },
    )
    config.validate()
    assert config.to_dict()["context"]["compile_commands"] == "build/compile_commands.json"


def test_registry_artifacts_must_stay_inside_repository_data_path(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "data")
    root = tmp_path / "repository"
    root.mkdir()
    workspace.register(root)
    registry = workspace.load()
    entry = registry["repositories"][0].to_dict()
    entry["analysis_path"] = "secret.json"
    (workspace.registry_path).write_text(
        json.dumps({**registry, "repositories": [entry]}, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError, match="analysis_path must be inside data_path"):
        workspace.records()


def test_registry_data_path_is_derived_from_repository_id(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "data")
    root = tmp_path / "repository"
    root.mkdir()
    workspace.register(root)
    registry = workspace.load()
    entry = registry["repositories"][0].to_dict()
    entry["data_path"] = "."
    (workspace.registry_path).write_text(
        json.dumps({**registry, "repositories": [entry]}, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError, match="data_path must be repositories"):
        workspace.records()


def test_workspace_rejects_symlinked_repository_storage(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "data")
    root = tmp_path / "repository"
    root.mkdir()
    record = workspace.register(root)
    data_path = workspace.path_for(record, record.data_path)
    redirected = tmp_path / "redirected-data"
    data_path.rename(redirected)
    try:
        data_path.symlink_to(redirected, target_is_directory=True)
    except OSError as exc:
        redirected.rename(data_path)
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(WorkspaceError, match="symlink or junction"):
        workspace.path_for(record, record.analysis_path)


def test_publish_failure_keeps_previous_snapshot(tmp_path: Path) -> None:
    workspace_root = tmp_path / "data"
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    workspace = Workspace(workspace_root)
    assert main(["analyze", "--root", str(repository), "--workspace", str(workspace_root), "--deterministic"]) == 0

    record = workspace.records()[0]
    analysis_path = workspace.path_for(record, record.analysis_path)
    bundle_index = workspace.path_for(record, record.bundle_path) / "index.json"
    previous_analysis = analysis_path.read_bytes()
    previous_index = bundle_index.read_bytes()

    with pytest.raises(BundleError):
        workspace.publish_analysis(record, {"format": "not-a-graph"})

    assert analysis_path.read_bytes() == previous_analysis
    assert bundle_index.read_bytes() == previous_index
    assert not list(workspace.path_for(record, record.data_path).glob(".staging-*"))


def test_publish_manifest_failure_restores_snapshot_and_registry(tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / "data"
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    workspace = Workspace(workspace_root)
    assert main(["analyze", "--root", str(repository), "--workspace", str(workspace_root), "--deterministic"]) == 0

    record = workspace.records()[0]
    analysis_path = workspace.path_for(record, record.analysis_path)
    manifest_path = workspace.path_for(record, Path(record.data_path, "manifest.json").as_posix())
    registry_before = workspace.registry_path.read_bytes()
    analysis_before = analysis_path.read_bytes()
    manifest_before = manifest_path.read_bytes()
    document = json.loads(analysis_before)

    def fail_manifest(*args, **kwargs):
        raise OSError("simulated manifest failure")

    monkeypatch.setattr(workspace, "write_manifest", fail_manifest)
    with pytest.raises(OSError, match="simulated manifest failure"):
        workspace.publish_analysis(record, document)

    assert workspace.registry_path.read_bytes() == registry_before
    assert analysis_path.read_bytes() == analysis_before
    assert manifest_path.read_bytes() == manifest_before


def test_publish_manifest_failure_removes_new_snapshot(tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / "data"
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    workspace = Workspace(workspace_root)
    record = workspace.register(repository)
    document = analyze_repository(repository, AnalysisConfig(), deterministic=True)
    registry_before = workspace.registry_path.read_bytes()

    def fail_manifest(*args, **kwargs):
        raise OSError("simulated manifest failure")

    monkeypatch.setattr(workspace, "write_manifest", fail_manifest)
    with pytest.raises(OSError, match="simulated manifest failure"):
        workspace.publish_analysis(record, document)

    assert workspace.registry_path.read_bytes() == registry_before
    assert not workspace.path_for(record, record.analysis_path).exists()
    assert not (workspace.path_for(record, record.bundle_path) / "index.json").exists()


def test_publish_cleanup_failure_keeps_committed_registry_and_snapshot(tmp_path: Path, monkeypatch) -> None:
    workspace = Workspace(tmp_path / "data")
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    record = workspace.register(repository)
    document = analyze_repository(repository, AnalysisConfig.from_toml(None), deterministic=True)

    def fail_cleanup(state):
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr(workspace, "_finalize_publish_state", fail_cleanup)

    published = workspace.publish_analysis(record, document)

    assert published.last_analysis_sha256 == canonical_sha256(document)
    assert workspace.get(record.repository_id).last_analysis_sha256 == canonical_sha256(document)
    assert workspace.path_for(record, record.analysis_path).is_file()


def test_publish_journal_recovers_after_process_interrupt(tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / "data"
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "main.py"
    source.write_text("def main():\n    return 1\n", encoding="utf-8")
    workspace = Workspace(workspace_root)
    assert main(["analyze", "--root", str(repository), "--workspace", str(workspace_root), "--deterministic"]) == 0

    record = workspace.records()[0]
    analysis_path = workspace.path_for(record, record.analysis_path)
    previous_analysis = analysis_path.read_bytes()
    document = json.loads(previous_analysis)

    def interrupt_publish(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(workspace, "_update_publish_phase", interrupt_publish)
    with pytest.raises(KeyboardInterrupt):
        workspace.publish_analysis(record, document)

    recovered_workspace = Workspace(workspace_root)
    assert recovered_workspace.records()[0].repository_id == record.repository_id
    assert analysis_path.read_bytes() == previous_analysis
    assert not list(workspace_root.glob(".connection-map-publish-*.json"))
