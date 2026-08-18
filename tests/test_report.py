from __future__ import annotations

import json
from pathlib import Path

from connection_map.report import summarize_document


def test_report_contains_resolution_and_diagnostic_rates() -> None:
    path = Path(__file__).parents[1] / "examples" / "analysis-v1.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    report = summarize_document(document)
    assert report["counts"]["nodes"] == 2
    assert report["counts"]["edges_by_relation"] == {"contains": 1}
    assert report["rates"]["unresolved_or_unsupported_edge_rate"] == 0.0
    assert report["rates"]["parse_error_file_count"] == 0
