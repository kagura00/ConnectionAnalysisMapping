from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from connection_map import cli, investigate_cli, output_profile
from connection_map.analyzer import analyze_repository
from connection_map.cli import main
from connection_map.workspace import WORKSPACE_ENV, Workspace


def _repository(base: Path) -> Path:
    root = base / "repo"
    root.mkdir(parents=True)
    (root / "app.py").write_text("def entry():\n    return 1\n", encoding="utf-8")
    return root


def _inventory(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _counter(monkeypatch):
    calls = {
        name: 0
        for name in (
            "workspace_load",
            "workspace_records",
            "workspace_find",
            "configure",
            "analyze",
            "cache_write",
            "directory_create",
            "file_write",
        )
    }

    def increment(name):
        calls[name] += 1

    monkeypatch.setattr(Workspace, "load", lambda self: increment("workspace_load"))
    monkeypatch.setattr(Workspace, "records", lambda self: increment("workspace_records"))
    monkeypatch.setattr(Workspace, "find", lambda self, root: increment("workspace_find"))
    monkeypatch.setattr(cli, "analyze_repository", lambda *args, **kwargs: increment("analyze"))
    monkeypatch.setattr(investigate_cli, "analyze_repository", lambda *args, **kwargs: increment("analyze"))
    monkeypatch.setattr(investigate_cli, "_atomic_json", lambda *args, **kwargs: increment("cache_write"))
    original_mkdir = Path.mkdir
    original_write_text = Path.write_text

    def tracked_mkdir(self, *args, **kwargs):
        increment("directory_create")
        return original_mkdir(self, *args, **kwargs)

    def tracked_write_text(self, *args, **kwargs):
        increment("file_write")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", tracked_mkdir)
    monkeypatch.setattr(Path, "write_text", tracked_write_text)

    @contextmanager
    def configure(path):
        increment("configure")
        yield True

    monkeypatch.setattr(cli, "configured_grammar_cache", configure)
    monkeypatch.setattr(investigate_cli, "configured_grammar_cache", configure)
    return calls


def test_analyze_profile_cli_paths_override_profile_and_report_to_stderr(tmp_path, monkeypatch, capsys):
    root = _repository(tmp_path)
    before = _inventory(root)
    ambient = tmp_path / "ambient-workspace"
    monkeypatch.setenv(WORKSPACE_ENV, str(ambient))
    profile = tmp_path / "profile"
    workspace = tmp_path / "workspace-cli"
    grammar = tmp_path / "grammar-cli"
    output = tmp_path / "analysis-cli.json"

    assert main(
        [
            "analyze",
            "--root",
            str(root),
            "--external-dir",
            str(profile),
            "--workspace",
            str(workspace),
            "--grammar-cache",
            str(grammar),
            "--output",
            str(output),
        ]
    ) == 0

    captured = capsys.readouterr()
    assert any(node["display_name"] == "entry" for node in json.loads(output.read_text(encoding="utf-8"))["nodes"])
    assert (workspace / "registry.json").is_file()
    assert not (profile / "workspace").exists()
    assert not (profile / "grammar-cache").exists()
    assert not ambient.exists()
    assert "analysis workspace:" in captured.err and "from --workspace" in captured.err
    assert "analysis output:" in captured.err and "from --output" in captured.err
    assert "grammar cache:" in captured.err and "from --grammar-cache" in captured.err
    assert captured.out.startswith("wrote ")
    assert _inventory(root) == before


def test_profile_overrides_ambient_workspace_and_keeps_root_read_only(tmp_path, monkeypatch, capsys):
    root = _repository(tmp_path)
    before = _inventory(root)
    ambient = tmp_path / "ambient-workspace"
    profile = tmp_path / "external-profile"
    monkeypatch.setenv(WORKSPACE_ENV, str(ambient))

    assert main(["analyze", "--root", str(root), "--external-dir", str(profile)]) == 0

    captured = capsys.readouterr()
    assert (profile / "workspace" / "registry.json").is_file()
    assert not ambient.exists()
    assert not (root / ".connection-map").exists()
    assert "analysis workspace:" in captured.err and "from --external-dir" in captured.err
    assert "grammar cache:" in captured.err and "from --external-dir" in captured.err
    assert _inventory(root) == before


def test_workspace_storage_is_reported_at_entry_and_actual_path_before_publish(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(WORKSPACE_ENV, raising=False)
    root = _repository(tmp_path)
    profile = tmp_path / "external"
    workspace = profile / "workspace"
    assert main(["analyze", "--root", str(root), "--external-dir", str(profile)]) == 0
    capsys.readouterr()

    original_load = Workspace.load
    original_publish = Workspace.publish_analysis
    stderr_before_load = []
    stderr_before_publish = []

    def inspect_before_load(instance):
        stderr_before_load.append(capsys.readouterr().err)
        return original_load(instance)

    def inspect_before_publish(instance, record, document):
        stderr_before_publish.append(capsys.readouterr().err)
        return original_publish(instance, record, document)

    monkeypatch.setattr(Workspace, "load", inspect_before_load)
    monkeypatch.setattr(Workspace, "publish_analysis", inspect_before_publish)
    assert main(["analyze", "--root", str(root), "--external-dir", str(profile)]) == 0

    registry = json.loads((workspace / "registry.json").read_text(encoding="utf-8"))
    record = registry["repositories"][0]
    expected = workspace / record["analysis_path"]
    assert str(workspace / "repositories") in stderr_before_load[0]
    assert "repository ID assigned at registration" in stderr_before_load[0]
    assert str(expected) in stderr_before_publish[0]
    assert "workspace analysis output:" in stderr_before_publish[0]
    assert "workspace registered record" in stderr_before_publish[0]


def test_workspace_inside_target_remains_available_without_external_profile(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(WORKSPACE_ENV, raising=False)
    root = _repository(tmp_path)
    source_hash = hashlib.sha256((root / "app.py").read_bytes()).hexdigest()
    workspace = root / "workspace"

    assert main(["analyze", "--root", str(root), "--workspace", str(workspace)]) == 0

    assert (workspace / "registry.json").is_file()
    assert (workspace / "repositories").is_dir()
    assert _inventory(root)["app.py"] == source_hash
    assert "workspace analysis storage:" in capsys.readouterr().err


def test_analyze_without_profile_keeps_local_snapshot_default(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(WORKSPACE_ENV, raising=False)
    root = _repository(tmp_path)

    assert main(["analyze", "--root", str(root)]) == 0

    assert (root / ".connection-map" / "snapshots" / "analysis.json").is_file()
    assert "analysis workspace:" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "command,override",
    [
        ("analyze", ["--workspace", "{root}/workspace"]),
        ("analyze", ["--grammar-cache", "{root}/grammar"]),
        ("analyze", ["--output", "{root}/analysis.json"]),
        ("investigate", ["--cache-dir", "{root}/cache"]),
        ("investigate", ["--grammar-cache", "{root}/grammar"]),
        ("investigate", ["--output", "{root}/packet.json"]),
    ],
)
def test_invalid_profile_overrides_fail_before_any_workspace_parser_analysis_or_cache_effect(
    tmp_path, monkeypatch, capsys, command, override
):
    root = _repository(tmp_path)
    before = _inventory(root)
    profile = tmp_path / "external-profile"
    calls = _counter(monkeypatch)
    args = [command, "--root", str(root), "--external-dir", str(profile)]
    if command == "investigate":
        args += ["--file", "app.py"]
    args += [item.format(root=str(root)) for item in override]

    assert main(args) == 2

    assert calls == {name: 0 for name in calls}
    assert _inventory(root) == before
    assert not profile.exists()
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("external", ["equal", "ancestor", "descendant"])
def test_external_profile_cannot_equal_or_contain_or_be_inside_target(tmp_path, monkeypatch, capsys, external):
    root = _repository(tmp_path)
    before = _inventory(root)
    profile = {"equal": root, "ancestor": tmp_path, "descendant": root / "external"}[external]
    calls = _counter(monkeypatch)

    assert main(["analyze", "--root", str(root), "--external-dir", str(profile)]) == 2

    assert calls == {name: 0 for name in calls}
    assert _inventory(root) == before
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize(
    "relative,as_directory",
    [
        (Path("grammar-cache/opaque-child"), True),
        (Path("workspace/registry.json.lock"), False),
        (Path("workspace/backups/registry-old.json"), False),
    ],
)
def test_storage_descendant_reparse_mock_is_rejected_before_workspace_load(
    tmp_path, monkeypatch, capsys, relative, as_directory
):
    root = _repository(tmp_path)
    before = _inventory(root)
    profile = tmp_path / "external"
    opaque = profile / relative
    opaque.parent.mkdir(parents=True)
    if as_directory:
        opaque.mkdir()
    else:
        opaque.write_text("", encoding="utf-8")
    calls = _counter(monkeypatch)
    original = output_profile._is_link_or_reparse

    def mocked_reparse(path):
        return Path(path) == opaque or original(path)

    monkeypatch.setattr(output_profile, "_is_link_or_reparse", mocked_reparse)

    assert main(["analyze", "--root", str(root), "--external-dir", str(profile)]) == 2

    assert calls == {name: 0 for name in calls}
    assert _inventory(root) == before
    assert "reparse" in capsys.readouterr().err


def test_existing_registry_lock_or_grammar_child_symlink_is_rejected_when_supported(tmp_path, monkeypatch, capsys):
    root = _repository(tmp_path)
    before = _inventory(root)
    profile = tmp_path / "external"
    lock = profile / "workspace" / "registry.json.lock"
    lock.parent.mkdir(parents=True)
    target = tmp_path / "outside-lock"
    target.write_text("", encoding="utf-8")
    try:
        os.symlink(target, lock)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")
    calls = _counter(monkeypatch)

    assert main(["analyze", "--root", str(root), "--external-dir", str(profile)]) == 2

    assert calls == {name: 0 for name in calls}
    assert _inventory(root) == before
    assert "symlink or reparse" in capsys.readouterr().err


def test_investigate_external_cache_rebuilds_then_reuses_without_target_writes(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(WORKSPACE_ENV, raising=False)
    root = _repository(tmp_path)
    before = _inventory(root)
    profile = tmp_path / "external"
    command = ["investigate", "--root", str(root), "--external-dir", str(profile), "--file", "app.py"]

    assert main(command) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["workflow"]["cache"] == "rebuilt"
    assert main(command) == 0
    second = json.loads(capsys.readouterr().out)

    assert second["workflow"]["cache"] == "reused"
    assert list((profile / "investigation-cache").glob("*/analysis.json"))
    assert not (root / ".connection-map").exists()
    assert _inventory(root) == before


def test_investigate_cli_cache_overrides_profile(tmp_path, monkeypatch, capsys):
    root = _repository(tmp_path)
    before = _inventory(root)
    profile = tmp_path / "profile"
    cache = tmp_path / "cache-cli"
    grammar = tmp_path / "grammar-cli"
    command = [
        "investigate",
        "--root",
        str(root),
        "--external-dir",
        str(profile),
        "--cache-dir",
        str(cache),
        "--grammar-cache",
        str(grammar),
        "--file",
        "app.py",
    ]

    assert main(command) == 0
    capsys.readouterr()
    assert main(command) == 0
    captured = capsys.readouterr()

    assert json.loads(captured.out)["workflow"]["cache"] == "reused"
    assert list(cache.glob("*/analysis.json"))
    assert not (profile / "investigation-cache").exists()
    assert "from --cache-dir" in captured.err and "from --grammar-cache" in captured.err
    assert _inventory(root) == before


def test_raw_alias_component_is_rejected_before_lexical_normalization(tmp_path, monkeypatch, capsys):
    root = _repository(tmp_path)
    before = _inventory(root)
    profile = tmp_path / "external"
    alias = tmp_path / "alias-component"
    alias.mkdir()
    workspace = alias / ".." / "workspace"
    calls = _counter(monkeypatch)
    original = output_profile._is_link_or_reparse

    def mocked_alias(path):
        return Path(path) == alias or original(path)

    monkeypatch.setattr(output_profile, "_is_link_or_reparse", mocked_alias)

    assert main(
        ["analyze", "--root", str(root), "--external-dir", str(profile), "--workspace", str(workspace)]
    ) == 2

    assert calls == {name: 0 for name in calls}
    assert _inventory(root) == before
    assert "symlink or reparse point" in capsys.readouterr().err


@pytest.mark.parametrize("compact", [False, True])
def test_context_profile_guards_both_output_branches_and_requires_root(tmp_path, monkeypatch, capsys, compact):
    root = _repository(tmp_path)
    before = _inventory(root)
    document = analyze_repository(root, deterministic=True)
    input_path = tmp_path / "analysis.json"
    input_path.write_text(json.dumps(document), encoding="utf-8")
    node_id = next(node["id"] for node in document["nodes"] if node["display_name"] == "entry")
    output = root / "context.json"
    calls = {"load": 0}
    monkeypatch.setattr(cli, "_load_analysis", lambda path: calls.__setitem__("load", calls["load"] + 1))
    args = ["context", "--input", str(input_path), "--node", node_id, "--external-dir", str(tmp_path / "external")]
    if compact:
        args.append("--compact")
    args += ["--root", str(root), "--output", str(output)]

    assert main(args) == 2

    assert calls["load"] == 0
    assert not output.exists()
    assert _inventory(root) == before
    assert "outside the target" in capsys.readouterr().err


def test_context_profile_requires_root_before_reading_input(tmp_path, monkeypatch, capsys):
    calls = {"load": 0}
    monkeypatch.setattr(cli, "_load_analysis", lambda path: calls.__setitem__("load", calls["load"] + 1))

    assert main(["context", "--input", "missing.json", "--node", "x", "--external-dir", str(tmp_path / "external")]) == 2

    assert calls["load"] == 0
    assert "requires --root" in capsys.readouterr().err


@pytest.mark.parametrize("compact", [False, True])
def test_context_profile_writes_requested_external_output_in_both_modes(tmp_path, capsys, compact):
    root = _repository(tmp_path)
    before = _inventory(root)
    document = analyze_repository(root, deterministic=True)
    input_path = tmp_path / "analysis.json"
    input_path.write_text(json.dumps(document), encoding="utf-8")
    node_id = next(node["id"] for node in document["nodes"] if node["display_name"] == "entry")
    profile = tmp_path / "external"
    output = profile / "context.json"
    args = ["context", "--input", str(input_path), "--node", node_id, "--external-dir", str(profile), "--root", str(root)]
    if compact:
        args.append("--compact")
    args += ["--output", str(output)]

    assert main(args) == 0

    captured = capsys.readouterr()
    assert json.loads(output.read_text(encoding="utf-8"))
    assert "context output:" in captured.err and "from --output" in captured.err
    assert _inventory(root) == before


def test_grammar_cache_redirect_uses_public_api_and_restores_previous_cache(monkeypatch, tmp_path):
    state = {"cache": str(tmp_path / "pinned-cache"), "configured": []}

    class PackConfig:
        def __init__(self, *, cache_dir=None):
            self.cache_dir = cache_dir

    def configure(config):
        state["configured"].append(config.cache_dir)
        state["cache"] = config.cache_dir or str(tmp_path / "default-cache")

    pack = SimpleNamespace(PackConfig=PackConfig, configure=configure, cache_dir=lambda: state["cache"])
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", pack)

    with output_profile.configured_grammar_cache(tmp_path / "selected-cache") as available:
        assert available is True
        assert state["cache"] == str(tmp_path / "selected-cache")

    assert state["cache"] == str(tmp_path / "pinned-cache")
    assert state["configured"] == [str(tmp_path / "selected-cache"), str(tmp_path / "pinned-cache")]


def test_missing_optional_parser_allows_python_profile_and_explicit_cache(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(WORKSPACE_ENV, raising=False)
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", None)
    root = _repository(tmp_path)
    before = _inventory(root)
    workspace = tmp_path / "workspace"
    grammar = tmp_path / "grammar-cache"
    output = tmp_path / "analysis.json"

    assert main(
        [
            "analyze",
            "--root",
            str(root),
            "--workspace",
            str(workspace),
            "--grammar-cache",
            str(grammar),
            "--output",
            str(output),
        ]
    ) == 0

    assert output.is_file()
    assert (workspace / "registry.json").is_file()
    assert not grammar.exists()
    assert _inventory(root) == before
    captured = capsys.readouterr()
    assert "grammar cache:" in captured.err and "from --grammar-cache" in captured.err


def test_unavailable_optional_grammar_path_is_not_reported_as_stdout(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", None)
    root = _repository(tmp_path)
    workspace = tmp_path / "workspace"

    assert main(["analyze", "--root", str(root), "--workspace", str(workspace)]) == 0

    captured = capsys.readouterr()
    assert "grammar cache: unavailable (from optional parser unavailable)" in captured.err
    assert "grammar cache: stdout" not in captured.err


def test_incompatible_parser_configure_signature_errors_on_redirect(monkeypatch, tmp_path):
    class PackConfig:
        def __init__(self, *, cache_dir=None):
            self.cache_dir = cache_dir

    pack = SimpleNamespace(PackConfig=PackConfig, configure=lambda: None, cache_dir=lambda: "current")
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", pack)

    with pytest.raises(ValueError, match="cannot configure grammar cache"):
        with output_profile.configured_grammar_cache(tmp_path / "selected"):
            pytest.fail("an incompatible configure function must not enter the redirected operation")


def test_installed_parser_without_redirect_api_errors_before_analysis(monkeypatch, tmp_path, capsys):
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", SimpleNamespace())
    root = _repository(tmp_path)
    before = _inventory(root)
    calls = {"analyze": 0}
    monkeypatch.setattr(cli, "analyze_repository", lambda *args, **kwargs: calls.__setitem__("analyze", calls["analyze"] + 1))

    assert main(["analyze", "--root", str(root), "--external-dir", str(tmp_path / "external")]) == 2

    assert calls["analyze"] == 0
    assert not (tmp_path / "external").exists()
    assert _inventory(root) == before
    assert "public PackConfig/configure/cache_dir API" in capsys.readouterr().err


@pytest.mark.parametrize(
    "command,override",
    [
        ("analyze", ["--output", "{profile}/workspace/registry.json"]),
        ("analyze", ["--workspace", "{profile}/custom-workspace", "--output", "{profile}/custom-workspace/registry.json"]),
        ("analyze", ["--output", "{profile}/investigation-cache/opaque/analysis.json"]),
        ("analyze", ["--output", "{profile}/grammar-cache/cpp.so"]),
        ("analyze", ["--grammar-cache", "{profile}/custom-grammar", "--output", "{profile}/custom-grammar/cpp.so"]),
        ("investigate", ["--output", "{profile}/investigation-cache/opaque/analysis.json"]),
        ("investigate", ["--cache-dir", "{profile}/custom-cache", "--output", "{profile}/custom-cache/opaque/cache.json"]),
        ("investigate", ["--output", "{profile}/workspace/registry.json"]),
        ("investigate", ["--output", "{profile}/grammar-cache/cpp.so"]),
        ("context", ["--output", "{profile}/workspace/registry.json"]),
        ("context", ["--output", "{profile}/investigation-cache/opaque/analysis.json"]),
        ("context", ["--compact", "--output", "{profile}/grammar-cache/cpp.so"]),
        ("analyze", ["--workspace", "{profile}/shared", "--grammar-cache", "{profile}/shared"]),
        ("analyze", ["--workspace", "{profile}/shared", "--grammar-cache", "{profile}/shared/grammar"]),
        ("investigate", ["--cache-dir", "{profile}/shared/cache", "--grammar-cache", "{profile}/shared"]),
    ],
)
def test_profile_rejects_storage_output_collisions_before_any_effect(
    tmp_path, monkeypatch, capsys, command, override
):
    root = _repository(tmp_path)
    profile = tmp_path / "external"
    saved = {
        "workspace/registry.json": '{"existing": "workspace registry"}',
        "custom-workspace/registry.json": '{"existing": "custom registry"}',
        "investigation-cache/opaque/analysis.json": '{"existing": "analysis"}',
        "custom-cache/opaque/cache.json": '{"existing": "cache identity"}',
        "grammar-cache/cpp.so": "existing grammar bytes",
        "custom-grammar/cpp.so": "existing custom grammar bytes",
        "shared/keep.txt": "existing shared store",
    }
    for relative, value in saved.items():
        path = profile / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    before_root, before_storage = _inventory(root), _inventory(profile)
    calls = _counter(monkeypatch)
    calls["input_load"] = 0
    monkeypatch.setattr(cli, "_load_analysis", lambda path: calls.__setitem__("input_load", calls["input_load"] + 1))
    args = [command, "--root", str(root), "--external-dir", str(profile)]
    if command == "investigate":
        args += ["--file", "app.py"]
    if command == "context":
        args += ["--input", str(tmp_path / "unread.json"), "--node", "unread-node"]
    args += [item.format(profile=str(profile)) for item in override]

    assert main(args) == 2

    assert calls == {name: 0 for name in calls}
    assert _inventory(root) == before_root
    assert _inventory(profile) == before_storage
    error = capsys.readouterr().err
    assert "managed storage" in error or "managed stores must not overlap" in error
