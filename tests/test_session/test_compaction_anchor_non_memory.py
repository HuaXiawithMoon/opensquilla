from __future__ import annotations

import json

import pytest

from opensquilla.session.compaction import (
    CompactionConfig,
    CompactionRequest,
    _format_chunk_for_llm,
    _sanitize_current_compaction_anchors,
    _summarize_chunk_fallback,
    _summarize_tool_calls_for_llm,
    compact_context,
    extract_anchors_from_summary,
)
from opensquilla.session.models import SessionNode, SessionSummary, TranscriptEntry
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.builtin.session_search import (
    _bound_payload,
    create_session_search_tool,
)
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolContext, current_tool_context


def _entry(
    session_id: str,
    session_key: str,
    role: str,
    content: str,
) -> TranscriptEntry:
    return TranscriptEntry(
        session_id=session_id,
        session_key=session_key,
        role=role,
        content=content,
    )


@pytest.mark.asyncio
async def test_fallback_compaction_emits_valid_recoverable_anchors() -> None:
    entries = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"message {index}",
            "token_count": 100,
        }
        for index in range(8)
    ]
    result = await compact_context(
        CompactionRequest(
            session_id="sid",
            entries=entries,
            context_window_tokens=400,
            config=CompactionConfig(anchor_enabled=True),
            compaction_index=3,
        )
    )

    assert result.removed_count > 0
    anchors = extract_anchors_from_summary(result.summary)
    assert anchors
    assert all(
        anchor["compaction_index"] == 3
        and int(anchor["entry_anchor_id"].removeprefix("entry_"))
        < result.removed_count
        for anchor in anchors
    )


@pytest.mark.asyncio
async def test_anchor_expands_exact_archived_entry_in_current_session(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "sessions.db"))
    await storage.connect()
    try:
        session_key = "agent:main:webchat:anchor"
        node = SessionNode(session_key=session_key, session_id="sid-anchor")
        await storage.upsert_session(node)
        summary = SessionSummary(
            session_id=node.session_id,
            session_key=session_key,
            summary_text="Decision [anchor:0:entry_000]",
        )
        await storage.rewrite_compacted_session(
            node=node,
            summary=summary,
            entries=[],
            archived_entries=[
                _entry(
                    node.session_id,
                    session_key,
                    "user",
                    "exact original decision",
                )
            ],
            anchor_enabled=True,
            expected_compaction_index=0,
        )

        registry = ToolRegistry()
        create_session_search_tool(storage, registry=registry)
        registered = registry.get("session_search")
        assert registered is not None
        token = current_tool_context.set(
            ToolContext(is_owner=True, session_key=session_key)
        )
        try:
            payload = json.loads(
                await registered.handler(anchor="0:entry_000")
            )
        finally:
            current_tool_context.reset(token)

        assert payload["anchor_resolution"] == "resolved"
        assert payload["results"][0]["snippet"] == "exact original decision"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_anchor_missing_states_do_not_collapse_unknown_into_unavailable(
    tmp_path,
) -> None:
    storage = SessionStorage(str(tmp_path / "sessions.db"))
    await storage.connect()
    try:
        session_key = "agent:main:webchat:missing-anchor"
        node = SessionNode(session_key=session_key, session_id="sid-missing-anchor")
        await storage.upsert_session(node)
        await storage.save_summary(
            SessionSummary(
                session_id=node.session_id,
                session_key=session_key,
                summary_text="Declared [anchor:0:entry_000]",
            )
        )
        registry = ToolRegistry()
        create_session_search_tool(storage, registry=registry)
        registered = registry.get("session_search")
        assert registered is not None

        declared = json.loads(
            await registered.handler(
                session_id=node.session_id,
                anchor="0:entry_000",
            )
        )
        unknown = json.loads(
            await registered.handler(
                session_id=node.session_id,
                anchor="0:entry_999",
            )
        )

        assert declared["anchor_resolution"] == "declared_unavailable"
        assert unknown["anchor_resolution"] == "unknown"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_chinese_query_searches_active_and_archived_transcripts(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "sessions.db"))
    await storage.connect()
    try:
        session_key = "agent:main:webchat:chinese"
        node = SessionNode(session_key=session_key, session_id="sid-chinese")
        await storage.upsert_session(node)
        await storage.rewrite_compacted_session(
            node=node,
            summary=SessionSummary(
                session_id=node.session_id,
                session_key=session_key,
                summary_text="压缩摘要",
            ),
            entries=[
                _entry(node.session_id, session_key, "assistant", "当前序度结论")
            ],
            archived_entries=[
                _entry(node.session_id, session_key, "user", "历史序度定义")
            ],
        )
        registry = ToolRegistry()
        create_session_search_tool(storage, registry=registry)
        registered = registry.get("session_search")
        assert registered is not None

        payload = json.loads(
            await registered.handler(
                query="序度",
                session_id=node.session_id,
                limit=5,
            )
        )

        assert {result["source"] for result in payload["results"]} == {
            "active",
            "archived",
        }
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_keyword_query_relaxes_one_imperfect_term_without_retry(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "sessions.db"))
    await storage.connect()
    try:
        session_key = "agent:main:webchat:relaxed"
        node = SessionNode(session_key=session_key, session_id="sid-relaxed")
        await storage.upsert_session(node)
        await storage.append_transcript_entry(
            _entry(
                node.session_id,
                session_key,
                "assistant",
                "The compaction anchor audit preserves source identity.",
            )
        )

        registry = ToolRegistry()
        create_session_search_tool(storage, registry=registry)
        registered = registry.get("session_search")
        assert registered is not None

        payload = json.loads(
            await registered.handler(
                query="compaction anchor nonexistentkeyword",
                session_id=node.session_id,
            )
        )

        assert payload["result_count"] == 1
        assert payload["results"][0]["match_mode"] == "relaxed"
        assert "compaction" in payload["results"][0]["snippet"].lower()
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_cjk_keyword_query_uses_minimum_should_match_ranking(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "sessions.db"))
    await storage.connect()
    try:
        session_key = "agent:main:webchat:cjk-relaxed"
        node = SessionNode(session_key=session_key, session_id="sid-cjk-relaxed")
        await storage.upsert_session(node)
        await storage.append_transcript_entry(
            _entry(
                node.session_id,
                session_key,
                "assistant",
                "\u538b\u7f29\u7684\u951a\u70b9\u5fc5\u987b\u4fdd\u7559\u539f\u59cb\u6765\u6e90\u8eab\u4efd\u3002",
            )
        )

        registry = ToolRegistry()
        create_session_search_tool(storage, registry=registry)
        registered = registry.get("session_search")
        assert registered is not None
        payload = json.loads(
            await registered.handler(
                query="\u538b\u7f29 \u951a\u70b9 \u4e0d\u5b58\u5728\u8bcd",
                session_id=node.session_id,
            )
        )

        assert payload["result_count"] == 1
        assert payload["results"][0]["match_mode"] == "relaxed"
        assert payload["results"][0]["matched_terms"] == [
            "\u538b\u7f29",
            "\u951a\u70b9",
        ]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_query_defaults_to_current_session_in_tool_context(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "sessions.db"))
    await storage.connect()
    try:
        current_key = "agent:main:webchat:current-scope"
        other_key = "agent:main:webchat:other-scope"
        current = SessionNode(session_key=current_key, session_id="sid-current")
        other = SessionNode(session_key=other_key, session_id="sid-other")
        await storage.upsert_session(current)
        await storage.upsert_session(other)
        historical_entry = _entry(
            current.session_id,
            current_key,
            "user",
            "scopeuniqueterm belongs to prior current-session evidence",
        )
        current_prompt = _entry(
            current.session_id,
            current_key,
            "user",
            "scopeuniqueterm is also present in the current question",
        )
        await storage.rewrite_compacted_session(
            node=current,
            summary=SessionSummary(
                session_id=current.session_id,
                session_key=current_key,
                summary_text="Prior current-session evidence was compacted.",
            ),
            entries=[current_prompt],
            archived_entries=[historical_entry],
            anchor_enabled=True,
            expected_compaction_index=0,
        )
        await storage.append_transcript_entry(
            _entry(
                other.session_id,
                other_key,
                "user",
                "scopeuniqueterm belongs to a different session",
            )
        )

        registry = ToolRegistry()
        create_session_search_tool(storage, registry=registry)
        registered = registry.get("session_search")
        assert registered is not None
        token = current_tool_context.set(
            ToolContext(is_owner=True, session_key=current_key)
        )
        try:
            payload = json.loads(
                await registered.handler(query="scopeuniqueterm")
            )
        finally:
            current_tool_context.reset(token)

        assert payload["searched_scope"] == "current_session_archive"
        assert {result["session_key"] for result in payload["results"]} == {
            current_key
        }
        assert {result["message_id"] for result in payload["results"]} == {
            historical_entry.message_id
        }
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_archived_query_result_carries_expandable_anchor(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "sessions.db"))
    await storage.connect()
    try:
        session_key = "agent:main:webchat:archived-query"
        node = SessionNode(session_key=session_key, session_id="sid-archived-query")
        await storage.upsert_session(node)
        await storage.rewrite_compacted_session(
            node=node,
            summary=SessionSummary(
                session_id=node.session_id,
                session_key=session_key,
                summary_text="Archived decision [anchor:0:entry_000]",
            ),
            entries=[],
            archived_entries=[
                _entry(
                    node.session_id,
                    session_key,
                    "assistant",
                    "The archived searchable decision is canonical.",
                )
            ],
            anchor_enabled=True,
            expected_compaction_index=0,
        )

        registry = ToolRegistry()
        create_session_search_tool(storage, registry=registry)
        registered = registry.get("session_search")
        assert registered is not None
        payload = json.loads(
            await registered.handler(
                query="archived searchable decision",
                session_id=node.session_id,
            )
        )

        assert payload["results"][0]["source"] == "archived"
        assert payload["results"][0]["anchor"] == "0:entry_000"
        assert payload["results"][0]["ref"] == "anchor:0:entry_000"
    finally:
        await storage.close()


def test_session_search_payload_enforces_hard_rendered_char_bound() -> None:
    payload = {
        "query": "q" * 10_000,
        "result_count": 4,
        "results": [
            {
                "ref": f"message:{index}",
                "session_key": "agent:main:webchat:bounded",
                "role": "assistant",
                "snippet": "x" * 10_000,
                "created_at": index,
            }
            for index in range(4)
        ],
    }

    bounded = _bound_payload(payload, 6_000)

    assert len(json.dumps(bounded, ensure_ascii=False, indent=2)) <= 6_000
    assert bounded["result_truncated"] is True
    assert len(bounded["query"]) <= 512


def test_invalid_current_anchor_is_removed_without_touching_prior_epoch() -> None:
    summary, removed = _sanitize_current_compaction_anchors(
        (
            "Earlier [anchor:2:entry_009] "
            "valid [anchor:3:entry_001] "
            "invented [anchor:3:entry_999]"
        ),
        compaction_index=3,
        removed_count=2,
    )

    assert removed == 1
    assert "[anchor:2:entry_009]" in summary
    assert "[anchor:3:entry_001]" in summary
    assert "[anchor:3:entry_999]" not in summary


def test_compaction_prompt_sees_session_search_receipt_not_retrieved_text() -> None:
    receipt = {
        "type": "tool_result",
        "tool_use_id": "call_search",
        "name": "session_search",
        "retrieval_receipt": True,
        "result": json.dumps(
            {
                "kind": "session_search_receipt",
                "result_count": 1,
                "refs": [{"anchor": "3:entry_007"}],
                "snippet": "BORROWED_SOURCE_TEXT_MUST_NOT_BE_RECOMPACTED",
            }
        ),
        "is_error": False,
    }

    rendered = _summarize_tool_calls_for_llm([receipt])

    assert "3:entry_007" in rendered
    assert "BORROWED_SOURCE_TEXT_MUST_NOT_BE_RECOMPACTED" not in rendered


def test_pure_session_search_receipt_cannot_mint_a_fallback_anchor() -> None:
    entry = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "type": "tool_result",
                "name": "session_search",
                "retrieval_receipt": True,
                "result": '{"refs":[{"anchor":"3:entry_007"}]}',
            }
        ],
    }

    prompt_input = _format_chunk_for_llm(
        [entry],
        include_anchors=True,
        anchor_base=4,
    )
    fallback = _summarize_chunk_fallback(
        [entry],
        "balanced",
        compaction_index=5,
        anchor_base=4,
    )

    assert "entry_004" not in prompt_input
    assert "[anchor:" not in fallback


def test_retrieval_derived_answer_reuses_source_anchor() -> None:
    entries = [
        {
            "role": "user",
            "content": "[Tool result (call_search): receipt]",
            "session_search_refs": ["3:entry_007"],
            "session_search_receipt_only": True,
        },
        {
            "role": "assistant",
            "content": "The earlier deployment used port 18797.",
        },
    ]

    prompt_input = _format_chunk_for_llm(
        entries,
        include_anchors=True,
        anchor_base=4,
    )
    fallback = _summarize_chunk_fallback(
        entries,
        "balanced",
        compaction_index=5,
        anchor_base=4,
    )

    assert "derived from session_search refs" in prompt_input
    assert "3:entry_007" in prompt_input
    assert "[derived-from:3:entry_007]" in fallback
    assert "[anchor:5:entry_005]" not in fallback
