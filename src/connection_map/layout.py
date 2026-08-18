"""Validation helpers for the repository-local layout format."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


class LayoutError(ValueError):
    """Raised when a layout document violates Layout v1."""


def _finite_number(value: Any, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise LayoutError(f"{name} must be a finite number")


def validate_layout(document: Any, *, analysis_schema_version: str = "1.0") -> None:
    """Validate the stable fields of a Layout v1 document."""

    if not isinstance(document, dict):
        raise LayoutError("layout must be an object")
    if document.get("format") != "connection-analysis-layout":
        raise LayoutError("layout.format must be connection-analysis-layout")
    if document.get("schema_version") != "1.0":
        raise LayoutError("layout.schema_version must be 1.0")
    declared_analysis_version = document.get("analysis_schema_version")
    if declared_analysis_version is not None and declared_analysis_version != analysis_schema_version:
        raise LayoutError(
            f"layout.analysis_schema_version must be {analysis_schema_version}"
        )

    camera = document.get("camera")
    if camera is not None:
        if not isinstance(camera, dict):
            raise LayoutError("layout.camera must be an object")
        for field in ("x", "y", "zoom"):
            _finite_number(camera.get(field), f"layout.camera.{field}")
        if camera["zoom"] <= 0:
            raise LayoutError("layout.camera.zoom must be greater than zero")

    nodes = document.get("nodes")
    if nodes is not None:
        if not isinstance(nodes, dict):
            raise LayoutError("layout.nodes must be an object")
        for node_id, position in nodes.items():
            if not isinstance(node_id, str) or not node_id:
                raise LayoutError("layout.nodes keys must be non-empty strings")
            if not isinstance(position, dict):
                raise LayoutError(f"layout.nodes[{node_id!r}] must be an object")
            _finite_number(position.get("x"), f"layout.nodes[{node_id!r}].x")
            _finite_number(position.get("y"), f"layout.nodes[{node_id!r}].y")

    annotations = document.get("annotations")
    if annotations is not None and not isinstance(annotations, list):
        raise LayoutError("layout.annotations must be an array")


def load_layout(path: Path, *, analysis_schema_version: str = "1.0") -> dict[str, Any]:
    """Load and validate a layout JSON file."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LayoutError(f"invalid layout JSON: {path}") from exc
    validate_layout(document, analysis_schema_version=analysis_schema_version)
    return document
