"""The retrieval protocol: what a model is allowed to ask for, and in what order.

Each level is more expensive than the last, so each is a separate call. The
package exports them at the same level of prominence deliberately - nothing
here chains them, because choosing to escalate is the caller's decision to
make and to account for.
"""

from .insights import Insight, Severity, find_insights
from .search import Candidate, Match, search
from .source import SourceSlice, get_source
from .structure import FileStructure, SymbolOutline, build_structure, render
from .summary import FileSummary, ProjectSummary, build_summary

__all__ = [
    "Candidate",
    "FileStructure",
    "FileSummary",
    "Insight",
    "Match",
    "ProjectSummary",
    "Severity",
    "SourceSlice",
    "SymbolOutline",
    "build_structure",
    "build_summary",
    "find_insights",
    "get_source",
    "render",
    "search",
]
