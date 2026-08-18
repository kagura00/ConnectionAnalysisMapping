"""Local static server for the analysis viewer."""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import unquote, urlsplit

from .bundle import BundleError, quick_validate_bundle, validate_bundle
from .contract import ContractError, canonical_sha256, validate_document
from .layout import LayoutError, load_layout
from .workspace import RepositoryRecord, Workspace, WorkspaceError


def _content_matches(content: bytes, expected_sha256: str | None, *, canonical_json: bool = False) -> bool:
    if expected_sha256 is None:
        return True
    if canonical_json:
        try:
            return canonical_sha256(json.loads(content)) == expected_sha256
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            return False
    return hashlib.sha256(content).hexdigest() == expected_sha256


class AnalysisRequestHandler(SimpleHTTPRequestHandler):
    """Serve packaged assets and optional graph, layout, and bundle JSON files."""

    analysis_path: Path
    layout_path: Path | None = None
    bundle_path: Path | None = None
    bundle_files: ClassVar[set[str]] = set()
    bundle_hashes: ClassVar[dict[str, str]] = {}
    analysis_sha256: str = ""
    layout_sha256: str | None = None

    def do_GET(self) -> None:
        request_path = self.path.split("?", 1)[0]
        if request_path == "/analysis.json":
            self._serve_analysis()
            return
        if request_path == "/layout.json":
            self._serve_layout()
            return
        if request_path == "/bundle/index.json" or request_path.startswith("/bundle/"):
            relative = unquote(request_path.removeprefix("/bundle/") or "index.json")
            self._serve_bundle(relative)
            return
        if request_path == "/":
            self.path = "/index.html"
        super().do_GET()

    def _serve_analysis(self) -> None:
        try:
            content = self.analysis_path.read_bytes()
        except OSError:
            self.send_error(404, "analysis JSON is unavailable")
            return
        if not _content_matches(content, self.analysis_sha256, canonical_json=True):
            self.send_error(409, "analysis JSON failed its integrity check")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _serve_layout(self) -> None:
        if self.layout_path is None:
            self.send_error(404, "layout JSON is not configured")
            return
        try:
            content = self.layout_path.read_bytes()
        except OSError:
            self.send_error(404, "layout JSON is unavailable")
            return
        if not _content_matches(content, self.layout_sha256):
            self.send_error(409, "layout JSON failed its integrity check")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _serve_bundle(self, relative: str) -> None:
        if self.bundle_path is None or relative not in self.bundle_files:
            self.send_error(404, "bundle file is unavailable")
            return
        candidate = (self.bundle_path / Path(relative)).resolve()
        try:
            candidate.relative_to(self.bundle_path.resolve())
        except ValueError:
            self.send_error(404, "bundle file is unavailable")
            return
        try:
            content = candidate.read_bytes()
        except OSError:
            self.send_error(404, "bundle file is unavailable")
            return
        if not _content_matches(content, self.bundle_hashes.get(relative)):
            self.send_error(409, "bundle file failed its integrity check")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the local CLI output quiet except for the startup URL.
        return


class WorkspaceRequestHandler(SimpleHTTPRequestHandler):
    """Serve a registered repository catalog and repository-scoped data."""

    workspace: Workspace
    validation_states: dict[str, dict[str, Any]]
    validation_lock: threading.Lock
    bundle_files: dict[str, set[str]]
    bundle_hashes: dict[str, dict[str, str]]
    artifact_hashes: dict[str, dict[str, str]]

    def do_GET(self) -> None:
        request_path = urlsplit(self.path).path
        if request_path == "/api/repositories":
            self._serve_json(self._catalog_payload())
            return
        if request_path.startswith("/api/repositories/"):
            self._serve_repository_api(request_path)
            return
        if request_path == "/":
            self.path = "/index.html"
        super().do_GET()

    def _catalog_payload(self) -> dict[str, Any]:
        state = self.workspace.load()
        repositories = []
        with self.validation_lock:
            validation = dict(self.validation_states)
        for record in state["repositories"]:
            item = self.workspace.record_payload(record)
            item["validation"] = validation.get(record.repository_id, {"status": record.validation_status})
            repositories.append(item)
        return {
            "format": "connection-analysis-workspace-catalog",
            "schema_version": "1.0",
            "active_repository_id": state["active_repository_id"],
            "repositories": repositories,
        }

    def _serve_repository_api(self, request_path: str) -> None:
        parts = [unquote(part) for part in request_path.split("/") if part]
        if len(parts) < 4 or parts[:2] != ["api", "repositories"]:
            self.send_error(404, "repository API endpoint is unavailable")
            return
        repository_id = parts[2]
        try:
            record = self.workspace.get(repository_id)
        except WorkspaceError:
            self.send_error(404, "repository is not registered")
            return
        resource = parts[3]
        if resource == "validation" and len(parts) == 4:
            with self.validation_lock:
                payload = self.validation_states.get(repository_id, {"status": record.validation_status})
            self._serve_json(payload)
            return
        with self.validation_lock:
            validation = self.validation_states.get(repository_id, {"status": record.validation_status})
        if resource in {"manifest", "analysis.json", "layout.json"} and validation.get("status") == "invalid":
            self.send_error(409, "repository validation failed")
            return
        if resource == "manifest" and len(parts) == 4:
            expected_sha256 = self.artifact_hashes.get(repository_id, {}).get("manifest.json")
            if expected_sha256 is None:
                self.send_error(404, "manifest JSON is unavailable")
                return
            self._serve_file(
                self.workspace.path_for(record, Path(record.data_path, "manifest.json").as_posix()),
                "application/json; charset=utf-8",
                expected_sha256=expected_sha256,
            )
            return
        if resource == "analysis.json" and len(parts) == 4:
            expected_sha256 = self.artifact_hashes.get(repository_id, {}).get("analysis.json")
            if expected_sha256 is None:
                self.send_error(404, "analysis JSON is unavailable")
                return
            self._serve_file(
                self.workspace.path_for(record, record.analysis_path),
                "application/json; charset=utf-8",
                expected_sha256=expected_sha256,
                canonical_json=True,
            )
            return
        if resource == "layout.json" and len(parts) == 4:
            expected_sha256 = self.artifact_hashes.get(repository_id, {}).get("layout.json")
            if expected_sha256 is None:
                self.send_error(404, "layout JSON is unavailable")
                return
            self._serve_file(
                self.workspace.path_for(record, record.layout_path),
                "application/json; charset=utf-8",
                expected_sha256=expected_sha256,
            )
            return
        if resource == "bundle" and len(parts) >= 5:
            relative = "/".join(parts[4:])
            if relative not in self.bundle_files.get(repository_id, set()):
                self.send_error(404, "bundle file is unavailable")
                return
            try:
                path = self.workspace.path_for(record, Path(record.bundle_path, relative).as_posix())
            except WorkspaceError:
                self.send_error(404, "bundle file is unavailable")
                return
            if validation.get("status") == "invalid":
                self.send_error(409, "repository validation failed")
                return
            expected_sha256 = self.bundle_hashes.get(repository_id, {}).get(relative)
            if expected_sha256 is None:
                self.send_error(503, "bundle file is not integrity-checked yet")
                return
            self._serve_file(path, "application/json; charset=utf-8", expected_sha256=expected_sha256)
            return
        self.send_error(404, "repository API endpoint is unavailable")

    def _serve_file(
        self,
        path: Path,
        content_type: str,
        *,
        expected_sha256: str | None = None,
        canonical_json: bool = False,
    ) -> None:
        try:
            content = path.read_bytes()
        except OSError:
            self.send_error(404, "requested data is unavailable")
            return
        if expected_sha256 is not None:
            actual_sha256 = hashlib.sha256(content).hexdigest()
            if canonical_json:
                try:
                    actual_sha256 = canonical_sha256(json.loads(content))
                except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                    self.send_error(409, "requested JSON failed its integrity check")
                    return
            if actual_sha256 != expected_sha256:
                self.send_error(409, "requested data failed its integrity check")
                return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _serve_json(self, payload: Any) -> None:
        content = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve_analysis(
    analysis_path: Path,
    *,
    layout_path: Path | None = None,
    bundle_path: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """Serve the packaged static UI and validated analysis/layout snapshots."""

    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("local analysis mode only supports loopback hosts")
    analysis_path = analysis_path.resolve()
    if not analysis_path.is_file():
        raise FileNotFoundError(analysis_path)
    try:
        document = json.loads(analysis_path.read_text(encoding="utf-8"))
        validate_document(document)
    except (OSError, UnicodeError, json.JSONDecodeError, ContractError) as exc:
        raise ValueError(f"invalid analysis JSON: {analysis_path}: {exc}") from exc
    if layout_path is not None:
        layout_path = layout_path.resolve()
        if not layout_path.is_file():
            raise FileNotFoundError(layout_path)
        try:
            load_layout(layout_path, analysis_schema_version=document["schema_version"])
        except LayoutError as exc:
            raise ValueError(f"invalid layout JSON: {layout_path}: {exc}") from exc
    bundle_index: dict[str, Any] | None = None
    if bundle_path is not None:
        bundle_path = bundle_path.resolve()
        try:
            bundle_index = validate_bundle(bundle_path)
        except (BundleError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError(f"invalid graph bundle: {bundle_path}: {exc}") from exc
        if canonical_sha256(document) != bundle_index["analysis_sha256"]:
            raise ValueError(
                f"analysis JSON and graph bundle refer to different snapshots: {analysis_path} / {bundle_path}"
            )
    asset_directory = Path(__file__).resolve().parent / "web"
    if not (asset_directory / "index.html").is_file():
        raise FileNotFoundError(f"web assets are not installed: {asset_directory}")
    class BoundAnalysisRequestHandler(AnalysisRequestHandler):
        pass

    BoundAnalysisRequestHandler.analysis_path = analysis_path
    BoundAnalysisRequestHandler.layout_path = layout_path
    BoundAnalysisRequestHandler.bundle_path = bundle_path
    BoundAnalysisRequestHandler.analysis_sha256 = canonical_sha256(document)
    BoundAnalysisRequestHandler.layout_sha256 = _file_sha256(layout_path) if layout_path is not None else None
    BoundAnalysisRequestHandler.bundle_files = _bundle_file_set(bundle_path, bundle_index) if bundle_index is not None else set()
    BoundAnalysisRequestHandler.bundle_hashes = _bundle_file_hashes(bundle_path, bundle_index) if bundle_index is not None else {}
    handler = partial(BoundAnalysisRequestHandler, directory=str(asset_directory))
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Connection Analysis Mapping: http://{host}:{server.server_port}/")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nserver stopped")
    finally:
        server.server_close()


def _bundle_file_set(bundle: Path, index: dict[str, Any]) -> set[str]:
    """Build the allow-list used by the repository-scoped bundle endpoint."""

    files = {"index.json"}
    for entries in index.get("chunks", {}).values():
        files.update(entry["path"] for entry in entries if isinstance(entry, dict) and isinstance(entry.get("path"), str))
    search = index.get("search", {})
    for entry in search.get("record_chunks", []):
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            files.add(entry["path"])
    for shard in search.get("shards", []):
        for entry in shard.get("chunks", []):
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                files.add(entry["path"])
    overview_entry = index.get("overview")
    if isinstance(overview_entry, dict) and isinstance(overview_entry.get("path"), str):
        files.add(overview_entry["path"])
        try:
            overview = json.loads((bundle / overview_entry["path"]).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return files
        if not isinstance(overview, dict):
            return files
        edge_group_chunks = overview.get("edge_group_chunks", [])
        if not isinstance(edge_group_chunks, list):
            return files
        for entry in edge_group_chunks:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                files.add(entry["path"])
    return files


def _file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _bundle_file_hashes(bundle: Path, index: dict[str, Any]) -> dict[str, str]:
    """Collect hashes for every file that can be served before full validation."""

    hashes: dict[str, str] = {}

    def collect(entries: Any) -> None:
        if not isinstance(entries, list):
            return
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str) and isinstance(entry.get("sha256"), str):
                hashes[entry["path"]] = entry["sha256"]

    index_path = bundle / "index.json"
    try:
        hashes["index.json"] = hashlib.sha256(index_path.read_bytes()).hexdigest()
    except OSError:
        pass
    chunks = index.get("chunks", {})
    if isinstance(chunks, dict):
        for entries in chunks.values():
            collect(entries)
    search = index.get("search", {})
    if isinstance(search, dict):
        collect(search.get("record_chunks"))
        for shard in search.get("shards", []) if isinstance(search.get("shards", []), list) else []:
            if isinstance(shard, dict):
                collect(shard.get("chunks"))
    overview_entry = index.get("overview")
    if isinstance(overview_entry, dict):
        collect([overview_entry])
        overview_path = overview_entry.get("path")
        if isinstance(overview_path, str):
            try:
                overview = json.loads((bundle / overview_path).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                overview = None
            if isinstance(overview, dict):
                collect(overview.get("edge_group_chunks"))
    return hashes


def _quick_workspace_state(
    workspace: Workspace, record: RepositoryRecord,
) -> tuple[dict[str, Any], set[str], dict[str, str]]:
    """Check the small manifest and bundle index without reading analysis.json."""

    manifest_path = workspace.path_for(record, Path(record.data_path, "manifest.json").as_posix())
    bundle_path = workspace.path_for(record, record.bundle_path)
    if not manifest_path.is_file():
        return {"status": "invalid", "message": "manifest.json is missing; rerun analyze", "phase": "startup"}, set(), {}
    if not (bundle_path / "index.json").is_file():
        return {"status": "invalid", "message": "bundle/index.json is missing", "phase": "startup"}, set(), {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("format") != "connection-analysis-repository-manifest":
            raise ValueError("repository manifest format is invalid")
        if manifest.get("schema_version") != "1.0" or manifest.get("repository_id") != record.repository_id:
            raise ValueError("repository manifest schema or repository_id is invalid")
        analysis = manifest.get("analysis")
        bundle = manifest.get("bundle")
        if not isinstance(analysis, dict) or not isinstance(bundle, dict):
            raise ValueError("repository manifest analysis/bundle entries are invalid")
        expected_analysis_path = Path(record.analysis_path).relative_to(Path(record.data_path)).as_posix()
        expected_bundle_path = Path(record.bundle_path).relative_to(Path(record.data_path)).as_posix()
        if analysis.get("path") != expected_analysis_path or bundle.get("path") != expected_bundle_path:
            raise ValueError("repository manifest paths do not match the registry")
        analysis_sha256 = analysis.get("sha256")
        if not isinstance(analysis_sha256, str) or len(analysis_sha256) != 64 or any(char not in "0123456789abcdef" for char in analysis_sha256):
            raise ValueError("repository manifest analysis digest is invalid")
        if bundle.get("present") is not True:
            raise ValueError("repository manifest does not contain a bundle")
        bundle_sha256 = bundle.get("sha256")
        if not isinstance(bundle_sha256, str) or len(bundle_sha256) != 64 or any(char not in "0123456789abcdef" for char in bundle_sha256):
            raise ValueError("repository manifest bundle digest is invalid")
        if hashlib.sha256((bundle_path / "index.json").read_bytes()).hexdigest() != bundle_sha256:
            raise ValueError("repository manifest bundle digest does not match the index")
        layout = manifest.get("layout", {})
        layout_sha256: str | None = None
        if not isinstance(layout, dict):
            raise ValueError("repository manifest layout entry is invalid")
        layout_path = workspace.path_for(record, record.layout_path)
        if layout.get("present") is True:
            layout_sha256 = layout.get("sha256")
            if not isinstance(layout_sha256, str) or len(layout_sha256) != 64 or any(char not in "0123456789abcdef" for char in layout_sha256):
                raise ValueError("repository manifest layout digest is invalid")
            if not layout_path.is_file() or hashlib.sha256(layout_path.read_bytes()).hexdigest() != layout_sha256:
                raise ValueError("repository manifest layout digest does not match the file")
        elif layout_path.exists():
            raise ValueError("repository manifest layout presence does not match the file")
        if record.last_analysis_sha256 and analysis_sha256 != record.last_analysis_sha256:
            raise ValueError("repository manifest differs from the registry digest")
        index = quick_validate_bundle(bundle_path, expected_analysis_sha256=analysis_sha256)
        return {
            "status": "pending",
            "phase": "startup",
            "message": "quick checks passed; full validation is running",
            "analysis_sha256": analysis_sha256,
            "layout_sha256": layout_sha256,
            "counts": index.get("counts", {}),
        }, _bundle_file_set(bundle_path, index), _bundle_file_hashes(bundle_path, index)
    except (OSError, UnicodeError, json.JSONDecodeError, BundleError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return {"status": "invalid", "phase": "startup", "message": str(exc)}, set(), {}


def _full_validate_workspace_record(workspace: Workspace, record: RepositoryRecord) -> dict[str, Any]:
    analysis_path = workspace.path_for(record, record.analysis_path)
    bundle_path = workspace.path_for(record, record.bundle_path)
    try:
        document = json.loads(analysis_path.read_text(encoding="utf-8"))
        validate_document(document)
        index = validate_bundle(bundle_path)
        if canonical_sha256(document) != index["analysis_sha256"]:
            raise ValueError("analysis JSON and bundle have different canonical digests")
        layout_path = workspace.path_for(record, record.layout_path)
        if layout_path.is_file():
            load_layout(layout_path, analysis_schema_version=document["schema_version"])
        return {
            "status": "valid",
            "phase": "full",
            "message": "analysis, bundle, and optional layout are valid",
            "analysis_sha256": index["analysis_sha256"],
            "counts": index.get("counts", {}),
        }
    except (OSError, UnicodeError, json.JSONDecodeError, ContractError, BundleError, LayoutError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return {"status": "invalid", "phase": "full", "message": str(exc)}


def serve_workspace(
    workspace: Workspace | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """Serve all registered repository snapshots from a central workspace.

    Only the bounded startup check runs before the socket is opened.  Full
    graph reconstruction is serialized in a background worker so a large
    repository cannot make the browser wait for every snapshot at startup.
    """

    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("central workspace mode only supports loopback hosts")
    active_workspace = workspace if isinstance(workspace, Workspace) else Workspace(workspace)
    records = active_workspace.records()
    asset_directory = Path(__file__).resolve().parent / "web"
    if not (asset_directory / "index.html").is_file():
        raise FileNotFoundError(f"web assets are not installed: {asset_directory}")
    validation_states: dict[str, dict[str, Any]] = {}
    bundle_files: dict[str, set[str]] = {}
    bundle_hashes: dict[str, dict[str, str]] = {}
    artifact_hashes: dict[str, dict[str, str]] = {}
    for record in records:
        quick_state, allowed_files, allowed_hashes = _quick_workspace_state(active_workspace, record)
        validation_states[record.repository_id] = quick_state
        if quick_state["status"] == "pending":
            bundle_files[record.repository_id] = allowed_files
            bundle_hashes[record.repository_id] = allowed_hashes
            manifest_path = active_workspace.path_for(
                record,
                Path(record.data_path, "manifest.json").as_posix(),
            )
            manifest_sha256 = _file_sha256(manifest_path)
            analysis_sha256 = quick_state.get("analysis_sha256")
            if manifest_sha256 is not None and isinstance(analysis_sha256, str):
                artifact_hashes[record.repository_id] = {
                    "manifest.json": manifest_sha256,
                    # The manifest stores the canonical graph digest.  The
                    # handler compares that digest after parsing the response.
                    "analysis.json": analysis_sha256,
                    **(
                        {"layout.json": quick_state["layout_sha256"]}
                        if isinstance(quick_state.get("layout_sha256"), str)
                        else {}
                    ),
                }
        else:
            bundle_files[record.repository_id] = set()
            bundle_hashes[record.repository_id] = {}
    lock = threading.Lock()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="connection-map-validation")

    def validate_record(record: RepositoryRecord) -> None:
        with lock:
            if validation_states[record.repository_id].get("status") == "pending":
                validation_states[record.repository_id] = {
                    **validation_states[record.repository_id],
                    "status": "running",
                    "phase": "full",
                }
        try:
            result = _full_validate_workspace_record(active_workspace, record)
        except Exception as exc:  # Keep one malformed snapshot from wedging the worker.
            result = {"status": "invalid", "phase": "full", "message": f"validation failed: {exc}"}
        try:
            active_workspace.set_validation(record.repository_id, result["status"])
        except WorkspaceError:
            # The in-memory result remains authoritative for this server
            # session if the registry was changed by an external process.
            pass
        with lock:
            validation_states[record.repository_id] = result

    for record in records:
        if validation_states[record.repository_id]["status"] == "pending":
            executor.submit(validate_record, record)

    class BoundWorkspaceRequestHandler(WorkspaceRequestHandler):
        pass

    BoundWorkspaceRequestHandler.workspace = active_workspace
    BoundWorkspaceRequestHandler.validation_states = validation_states
    BoundWorkspaceRequestHandler.validation_lock = lock
    BoundWorkspaceRequestHandler.bundle_files = bundle_files
    BoundWorkspaceRequestHandler.bundle_hashes = bundle_hashes
    BoundWorkspaceRequestHandler.artifact_hashes = artifact_hashes
    handler = partial(BoundWorkspaceRequestHandler, directory=str(asset_directory))
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Connection Analysis Mapping: http://{host}:{server.server_port}/")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nserver stopped")
    finally:
        server.server_close()
        executor.shutdown(wait=False, cancel_futures=True)
