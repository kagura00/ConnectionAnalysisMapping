from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
MANIFEST = PROJECT_ROOT / "tests" / "completeness" / "manifest.json"
EXTENDED_MANIFEST = PROJECT_ROOT / "tests" / "completeness" / "extended_manifest.json"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verify_completeness import verify_case, verify_manifest  # noqa: E402


def test_golden_completeness_covers_all_concrete_language_keys() -> None:
    reports = [
        verify_manifest(MANIFEST, PROJECT_ROOT),
        verify_manifest(EXTENDED_MANIFEST, PROJECT_ROOT),
    ]

    assert all(report["complete"] for report in reports), json.dumps(reports, ensure_ascii=False, indent=2)
    assert sum(len(report["cases"]) for report in reports) == 46
    for report in reports:
        assert all(not case["missing_nodes"] for case in report["cases"])
        assert all(not case["extra_nodes"] for case in report["cases"])
        assert all(not case["missing_edges"] for case in report["cases"])
        assert all(not case["blocked_diagnostics"] for case in report["cases"])


def test_completeness_verifier_reports_missing_and_extra_values() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    case = copy.deepcopy(manifest["cases"][0])
    case["expected_nodes"].pop()
    case["expected_nodes"].append(["python/main.py", "function", "not_present"])
    case["expected_edges"].pop()
    case["expected_edges"].append(
        [
            "calls",
            ["python/main.py", "function", "not_present"],
            ["python/main.py", "class", "Service"],
        ]
    )

    report = verify_case(PROJECT_ROOT, case)

    assert report["status"] == "incomplete"
    assert report["missing_nodes"]
    assert report["extra_nodes"]
    assert report["missing_edges"]
