"""Session search tool — FTS5-powered transcript full-text search.

Registered at boot time when a SessionStorage is available.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from opensquilla.session.compaction import extract_anchors_from_summary
from opensquilla.tools.registry import ToolRegistry, tool
from opensquilla.tools.types import PlanAccess, ToolError, current_tool_context

if TYPE_CHECKING:
    from opensquilla.session.storage import SessionStorage

logger = structlog.get_logger(__name__)

_storage: SessionStorage | None = None
_DEFAULT_RESULT_LIMIT = 5
_DEFAULT_MAX_CHARS = 4000
_MIN_RESULT_CHARS = 1000


class AnchorResolution(StrEnum):
    RESOLVED = "resolved"
    DECLARED_UNAVAILABLE = "declared_unavailable"
    UNKNOWN = "unknown"


async def _lookup_anchor(
    storage: SessionStorage,
    *,
    session_id: str,
    anchor: str,
    limit: int,
) -> tuple[AnchorResolution, list[dict[str, Any]]]:
    results = await storage.search_transcript(
        session_id=session_id,
        limit=limit,
        anchor=anchor,
    )
    if results:
        return AnchorResolution.RESOLVED, results

    compaction_index_text, entry_anchor_id = anchor.split(":", 1)
    compaction_index = int(compaction_index_text)
    declared = False
    for summary in await storage.get_all_summaries(session_id):
        anchors = [
            candidate
            for candidate in extract_anchors_from_summary(summary.summary_text)
            if candidate.get("compaction_index") == summary.compaction_index
            and (
                summary.removed_count <= 0
                or int(
                    str(candidate.get("entry_anchor_id", "")).removeprefix(
                        "entry_"
                    )
                )
                < summary.removed_count
            )
        ]
        if any(
            candidate.get("compaction_index") == compaction_index
            and candidate.get("entry_anchor_id") == entry_anchor_id
            for candidate in anchors
        ):
            declared = True
            break

    resolution = (
        AnchorResolution.DECLARED_UNAVAILABLE
        if declared
        else AnchorResolution.UNKNOWN
    )
    logger.warning(
        "session_search.anchor_resolution_obligation",
        session_id=session_id,
        anchor=anchor,
        resolution=resolution.value,
    )
    return resolution, []


def _result_ref(result: dict[str, Any]) -> str:
    anchor = result.get("anchor")
    if isinstance(anchor, str) and anchor:
        return f"anchor:{anchor}"
    message_id = result.get("message_id")
    if isinstance(message_id, str) and message_id:
        return f"message:{message_id}"
    return f"{result.get('source', 'unknown')}:{result.get('id', 'unknown')}"


def _bound_payload(payload: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Bound transcript excerpts without dropping retrieval identity metadata."""
    max_chars = max(_MIN_RESULT_CHARS, int(max_chars))
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(rendered) <= max_chars:
        return payload

    bounded = {
        key: value
        for key, value in payload.items()
        if key != "results"
    }
    for key, value in list(bounded.items()):
        if isinstance(value, str) and len(value) > 512:
            bounded[key] = value[:509] + "..."
        elif isinstance(value, list):
            bounded[key] = [
                item[:125] + "..."
                if isinstance(item, str) and len(item) > 128
                else item
                for item in value[:20]
            ]
    bounded["results"] = []
    bounded["result_count"] = 0
    bounded["result_truncated"] = True
    bounded["result_original_chars"] = len(rendered)

    for raw in payload.get("results") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        snippet = str(item.get("snippet") or "")
        for key, value in list(item.items()):
            if key != "snippet" and isinstance(value, str) and len(value) > 256:
                item[key] = value[:253] + "..."

        candidate = {**bounded, "results": [*bounded["results"], item]}
        candidate["result_count"] = len(candidate["results"])
        if len(json.dumps(candidate, ensure_ascii=False, indent=2)) <= max_chars:
            bounded = candidate
            continue

        item["snippet_truncated"] = True
        item["snippet_original_chars"] = len(snippet)
        low = 0
        high = len(snippet)
        best: dict[str, Any] | None = None
        while low <= high:
            midpoint = (low + high) // 2
            item["snippet"] = snippet[:midpoint]
            candidate = {**bounded, "results": [*bounded["results"], dict(item)]}
            candidate["result_count"] = len(candidate["results"])
            if len(json.dumps(candidate, ensure_ascii=False, indent=2)) <= max_chars:
                best = candidate
                low = midpoint + 1
            else:
                high = midpoint - 1
        if best is None:
            break
        bounded = best
        if len(str(bounded["results"][-1].get("snippet") or "")) < len(snippet):
            item["snippet_truncated"] = True
            break
    return bounded


def _render_payload(payload: dict[str, Any], max_chars: int) -> str:
    return json.dumps(
        _bound_payload(payload, max_chars),
        ensure_ascii=False,
        indent=2,
    )


def create_session_search_tool(
    storage: SessionStorage,
    *,
    registry: ToolRegistry | None = None,
) -> None:
    """Register session_search tool with the global registry."""
    global _storage
    _storage = storage
    active_storage = storage

    @tool(
        name="session_search",
        description=(
            "Full-text search across persisted session transcripts, including entries "
            "archived during compaction. Returns matching excerpts with session context. "
            "The query behaves like a web search string: natural-language phrases and "
            "space-separated keywords are ranked and relaxed automatically. In an active "
            "agent turn, query search defaults to compacted entries from the current "
            "session, avoiding messages that are already in the live context. "
            "Use when exact prior chat wording, transcript context, or code snippets "
            "from persisted sessions are needed. Ordinary recall should start with "
            "memory_search, which defaults to curated memory source files. To search "
            "indexed session snippets through memory_search, use source=sessions or "
            "source=all. Expand a compaction summary anchor by passing its "
            "'<compaction_index>:<entry_anchor_id>' value as anchor; the current "
            "session is selected automatically. Anchor lookup distinguishes resolved, "
            "declared-but-unavailable, and unknown references. session_search does not "
            "search MEMORY.md or memory/**/*.md."
        ),
        params={
            "query": {
                "type": "string",
                "description": "Search query - natural language terms to find in transcripts.",
            },
            "session_id": {
                "type": "string",
                "description": "Optional: restrict search to a specific session ID.",
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return (1-50, default 5).",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum total result characters (default 4000).",
            },
            "anchor": {
                "type": "string",
                "description": (
                    "Exact reference from a compaction summary, formatted "
                    "'<compaction_index>:entry_NNN'. Returns the original archived "
                    "entry. The active session is used unless session_id is supplied."
                ),
            },
        },
        required=[],
        owner_only=True,
        plan_access=PlanAccess.READ_ONLY,
        registry=registry,
    )
    async def session_search(
        query: str = "",
        session_id: str | None = None,
        limit: int = _DEFAULT_RESULT_LIMIT,
        max_chars: int = _DEFAULT_MAX_CHARS,
        anchor: str | None = None,
    ) -> str:
        if active_storage is None:
            raise ToolError("Session storage not available")

        if not query.strip() and anchor is None:
            raise ToolError("Query must not be empty (unless anchor is provided)")

        limit = max(1, min(50, limit))
        max_chars = max(_MIN_RESULT_CHARS, max_chars)
        if anchor is not None:
            parts = anchor.split(":", 1)
            if (
                len(parts) != 2
                or not parts[0].isdigit()
                or not parts[1].startswith("entry_")
                or not parts[1].removeprefix("entry_").isdigit()
            ):
                raise ToolError(
                    "Invalid anchor; expected '<compaction_index>:entry_NNN'"
                )

        resolved_session_id = session_id
        ctx = current_tool_context.get()
        implicit_current_archive = False
        if not resolved_session_id and ctx is not None and ctx.session_key:
            current_session = await active_storage.get_session(ctx.session_key)
            if current_session is None:
                raise ToolError(
                    "Active session context is unavailable; refusing a global search"
                )
            resolved_session_id = current_session.session_id
            implicit_current_archive = anchor is None
        if anchor is not None and not resolved_session_id:
            raise ToolError(
                "Anchor lookup requires an active session context or session_id"
            )

        try:
            anchor_resolution: AnchorResolution | None = None
            if anchor is not None:
                assert resolved_session_id is not None
                anchor_resolution, results = await _lookup_anchor(
                    active_storage,
                    session_id=resolved_session_id,
                    anchor=anchor,
                    limit=limit,
                )
            elif any(ord(char) > 127 for char in query):
                results = await active_storage.search_transcript_like(
                    query=query,
                    session_id=resolved_session_id,
                    limit=limit,
                    include_active=not implicit_current_archive,
                )
            else:
                results = await active_storage.search_transcript(
                    query=query,
                    session_id=resolved_session_id,
                    limit=limit,
                    include_active=not implicit_current_archive,
                )
        except Exception as exc:
            logger.warning("session_search.error", query=query[:80], error=str(exc))
            return _render_payload(
                {"query": query, "results": [], "error": "Search failed"},
                max_chars,
            )

        if not results:
            if anchor_resolution is AnchorResolution.DECLARED_UNAVAILABLE:
                return _render_payload(
                    {
                        "anchor": anchor,
                        "anchor_resolution": anchor_resolution.value,
                        "results": [],
                        "note": (
                            "This anchor was declared by the session, but its "
                            "archived source is unavailable."
                        ),
                    },
                    max_chars,
                )
            if anchor_resolution is AnchorResolution.UNKNOWN:
                return _render_payload(
                    {
                        "anchor": anchor,
                        "anchor_resolution": anchor_resolution.value,
                        "results": [],
                        "note": (
                            "This anchor is not declared by the session. It may "
                            "be model-generated or belong to another session."
                        ),
                    },
                    max_chars,
                )
            return _render_payload(
                {
                    "query": query,
                    "results": [],
                    "note": "No matches found.",
                },
                max_chars,
            )

        payload = _bound_payload(
            {
                "query": query,
                **(
                    {"anchor_resolution": anchor_resolution.value}
                    if anchor_resolution is not None
                    else {}
                ),
                "searched_scope": (
                    "current_session_archive"
                    if implicit_current_archive
                    else (
                        "specified_session"
                        if resolved_session_id is not None
                        else "all_sessions"
                    )
                ),
                "query_terms": query.split()[:20],
                "result_count": len(results),
                "results": [
                    {
                        "ref": _result_ref(r),
                        "message_id": r.get("message_id"),
                        "session_key": r["session_key"],
                        "role": r["role"],
                        "snippet": r["snippet"],
                        "created_at": r["created_at"],
                        "source": r.get("source", "active"),
                        "anchor": r.get("anchor"),
                        "matched_terms": r.get("matched_terms"),
                        "match_mode": r.get("match_mode"),
                    }
                    for r in results
                ],
            },
            max_chars,
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    logger.info("session_search_tool.registered")
