from __future__ import annotations

import pytest

from opensquilla.result_budget import (
    DEFAULT_TOOL_RUN_BUDGET_POLICY,
    DuplicateRetrievalInFlightError,
    TerminalRetrievalReplayError,
    ToolRunBudgetExceededError,
    ToolRunBudgetPolicy,
    ToolRunBudgetTracker,
    clamp_tool_arguments,
)


def test_default_session_search_budget_is_small_and_hard_bounded() -> None:
    policy = DEFAULT_TOOL_RUN_BUDGET_POLICY

    assert policy.max_session_search_calls_per_turn == 6
    assert policy.max_session_search_query_calls_per_turn == 2
    assert policy.max_session_search_anchor_calls_per_turn == 4
    assert policy.max_session_search_chars_per_turn == 12_000
    assert policy.max_session_search_chars_per_call == 4_000


@pytest.mark.asyncio
async def test_web_search_counts_as_external_search() -> None:
    tracker = ToolRunBudgetTracker()

    reservation = await tracker.reserve_tool_call(
        tool_name="web_search",
        arguments={"query": "python release", "max_results": 10, "fetch_top_k": 3},
    )
    await tracker.commit_tool_result(reservation, "x" * 100)
    snapshot = await tracker.snapshot()

    assert reservation.counted_as_search is True
    assert reservation.counted_as_fetch is False
    assert reservation.counted_as_external_text is True
    assert snapshot["web_search_calls_used"] == 1
    assert snapshot["web_fetch_calls_used"] == 0
    assert snapshot["external_text_chars_used"] == 100


@pytest.mark.asyncio
async def test_web_discover_counts_as_external_search_budget() -> None:
    tracker = ToolRunBudgetTracker(ToolRunBudgetPolicy(max_web_search_calls_per_turn=1))

    reservation = await tracker.reserve_tool_call(
        tool_name="web_discover",
        arguments={"query": "python release", "max_results": 5},
    )
    snapshot = await tracker.snapshot()

    assert reservation.counted_as_search is True
    assert reservation.counted_as_fetch is False
    assert snapshot["web_search_calls_used"] == 1

    with pytest.raises(ToolRunBudgetExceededError) as exc_info:
        await tracker.reserve_tool_call(
            tool_name="web_search",
            arguments={"query": "another search"},
        )

    assert exc_info.value.tool_name == "web_search"


def test_web_clamps_leave_bool_arguments_for_validation() -> None:
    search_args = {
        "query": "q",
        "max_results": True,
        "fetch_top_k": True,
        "max_chars_per_source": False,
    }
    discover_args = {"query": "q", "max_results": True}
    fetch_args = {"url": "https://example.com", "max_chars": False}

    assert (
        clamp_tool_arguments(
            "web_search",
            search_args,
            ToolRunBudgetPolicy(max_web_search_results=8),
        )
        == search_args
    )
    assert (
        clamp_tool_arguments(
            "web_discover",
            discover_args,
            ToolRunBudgetPolicy(max_web_search_results=8),
        )
        == discover_args
    )
    assert (
        clamp_tool_arguments(
            "web_fetch",
            fetch_args,
            ToolRunBudgetPolicy(max_single_fetch_chars=900),
        )
        == fetch_args
    )


def test_web_search_clamps_source_backed_arguments() -> None:
    clamped = clamp_tool_arguments(
        "web_search",
        {
            "query": "q",
            "max_results": 1000,
            "fetch_top_k": 1000,
            "max_chars_per_source": 1000,
        },
        ToolRunBudgetPolicy(
            max_web_search_results=8,
            max_web_search_fetch_top_k=2,
            max_web_search_chars_per_source=900,
        ),
    )

    assert clamped == {
        "query": "q",
        "max_results": 8,
        "fetch_top_k": 2,
        "max_chars_per_source": 900,
    }


def test_session_search_clamps_result_count_and_text_budget() -> None:
    clamped = clamp_tool_arguments(
        "session_search",
        {"query": "compaction anchor", "limit": 100, "max_chars": 100_000},
        ToolRunBudgetPolicy(
            max_session_search_results=5,
            max_session_search_chars_per_call=6_000,
        ),
    )

    assert clamped == {
        "query": "compaction anchor",
        "limit": 5,
        "max_chars": 6_000,
    }


@pytest.mark.asyncio
async def test_session_search_reordered_keywords_are_one_query_identity() -> None:
    tracker = ToolRunBudgetTracker()
    first = await tracker.reserve_tool_call(
        tool_name="session_search",
        arguments={"query": "  Compaction   Anchor  "},
    )
    await tracker.commit_tool_result(first, '{"results": []}')

    with pytest.raises(ToolRunBudgetExceededError) as exc_info:
        await tracker.reserve_tool_call(
            tool_name="session_search",
            arguments={"query": "anchor compaction"},
        )

    assert exc_info.value.tool_name == "session_search"
    assert "repeated query or anchor" in str(exc_info.value)


@pytest.mark.asyncio
async def test_session_search_query_calls_have_a_separate_turn_cap() -> None:
    tracker = ToolRunBudgetTracker(
        ToolRunBudgetPolicy(
            max_session_search_calls_per_turn=8,
            max_session_search_query_calls_per_turn=2,
            max_session_search_chars_per_turn=20_000,
        )
    )
    for query in ("first evidence", "second evidence"):
        reservation = await tracker.reserve_tool_call(
            tool_name="session_search",
            arguments={"query": query},
        )
        await tracker.commit_tool_result(reservation, '{"results": []}')

    with pytest.raises(ToolRunBudgetExceededError):
        await tracker.reserve_tool_call(
            tool_name="session_search",
            arguments={"query": "third evidence"},
        )

    snapshot = await tracker.snapshot()
    assert snapshot["session_search_calls_used"] == 2
    assert snapshot["session_search_query_calls_used"] == 2
    assert snapshot["session_search_anchor_calls_used"] == 0


@pytest.mark.asyncio
async def test_session_search_aggregate_text_budget_clamps_later_calls() -> None:
    tracker = ToolRunBudgetTracker(
        ToolRunBudgetPolicy(
            max_session_search_chars_per_turn=7_000,
            max_session_search_chars_per_call=6_000,
        )
    )
    first = await tracker.reserve_tool_call(
        tool_name="session_search",
        arguments={"query": "first"},
    )
    assert first.arguments["max_chars"] == 6_000
    await tracker.commit_tool_result(first, "x" * 5_000)

    second = await tracker.reserve_tool_call(
        tool_name="session_search",
        arguments={"query": "second"},
    )
    assert second.arguments["max_chars"] == 2_000
    await tracker.commit_tool_result(second, "x" * 2_000)

    with pytest.raises(ToolRunBudgetExceededError):
        await tracker.reserve_tool_call(
            tool_name="session_search",
            arguments={"query": "third"},
        )

    snapshot = await tracker.snapshot()
    assert snapshot["session_search_chars_used"] == 7_000
    assert snapshot["session_search_chars_reserved"] == 0


@pytest.mark.asyncio
async def test_loop_guard_blocks_repeated_identical_web_search() -> None:
    tracker = ToolRunBudgetTracker(
        ToolRunBudgetPolicy(max_repeated_retrievals_per_turn=2)
    )

    first = await tracker.reserve_tool_call(
        tool_name="web_search",
        arguments={
            "query": "  Python   Release  ",
            "provider": "tavily",
            "mode": "auto",
        },
    )
    await tracker.commit_tool_result(first, '{"ok": true, "results": []}')
    second = await tracker.reserve_tool_call(
        tool_name="web_search",
        arguments={"query": "python release", "provider": "tavily", "mode": "auto"},
    )
    await tracker.commit_tool_result(second, '{"ok": true, "results": []}')

    with pytest.raises(ToolRunBudgetExceededError) as exc_info:
        await tracker.reserve_tool_call(
            tool_name="web_search",
            arguments={
                "query": "PYTHON RELEASE",
                "provider": "tavily",
                "mode": "auto",
            },
        )

    assert exc_info.value.tool_name == "web_search"
    assert "repeated retrieval" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_loop_guard_counts_web_search_repeated_queries_too() -> None:
    tracker = ToolRunBudgetTracker(
        ToolRunBudgetPolicy(max_repeated_retrievals_per_turn=1)
    )

    first = await tracker.reserve_tool_call(
        tool_name="web_search",
        arguments={"query": "OpenSquilla"},
    )
    await tracker.commit_tool_result(first, '{"ok": true, "results": []}')

    with pytest.raises(ToolRunBudgetExceededError):
        await tracker.reserve_tool_call(
            tool_name="web_search",
            arguments={"query": " opensquilla "},
        )


@pytest.mark.asyncio
async def test_loop_guard_counts_web_discover_repeated_queries_too() -> None:
    tracker = ToolRunBudgetTracker(
        ToolRunBudgetPolicy(max_repeated_retrievals_per_turn=1)
    )

    first = await tracker.reserve_tool_call(
        tool_name="web_discover",
        arguments={"query": "OpenSquilla"},
    )
    await tracker.commit_tool_result(first, '{"ok": true, "results": []}')

    with pytest.raises(ToolRunBudgetExceededError):
        await tracker.reserve_tool_call(
            tool_name="web_discover",
            arguments={"query": " opensquilla "},
        )


@pytest.mark.asyncio
async def test_loop_guard_snapshot_exposes_counts() -> None:
    tracker = ToolRunBudgetTracker()

    await tracker.reserve_tool_call(
        tool_name="web_search",
        arguments={
            "query": "Python Release",
            "provider": "tavily",
            "mode": "news",
        },
    )
    snapshot = await tracker.snapshot()

    assert snapshot["retrieval_loop_guard"] == [
        {
            "tool_name": "web_search",
            "query": "python release",
            "provider": "tavily",
            "mode": "news",
            "count": 1,
        }
    ]


@pytest.mark.asyncio
async def test_semantic_guard_blocks_duplicate_retrieval_while_first_is_in_flight() -> None:
    tracker = ToolRunBudgetTracker()
    first = await tracker.reserve_tool_call(
        tool_name="web_search",
        arguments={
            "query": "  Python   Release ",
            "mode": "news",
            "recency": "week",
            "include_domains": ["Example.COM", "docs.example.com"],
            "provider": "tavily",
            "max_results": 3,
        },
    )

    with pytest.raises(DuplicateRetrievalInFlightError) as exc_info:
        await tracker.reserve_tool_call(
            tool_name="web_discover",
            arguments={
                "query": "python release",
                "mode": "news",
                "recency": "week",
                "include_domains": ["docs.example.com", "example.com"],
                "provider": "duckduckgo",
                "max_results": 10,
                "fetch_top_k": 0,
            },
        )

    assert exc_info.value.tool_name == "web_discover"
    await tracker.abort_tool_result(first)

    replacement = await tracker.reserve_tool_call(
        tool_name="web_discover",
        arguments={
            "query": "python release",
            "mode": "news",
            "recency": "week",
            "include_domains": ["example.com", "docs.example.com"],
        },
    )
    assert replacement.counted_as_search is True


@pytest.mark.asyncio
async def test_semantic_guard_replays_terminal_non_retryable_failure() -> None:
    tracker = ToolRunBudgetTracker()
    reservation = await tracker.reserve_tool_call(
        tool_name="web_search",
        arguments={
            "query": "OpenSquilla release",
            "mode": "auto",
            "exclude_domains": ["EXAMPLE.com"],
            "provider": "tavily",
            "max_results": 3,
        },
    )
    await tracker.commit_tool_result(
        reservation,
        '{"ok": false, "error_kind": "auth", "retry_allowed": false}',
    )

    with pytest.raises(TerminalRetrievalReplayError) as exc_info:
        await tracker.reserve_tool_call(
            tool_name="web_discover",
            arguments={
                "query": " opensquilla   RELEASE ",
                "exclude_domains": ["example.com"],
                "provider": "duckduckgo",
                "max_results": 10,
            },
        )

    assert exc_info.value.tool_name == "web_discover"
    assert exc_info.value.error_kind == "auth"


@pytest.mark.asyncio
async def test_semantic_guard_does_not_ledger_success_or_retryable_failure() -> None:
    tracker = ToolRunBudgetTracker(
        ToolRunBudgetPolicy(max_repeated_retrievals_per_turn=None)
    )
    first = await tracker.reserve_tool_call(
        tool_name="web_search",
        arguments={"query": "OpenSquilla"},
    )
    await tracker.commit_tool_result(first, '{"ok": true, "results": []}')

    second = await tracker.reserve_tool_call(
        tool_name="web_search",
        arguments={"query": "opensquilla", "provider": "duckduckgo"},
    )
    await tracker.commit_tool_result(
        second,
        '{"ok": false, "error_kind": "network", "retry_allowed": true}',
    )

    third = await tracker.reserve_tool_call(
        tool_name="web_search",
        arguments={"query": "opensquilla"},
    )
    assert third.counted_as_search is True


@pytest.mark.asyncio
async def test_semantic_guard_key_includes_mode_recency_and_domain_filters() -> None:
    base = {
        "query": "OpenSquilla",
        "mode": "auto",
        "recency": "week",
        "include_domains": ["example.com"],
        "exclude_domains": ["blocked.example"],
    }

    for changed in (
        {**base, "query": "OpenSquilla docs"},
        {**base, "mode": "technical"},
        {**base, "recency": "month"},
        {**base, "include_domains": ["docs.example.com"]},
        {**base, "exclude_domains": ["other.example"]},
    ):
        tracker = ToolRunBudgetTracker()
        terminal = await tracker.reserve_tool_call(tool_name="web_search", arguments=base)
        await tracker.commit_tool_result(
            terminal,
            '{"ok": false, "error_kind": "blocked", "retry_allowed": false}',
        )

        allowed = await tracker.reserve_tool_call(
            tool_name="web_search",
            arguments=changed,
        )
        assert allowed.counted_as_search is True


@pytest.mark.asyncio
async def test_semantic_guard_isolated_between_trackers() -> None:
    first_turn = ToolRunBudgetTracker()
    reservation = await first_turn.reserve_tool_call(
        tool_name="web_search",
        arguments={"query": "OpenSquilla"},
    )
    await first_turn.commit_tool_result(
        reservation,
        '{"ok": false, "error_kind": "blocked", "retry_allowed": false}',
    )

    next_turn = ToolRunBudgetTracker()
    allowed = await next_turn.reserve_tool_call(
        tool_name="web_search",
        arguments={"query": "OpenSquilla"},
    )

    assert allowed.counted_as_search is True
