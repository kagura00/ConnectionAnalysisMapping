from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
MANIFEST = PROJECT_ROOT / "tests" / "completeness" / "extended_manifest.json"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verify_completeness import verify_manifest  # noqa: E402


def test_extended_golden_completeness_covers_all_new_language_cases() -> None:
    report = verify_manifest(MANIFEST, PROJECT_ROOT)

    assert report["complete"], json.dumps(report, ensure_ascii=False, indent=2)
    assert len(report["cases"]) == 21
    assert all(not case["missing_nodes"] for case in report["cases"])
    assert all(not case["extra_nodes"] for case in report["cases"])
    assert all(not case["missing_edges"] for case in report["cases"])
    assert all(not case["extra_edges"] for case in report["cases"])
    assert all(not case["blocked_diagnostics"] for case in report["cases"])
