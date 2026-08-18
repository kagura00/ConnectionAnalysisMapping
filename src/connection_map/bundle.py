"""Static chunking, validation, and search for large graph snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import unicodedata
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .contract import ContractError, canonical_sha256, validate_document


class BundleError(ValueError):
    """Raised when a graph bundle is invalid or cannot be created safely."""


def _is_link_or_reparse_point(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise BundleError(f"cannot inspect bundle path: {path}") from exc
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _ensure_output_path_is_real(path: Path) -> None:
    lexical = path.absolute()
    current = Path(lexical.anchor) if lexical.anchor else Path()
    for part in lexical.parts[1:] if lexical.anchor else lexical.parts:
        current /= part
        if _is_link_or_reparse_point(current):
            raise BundleError(f"bundle output must not contain a symlink or junction: {current}")


_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_CATEGORIES = ("nodes", "edges", "diagnostics")
_OVERVIEW_EDGE_CHUNK_SIZE = 2_000


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_json(path: Path, value: Any) -> tuple[int, str]:
    payload = _json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return len(value) if isinstance(value, list) else 1, _sha256_bytes(payload)


def _chunked(items: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _validate_chunk_size(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise BundleError(f"{name} must be a positive integer")


def _module_by_node_id(nodes: list[dict[str, Any]]) -> dict[str, str]:
    """Map each graph node to its real file module when one can be found.

    File membership wins over the lexical parent chain because some analyzers
    create logical namespace nodes above a file module. Module-less nodes are
    intentionally left for :func:`_presentation_modules` to place into a
    virtual presentation module.
    """

    node_by_id = {node.get("id"): node for node in nodes if isinstance(node, dict)}
    modules = {node_id for node_id, node in node_by_id.items() if node.get("kind") == "module"}
    module_by_file = {
        node.get("file"): node_id
        for node_id, node in node_by_id.items()
        if node.get("kind") == "module" and isinstance(node.get("file"), str) and node.get("file")
    }
    result: dict[str, str] = {}
    for node_id, node in node_by_id.items():
        file_path = node.get("file")
        if isinstance(file_path, str) and file_path in module_by_file:
            result[node_id] = module_by_file[file_path]
            continue
        current = node_id
        visited: set[str] = set()
        while current and current not in visited:
            if current in modules:
                result[node_id] = current
                break
            visited.add(current)
            parent = node_by_id.get(current, {}).get("parent_id")
            current = parent if isinstance(parent, str) else ""
    return result


def _virtual_module_for_node(node: dict[str, Any]) -> dict[str, Any]:
    """Create deterministic presentation metadata for module-less nodes."""

    kind = str(node.get("kind") or "unknown")
    bucket = "external" if kind == "external" else "unresolved"
    language = str((node.get("extensions") or {}).get("language") or "unknown")
    digest = hashlib.sha1(f"{bucket}\x1f{language}".encode()).hexdigest()[:16]
    module_id = f"bundle:virtual-module:{digest}"
    label = "外部" if bucket == "external" else "未解決"
    return {
        "id": module_id,
        "kind": "module",
        "qualified_name": f"<{label}>:{language}",
        "display_name": f"{label} ({language})",
        "file": None,
        "span": None,
        "parent_id": None,
        "visibility": "unknown",
        "extensions": {
            "language": language,
            "virtual": True,
            "virtual_scope": bucket,
        },
    }


def _presentation_modules(
    nodes: list[dict[str, Any]],
    module_by_node_id: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Return real modules plus virtual modules for external/unresolved nodes."""

    real_modules = [node for node in nodes if node.get("kind") == "module"]
    virtual_by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        node_id = node.get("id")
        if not isinstance(node_id, str) or node_id in module_by_node_id:
            continue
        virtual = _virtual_module_for_node(node)
        module_by_node_id[node_id] = virtual["id"]
        virtual_by_id[virtual["id"]] = virtual
    return real_modules + [virtual_by_id[key] for key in sorted(virtual_by_id)], module_by_node_id


def _search_key(character: str) -> str:
    return f"u{ord(character):x}"


def normalize_search_text(value: str) -> str:
    """Normalize search text identically for bundle, CLI, and browser clients."""

    return unicodedata.normalize("NFKC", value).lower()


def split_analysis(
    document: dict[str, Any],
    output: Path,
    *,
    node_chunk_size: int = 2_000,
    edge_chunk_size: int = 5_000,
    diagnostic_chunk_size: int = 2_000,
    search_chunk_size: int = 5_000,
    force: bool = False,
) -> dict[str, Any]:
    """Write a bundle through a validated sibling directory swap."""

    _ensure_output_path_is_real(Path(output))
    final_output = Path(output).resolve()
    if final_output.exists() and not final_output.is_dir():
        raise BundleError(f"bundle output is not a directory: {final_output}")
    if final_output.exists() and any(final_output.iterdir()) and not force:
        raise BundleError(f"bundle output is not empty; use --force to update: {final_output}")

    staging = final_output.parent / f".{final_output.name}.staging-{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)
    backup: Path | None = None
    try:
        index = _split_analysis_unchecked(
            document,
            staging,
            node_chunk_size=node_chunk_size,
            edge_chunk_size=edge_chunk_size,
            diagnostic_chunk_size=diagnostic_chunk_size,
            search_chunk_size=search_chunk_size,
            force=False,
        )
        validate_bundle(staging)
        if final_output.exists():
            backup = final_output.parent / f".{final_output.name}.backup-{uuid.uuid4().hex}"
            final_output.rename(backup)
        try:
            staging.rename(final_output)
        except Exception:
            if backup is not None and backup.exists() and not final_output.exists():
                backup.rename(final_output)
            raise
        if backup is not None:
            shutil.rmtree(backup)
            backup = None
        return index
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if backup is not None and backup.exists() and not final_output.exists():
            backup.rename(final_output)


def _split_analysis_unchecked(
    document: dict[str, Any],
    output: Path,
    *,
    node_chunk_size: int = 2_000,
    edge_chunk_size: int = 5_000,
    diagnostic_chunk_size: int = 2_000,
    search_chunk_size: int = 5_000,
    force: bool = False,
) -> dict[str, Any]:
    """Write a deterministic bundle and return its index document."""

    try:
        validate_document(document)
    except ContractError as exc:
        raise BundleError(f"analysis is invalid: {exc}") from exc
    for value, name in (
        (node_chunk_size, "node_chunk_size"),
        (edge_chunk_size, "edge_chunk_size"),
        (diagnostic_chunk_size, "diagnostic_chunk_size"),
        (search_chunk_size, "search_chunk_size"),
    ):
        _validate_chunk_size(value, name)

    output = output.resolve()
    if output.exists() and not output.is_dir():
        raise BundleError(f"bundle output is not a directory: {output}")
    if output.exists() and any(output.iterdir()) and not force:
        raise BundleError(f"bundle output is not empty; use --force to update: {output}")
    output.mkdir(parents=True, exist_ok=True)

    chunks: dict[str, list[dict[str, Any]]] = {category: [] for category in _CATEGORIES}
    node_chunk_by_id: dict[str, int] = {}
    edge_chunk_by_id: dict[str, int] = {}
    source_hash = canonical_sha256(document)
    for category, size in (
        ("nodes", node_chunk_size),
        ("edges", edge_chunk_size),
        ("diagnostics", diagnostic_chunk_size),
    ):
        values = document[category]
        for index, chunk in enumerate(_chunked(values, size)):
            relative = f"{category}/{index:05d}.json"
            count, digest = _write_json(output / relative, chunk)
            chunks[category].append({"path": relative, "count": count, "sha256": digest})
            if category == "nodes":
                for node in chunk:
                    node_chunk_by_id[node["id"]] = index
            elif category == "edges":
                for edge in chunk:
                    edge_chunk_by_id[edge["id"]] = index

    module_by_node_id = _module_by_node_id(document["nodes"])
    module_nodes, module_by_node_id = _presentation_modules(document["nodes"], module_by_node_id)
    node_ordinal_by_id = {node["id"]: ordinal for ordinal, node in enumerate(document["nodes"])}
    node_chunks_by_module: dict[str, set[int]] = {node["id"]: set() for node in module_nodes}
    edge_chunks_by_module: dict[str, set[int]] = {node["id"]: set() for node in module_nodes}
    edge_chunks_by_node: dict[str, set[int]] = {
        str(ordinal): set() for ordinal in range(len(document["nodes"]))
    }
    for node_id, module_id in module_by_node_id.items():
        chunk_index = node_chunk_by_id.get(node_id)
        if chunk_index is not None:
            node_chunks_by_module.setdefault(module_id, set()).add(chunk_index)
    for edge in document["edges"]:
        chunk_index = edge_chunk_by_id.get(edge["id"])
        if chunk_index is None:
            continue
        for node_id in (edge.get("source_id"), edge.get("target_id")):
            module_id = module_by_node_id.get(node_id)
            if module_id is not None:
                edge_chunks_by_module.setdefault(module_id, set()).add(chunk_index)
            ordinal = node_ordinal_by_id.get(node_id)
            if ordinal is not None:
                edge_chunks_by_node[str(ordinal)].add(chunk_index)

    module_groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for edge in document["edges"]:
        source_module = module_by_node_id.get(edge.get("source_id"))
        target_module = module_by_node_id.get(edge.get("target_id"))
        if source_module is None or target_module is None or source_module == target_module:
            continue
        key = (
            source_module,
            target_module,
            str(edge.get("relation_type")),
            str(edge.get("resolution_status")),
        )
        group = module_groups.get(key)
        if group is None:
            digest = hashlib.sha1("\x1f".join(key).encode("utf-8")).hexdigest()[:20]
            group = {
                "id": f"bundle:module-group:{digest}",
                "source_id": source_module,
                "target_id": target_module,
                "relation_type": edge["relation_type"],
                "resolution_status": edge["resolution_status"],
                "provenance": "unknown",
                "confidence": edge["confidence"],
                "source_span": None,
                "detail": {
                    "aggregate": True,
                    "count": 0,
                    "representative_edge_id": edge["id"],
                    "confidence_min": edge["confidence"],
                    "confidence_max": edge["confidence"],
                },
            }
            module_groups[key] = group
        group["detail"]["count"] += 1
        group["detail"]["confidence_min"] = min(group["detail"]["confidence_min"], edge["confidence"])
        group["detail"]["confidence_max"] = max(group["detail"]["confidence_max"], edge["confidence"])

    module_index_by_id = {node["id"]: index for index, node in enumerate(module_nodes)}
    node_chunks_by_node = {
        str(ordinal): [node_chunk_by_id[node["id"]]]
        for ordinal, node in enumerate(document["nodes"])
        if node["id"] in node_chunk_by_id
    }
    node_chunks_by_id = {
        node["id"]: [node_chunk_by_id[node["id"]]]
        for node in document["nodes"]
        if node["id"] in node_chunk_by_id
    }
    compact_groups = [
        {
            "source": module_index_by_id[group["source_id"]],
            "target": module_index_by_id[group["target_id"]],
            "relation": group["relation_type"],
            "status": group["resolution_status"],
            "count": group["detail"]["count"],
            "confidence_min": group["detail"]["confidence_min"],
            "confidence_max": group["detail"]["confidence_max"],
            # Keep one concrete edge reachable from the overview.  The
            # aggregate remains cheap to render, while a user can inspect the
            # exact source span and target by loading this edge's chunk.
            "representative_edge_id": group["detail"]["representative_edge_id"],
            "representative_edge_chunk": edge_chunk_by_id[group["detail"]["representative_edge_id"]],
        }
        for group in sorted(module_groups.values(), key=lambda item: item["id"])
    ]
    overview_edge_chunks: list[dict[str, Any]] = []
    overview_edge_chunk_by_module: dict[int, set[int]] = {
        index: set() for index in range(len(module_nodes))
    }
    for index, chunk in enumerate(_chunked(compact_groups, _OVERVIEW_EDGE_CHUNK_SIZE)):
        relative = f"overview/edges/{index:05d}.json"
        count, digest = _write_json(output / relative, chunk)
        overview_edge_chunks.append({"path": relative, "count": count, "sha256": digest})
        for group in chunk:
            overview_edge_chunk_by_module[group["source"]].add(index)
            overview_edge_chunk_by_module[group["target"]].add(index)

    overview = {
        "format": "connection-analysis-overview",
        "schema_version": "1.0",
        "analysis_schema_version": document["schema_version"],
        "modules": module_nodes,
        "module_by_node": {
            str(ordinal): module_index_by_id[module_by_node_id[node["id"]]]
            for ordinal, node in enumerate(document["nodes"])
            if node["id"] in module_by_node_id
        },
        "node_chunks_by_module": {
            str(module_index_by_id[module_id]): sorted(indices)
            for module_id, indices in sorted(node_chunks_by_module.items())
        },
        "node_chunks_by_node": node_chunks_by_node,
        "node_chunks_by_id": node_chunks_by_id,
        "edge_chunks_by_module": {
            str(module_index_by_id[module_id]): sorted(indices)
            for module_id, indices in sorted(edge_chunks_by_module.items())
        },
        "edge_chunks_by_node": {
            ordinal: sorted(indices) for ordinal, indices in sorted(edge_chunks_by_node.items())
        },
        "module_edge_chunks_by_module": {
            str(module_index): sorted(indices)
            for module_index, indices in sorted(overview_edge_chunk_by_module.items())
        },
        "edge_group_chunks": overview_edge_chunks,
    }
    overview_count, overview_digest = _write_json(output / "overview/index.json", overview)

    # Keep the character index compact. Full node records are already stored
    # in node chunks, while the separate search records let the browser
    # confirm a query without loading those large chunks just to render a
    # result list.
    search_record_chunks: list[dict[str, Any]] = []
    search_records: list[dict[str, Any]] = []
    for node_index, node in enumerate(document["nodes"]):
        record: dict[str, Any] = {
            "ordinal": node_index,
            "id": node["id"],
            "kind": node["kind"],
            "qualified_name": node.get("qualified_name", ""),
            "display_name": node.get("display_name", ""),
            "file": str(node.get("file") or ""),
            "language": (node.get("extensions") or {}).get("language", ""),
        }
        module_id = module_by_node_id.get(node["id"])
        if module_id is not None:
            record["module"] = module_index_by_id.get(module_id)
        search_records.append(record)
    for index, chunk in enumerate(_chunked(search_records, node_chunk_size)):
        relative = f"search/records/{index:05d}.json"
        count, digest = _write_json(output / relative, chunk)
        search_record_chunks.append({"path": relative, "count": count, "sha256": digest})

    search_by_key: dict[str, list[int]] = {}
    for node_index, node in enumerate(document["nodes"]):
        searchable = "\n".join(
            str(node.get(field) or "")
            for field in ("id", "qualified_name", "display_name", "file")
        )
        searchable = normalize_search_text(searchable)
        keys = sorted({_search_key(char) for char in searchable if not char.isspace()})
        for key in keys:
            search_by_key.setdefault(key, []).append(node_index)

    search_shards: list[dict[str, Any]] = []
    for key in sorted(search_by_key):
        records = sorted(set(search_by_key[key]))
        shard_chunks: list[dict[str, Any]] = []
        for index, chunk in enumerate(_chunked(records, search_chunk_size)):
            relative = f"search/{key}-{index:05d}.json"
            count, digest = _write_json(output / relative, chunk)
            shard_chunks.append({"path": relative, "count": count, "sha256": digest})
        search_shards.append({"key": key, "count": len(records), "chunks": shard_chunks})

    index = {
        "format": "connection-analysis-bundle",
        "schema_version": "1.0",
        "analysis_schema_version": document["schema_version"],
        "analysis_sha256": source_hash,
        "meta": document["meta"],
        "counts": {
            "nodes": len(document["nodes"]),
            "edges": len(document["edges"]),
            "diagnostics": len(document["diagnostics"]),
        },
        "chunks": chunks,
        "overview": {
            "path": "overview/index.json",
            "count": overview_count,
            "sha256": overview_digest,
        },
        "search": {
            "mode": "contains",
            "normalization": "nfkc-lower",
            "record_format": "node_records",
            "record_chunk_size": node_chunk_size,
            "record_chunks": search_record_chunks,
            "shards": search_shards,
        },
    }
    _write_json(output / "index.json", index)
    return index


def split_analysis_file(
    input_path: Path,
    output: Path,
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        document = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"invalid analysis JSON: {input_path}") from exc
    return split_analysis(document, output, **kwargs)


def load_bundle_index(bundle: Path) -> dict[str, Any]:
    index_path = bundle.resolve() / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"invalid bundle index: {index_path}") from exc
    if not isinstance(index, dict) or index.get("format") != "connection-analysis-bundle":
        raise BundleError("bundle index.format must be connection-analysis-bundle")
    if index.get("schema_version") != "1.0":
        raise BundleError("bundle index.schema_version must be 1.0")
    if not isinstance(index.get("chunks"), dict) or not isinstance(index.get("search"), dict):
        raise BundleError("bundle index must contain chunks and search objects")
    if not isinstance(index.get("meta"), dict):
        raise BundleError("bundle index.meta must be an object")
    if not isinstance(index.get("analysis_schema_version"), str) or not index["analysis_schema_version"]:
        raise BundleError("bundle index.analysis_schema_version must be a string")
    counts = index.get("counts")
    if not isinstance(counts, dict):
        raise BundleError("bundle index.counts must be an object")
    for category in _CATEGORIES:
        count = counts.get(category)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise BundleError(f"bundle index.counts.{category} must be a non-negative integer")
    analysis_hash = index.get("analysis_sha256")
    if not isinstance(analysis_hash, str) or not _HEX_RE.fullmatch(analysis_hash):
        raise BundleError("bundle index.analysis_sha256 must be a SHA-256 hex string")
    return index


def quick_validate_bundle(bundle: Path, *, expected_analysis_sha256: str | None = None) -> dict[str, Any]:
    """Validate only the bundle index and referenced file names.

    Workspace startup uses this bounded check so the UI can open before every
    chunk has been hashed and reconstructed.  ``validate_bundle`` remains the
    authoritative full check and is run asynchronously by central mode.
    """

    index = load_bundle_index(bundle)
    if expected_analysis_sha256 is not None and index["analysis_sha256"] != expected_analysis_sha256:
        raise BundleError("bundle analysis_sha256 does not match the registered analysis")
    chunks = index["chunks"]
    for category in _CATEGORIES:
        entries = chunks.get(category)
        if not isinstance(entries, list):
            raise BundleError(f"bundle index.chunks.{category} must be an array")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise BundleError(f"invalid bundle {category} chunk entry")
            _safe_relative_file(bundle.resolve(), entry["path"])
    search = index["search"]
    record_chunks = search.get("record_chunks", [])
    if not isinstance(record_chunks, list):
        raise BundleError("bundle search.record_chunks must be an array")
    for entry in record_chunks:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise BundleError("invalid bundle search record chunk entry")
        _safe_relative_file(bundle.resolve(), entry["path"])
    shards = search.get("shards", [])
    if not isinstance(shards, list):
        raise BundleError("bundle search.shards must be an array")
    for shard in shards:
        if not isinstance(shard, dict) or not isinstance(shard.get("chunks"), list):
            raise BundleError("invalid bundle search shard")
        for entry in shard["chunks"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise BundleError("invalid bundle search shard chunk entry")
            _safe_relative_file(bundle.resolve(), entry["path"])
    overview = index.get("overview")
    if isinstance(overview, dict) and isinstance(overview.get("path"), str):
        _safe_relative_file(bundle.resolve(), overview["path"])
    return index


def _safe_relative_file(bundle: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise BundleError(f"unsafe bundle path: {relative!r}")
    candidate = (bundle / relative).resolve()
    try:
        candidate.relative_to(bundle.resolve())
    except ValueError as exc:
        raise BundleError(f"bundle path escapes root: {relative!r}") from exc
    if not candidate.is_file():
        raise BundleError(f"bundle chunk is missing: {relative}")
    return candidate


def _read_chunk(bundle: Path, entry: dict[str, Any]) -> list[Any]:
    if not isinstance(entry, dict):
        raise BundleError("bundle chunk entry must be an object")
    relative = entry.get("path")
    path = _safe_relative_file(bundle, relative)
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"invalid bundle chunk: {relative}") from exc
    if _sha256_bytes(payload) != entry.get("sha256"):
        raise BundleError(f"bundle chunk SHA-256 mismatch: {relative}")
    if not isinstance(value, list) or len(value) != entry.get("count"):
        raise BundleError(f"bundle chunk count mismatch: {relative}")
    return value


def _read_json_entry(bundle: Path, entry: dict[str, Any]) -> Any:
    if not isinstance(entry, dict):
        raise BundleError("bundle JSON entry must be an object")
    relative = entry.get("path")
    path = _safe_relative_file(bundle, relative)
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"invalid bundle JSON: {relative}") from exc
    if _sha256_bytes(payload) != entry.get("sha256"):
        raise BundleError(f"bundle JSON SHA-256 mismatch: {relative}")
    expected_count = entry.get("count")
    actual_count = len(value) if isinstance(value, list) else 1
    if actual_count != expected_count:
        raise BundleError(f"bundle JSON count mismatch: {relative}")
    return value


def _load_category(bundle: Path, index: dict[str, Any], category: str) -> list[Any]:
    entries = index["chunks"].get(category)
    if not isinstance(entries, list):
        raise BundleError(f"bundle index.chunks.{category} must be an array")
    values: list[Any] = []
    for entry in entries:
        values.extend(_read_chunk(bundle, entry))
    if len(values) != index["counts"].get(category):
        raise BundleError(f"bundle {category} count does not match index")
    return values


def validate_bundle(bundle: Path) -> dict[str, Any]:
    """Verify all referenced files and the reconstructed Contract v1 graph."""

    bundle = bundle.resolve()
    index = load_bundle_index(bundle)
    nodes = _load_category(bundle, index, "nodes")
    edges = _load_category(bundle, index, "edges")
    diagnostics = _load_category(bundle, index, "diagnostics")
    document = {
        "format": "connection-analysis-map",
        "schema_version": index["analysis_schema_version"],
        "meta": index["meta"],
        "nodes": nodes,
        "edges": edges,
        "diagnostics": diagnostics,
    }
    try:
        validate_document(document)
    except ContractError as exc:
        raise BundleError(f"reconstructed bundle graph is invalid: {exc}") from exc
    if canonical_sha256(document) != index["analysis_sha256"]:
        raise BundleError("bundle analysis_sha256 does not match reconstructed graph")

    overview_entry = index.get("overview")
    if overview_entry is not None:
        overview = _read_json_entry(bundle, overview_entry)
        edge_chunk_by_id = {
            edge["id"]: chunk_index
            for chunk_index, entry in enumerate(index["chunks"]["edges"])
            for edge in _read_chunk(bundle, entry)
            if isinstance(edge, dict) and isinstance(edge.get("id"), str)
        }
        _validate_overview(bundle, index, overview, nodes, edges, edge_chunk_by_id)

    node_count = len(nodes)
    search = index["search"]
    record_format = search.get("record_format")
    if search.get("normalization") != "nfkc-lower":
        raise BundleError("unsupported bundle search normalization")
    records_by_ordinal: dict[int, dict[str, Any]] = {}
    if record_format == "node_records":
        record_chunks = search.get("record_chunks")
        record_chunk_size = search.get("record_chunk_size")
        if (
            not isinstance(record_chunks, list)
            or not isinstance(record_chunk_size, int)
            or isinstance(record_chunk_size, bool)
            or record_chunk_size <= 0
        ):
            raise BundleError("bundle search.record_chunks must be an array")
        ordinal = 0
        for entry in record_chunks:
            records = _read_chunk(bundle, entry)
            if len(records) > record_chunk_size:
                raise BundleError("bundle search record chunk exceeds record_chunk_size")
            for record in records:
                if (
                    not isinstance(record, dict)
                    or not isinstance(record.get("ordinal"), int)
                    or isinstance(record.get("ordinal"), bool)
                    or not 0 <= record["ordinal"] < node_count
                    or record["ordinal"] != ordinal
                ):
                    raise BundleError("bundle search record ordinal does not match its chunk position")
                node = nodes[ordinal]
                expected_values = {
                    "id": str(node.get("id") or ""),
                    "kind": node.get("kind"),
                    "qualified_name": str(node.get("qualified_name") or ""),
                    "display_name": str(node.get("display_name") or ""),
                    "file": str(node.get("file") or ""),
                    "language": str((node.get("extensions") or {}).get("language") or ""),
                }
                if any(record.get(field) != value for field, value in expected_values.items()):
                    raise BundleError("bundle search record does not match its node")
                records_by_ordinal[ordinal] = record
                ordinal += 1
        if ordinal != node_count:
            raise BundleError("bundle search records do not cover all nodes")
    elif record_format != "node_ordinals":
        raise BundleError("unsupported bundle search.record_format")

    shards = search.get("shards")
    if not isinstance(shards, list):
        raise BundleError("bundle index.search.shards must be an array")
    for shard in shards:
        if (
            not isinstance(shard, dict)
            or not isinstance(shard.get("key"), str)
            or not isinstance(shard.get("chunks"), list)
            or not isinstance(shard.get("count"), int)
            or isinstance(shard.get("count"), bool)
        ):
            raise BundleError("invalid search shard entry")
        key = shard["key"]
        if not re.fullmatch(r"u[0-9a-f]+", key):
            raise BundleError("invalid search shard key")
        shard_codepoint = int(key[1:], 16)
        try:
            shard_character = chr(shard_codepoint)
        except ValueError as exc:
            raise BundleError("invalid search shard codepoint") from exc
        shard_ordinals: set[int] = set()
        for entry in shard["chunks"]:
            records = _read_chunk(bundle, entry)
            for record in records:
                if not isinstance(record, int) or isinstance(record, bool) or not 0 <= record < node_count:
                    raise BundleError("search shard references an unknown node")
                if record in shard_ordinals:
                    raise BundleError("search shard contains a duplicate node ordinal")
                shard_ordinals.add(record)
                if record_format == "node_records":
                    record_values = records_by_ordinal[record]
                    searchable = normalize_search_text("\n".join(
                        str(record_values.get(field) or "")
                        for field in ("id", "qualified_name", "display_name", "file")
                    ))
                    if shard_character not in searchable:
                        raise BundleError("search shard key does not match its node record")
        if len(shard_ordinals) != shard["count"]:
            raise BundleError("search shard count does not match its chunks")
    return index


def _validate_overview(
    bundle: Path,
    index: dict[str, Any],
    overview: Any,
    nodes: list[Any],
    edges: list[Any],
    edge_chunk_by_id: dict[str, int],
) -> None:
    if not isinstance(overview, dict) or overview.get("format") != "connection-analysis-overview":
        raise BundleError("invalid bundle overview format")
    if overview.get("schema_version") != "1.0":
        raise BundleError("invalid bundle overview schema_version")
    overview_modules = overview.get("modules")
    if not isinstance(overview_modules, list):
        raise BundleError("bundle overview.modules must be an array")
    expected_modules, _ = _presentation_modules(nodes, _module_by_node_id(nodes))
    expected_module_ids = {node["id"] for node in expected_modules}
    actual_module_ids = {node.get("id") for node in overview_modules if isinstance(node, dict)}
    if actual_module_ids != expected_module_ids or len(actual_module_ids) != len(overview_modules):
        raise BundleError("bundle overview modules do not match node chunks")
    node_ids = {node["id"] for node in nodes if isinstance(node, dict)}
    edge_ids = {edge["id"] for edge in edges if isinstance(edge, dict)}
    for module in overview_modules:
        if not isinstance(module, dict) or module.get("kind") != "module" or not isinstance(module.get("id"), str):
            raise BundleError("invalid bundle overview module")
        if (module.get("extensions") or {}).get("virtual") and module["id"] in node_ids:
            raise BundleError("virtual bundle overview module collides with a graph node")
    module_count = len(expected_module_ids)
    module_by_node = overview.get("module_by_node")
    if module_by_node is not None:
        if not isinstance(module_by_node, dict) or set(module_by_node) != {str(item) for item in range(len(nodes))}:
            raise BundleError("bundle overview.module_by_node must cover every node")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < module_count
            for value in module_by_node.values()
        ):
            raise BundleError("invalid bundle overview.module_by_node entry")
    for field, category in (
        ("node_chunks_by_module", "nodes"),
        ("edge_chunks_by_module", "edges"),
    ):
        mapping = overview.get(field)
        if not isinstance(mapping, dict):
            raise BundleError(f"bundle overview.{field} must be an object")
        max_index = len(index["chunks"][category])
        for module_index, chunk_indices in mapping.items():
            if (
                not isinstance(module_index, str)
                or not module_index.isdigit()
                or not 0 <= int(module_index) < module_count
                or not isinstance(chunk_indices, list)
            ):
                raise BundleError(f"invalid bundle overview.{field} entry")
            if any(not isinstance(item, int) or isinstance(item, bool) or not 0 <= item < max_index for item in chunk_indices):
                raise BundleError(f"invalid bundle overview.{field} chunk index")

    for field, max_index, expected_keys in (
        ("node_chunks_by_node", len(index["chunks"]["nodes"]), set(range(len(nodes)))),
        ("edge_chunks_by_node", len(index["chunks"]["edges"]), set(range(len(nodes)))),
    ):
        mapping = overview.get(field)
        if mapping is None:
            # Bundles produced before the node-local index remain readable.
            continue
        if not isinstance(mapping, dict) or set(mapping) != {str(item) for item in expected_keys}:
            raise BundleError(f"bundle overview.{field} must cover every node")
        for chunk_indices in mapping.values():
            if not isinstance(chunk_indices, list) or any(
                not isinstance(item, int) or isinstance(item, bool) or not 0 <= item < max_index
                for item in chunk_indices
            ):
                raise BundleError(f"invalid bundle overview.{field} chunk index")

    node_chunks_by_id = overview.get("node_chunks_by_id")
    if node_chunks_by_id is not None:
        if not isinstance(node_chunks_by_id, dict) or set(node_chunks_by_id) != node_ids:
            raise BundleError("bundle overview.node_chunks_by_id must cover every node")
        max_index = len(index["chunks"]["nodes"])
        for node_id, chunk_indices in node_chunks_by_id.items():
            if not isinstance(node_id, str) or not isinstance(chunk_indices, list) or any(
                not isinstance(item, int) or isinstance(item, bool) or not 0 <= item < max_index
                for item in chunk_indices
            ):
                raise BundleError("invalid bundle overview.node_chunks_by_id entry")
    group_chunks = overview.get("edge_group_chunks")
    group_mapping = overview.get("module_edge_chunks_by_module")
    if not isinstance(group_chunks, list) or not isinstance(group_mapping, dict):
        raise BundleError("invalid bundle overview edge group index")
    for module_index, chunk_indices in group_mapping.items():
        if (
            not isinstance(module_index, str)
            or not module_index.isdigit()
            or not 0 <= int(module_index) < module_count
            or not isinstance(chunk_indices, list)
        ):
            raise BundleError("invalid bundle overview.module_edge_chunks_by_module entry")
        if any(not isinstance(item, int) or isinstance(item, bool) or not 0 <= item < len(group_chunks) for item in chunk_indices):
            raise BundleError("invalid bundle overview edge group chunk index")
    for entry in group_chunks:
        records = _read_chunk(bundle, entry)
        for group in records:
            if (
                not isinstance(group, dict)
                or not isinstance(group.get("source"), int)
                or isinstance(group.get("source"), bool)
                or not isinstance(group.get("target"), int)
                or isinstance(group.get("target"), bool)
                or not 0 <= group["source"] < module_count
                or not 0 <= group["target"] < module_count
            ):
                raise BundleError("bundle overview edge group references an unknown module")
            representative = group.get("representative_edge_id")
            representative_chunk = group.get("representative_edge_chunk")
            if representative is not None and (
                not isinstance(representative, str)
                or not representative
                or not isinstance(representative_chunk, int)
                or isinstance(representative_chunk, bool)
                or not 0 <= representative_chunk < len(index["chunks"]["edges"])
            ):
                raise BundleError("invalid bundle overview representative edge")
            if representative is not None and representative not in edge_ids:
                raise BundleError("bundle overview representative edge does not exist")
            if representative is not None and edge_chunk_by_id.get(representative) != representative_chunk:
                raise BundleError("bundle overview representative edge chunk does not contain the edge")
            confidence_min = group.get("confidence_min")
            confidence_max = group.get("confidence_max")
            if (confidence_min is not None or confidence_max is not None) and (
                not isinstance(confidence_min, int | float)
                or isinstance(confidence_min, bool)
                or not isinstance(confidence_max, int | float)
                or isinstance(confidence_max, bool)
                or not 0 <= confidence_min <= confidence_max <= 1
            ):
                raise BundleError("invalid bundle overview confidence range")


def search_bundle(bundle: Path, query: str, *, limit: int = 80) -> list[dict[str, str]]:
    """Search a bundle without loading node and edge chunks."""

    if not isinstance(query, str) or not query.strip():
        return []
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise BundleError("search limit must be a positive integer")
    bundle = bundle.resolve()
    index = load_bundle_index(bundle)
    normalized_query = normalize_search_text(query.strip())
    first_key = _search_key(normalized_query[0])
    shard = next((item for item in index["search"].get("shards", []) if item.get("key") == first_key), None)
    if shard is None:
        return []
    candidate_indices: set[int] = set()
    for entry in shard.get("chunks", []):
        for record in _read_chunk(bundle, entry):
            if isinstance(record, int) and not isinstance(record, bool):
                candidate_indices.add(record)

    found: dict[str, dict[str, str]] = {}
    if index["search"].get("record_format") == "node_records":
        record_chunks = index["search"].get("record_chunks", [])
        for entry in record_chunks:
            for record in _read_chunk(bundle, entry):
                ordinal = record.get("ordinal") if isinstance(record, dict) else None
                if ordinal not in candidate_indices:
                    continue
                values = {
                    field: str(record.get(field) or "")
                    for field in ("id", "qualified_name", "display_name", "file")
                }
                haystack = "\n".join(values.values())
                if normalized_query in normalize_search_text(haystack):
                    found[values["id"]] = {
                        "id": values["id"],
                        "kind": str(record.get("kind") or "unknown"),
                        **values,
                    }
                    if len(found) >= limit:
                        return [found[key] for key in sorted(found)]
        return [found[key] for key in sorted(found)]

    node_index = 0
    for entry in index["chunks"]["nodes"]:
        for node in _read_chunk(bundle, entry):
            if not isinstance(node, dict) or node_index not in candidate_indices:
                node_index += 1
                continue
            values = {
                field: str(node.get(field) or "")
                for field in ("id", "qualified_name", "display_name", "file")
            }
            haystack = "\n".join(values.values())
            if normalized_query in normalize_search_text(haystack):
                found[node["id"]] = {"id": node["id"], "kind": node["kind"], **values}
            node_index += 1
    return [found[key] for key in sorted(found)[:limit]]
