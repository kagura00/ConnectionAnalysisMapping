"""TypeScript/TSX analyzer adapter.

JavaScript-shaped definitions and relations are shared with the JavaScript
analyzer, while TypeScript-only declaration kinds remain explicit here.
"""

from __future__ import annotations

from .javascript_analyzer import collect_definitions as collect_javascript_definitions
from .javascript_analyzer import collect_relations as collect_javascript_relations
from .web_common import WebAnalysisContext, WebFile


def collect_definitions(context: WebAnalysisContext, web_file: WebFile) -> None:
    collect_javascript_definitions(context, web_file, typescript=True)


def collect_relations(context: WebAnalysisContext, web_file: WebFile) -> None:
    collect_javascript_relations(context, web_file, typescript=True)
