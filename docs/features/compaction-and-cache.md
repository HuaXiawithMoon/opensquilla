# Compaction and Cache Continuity

Long agent sessions need context management. OpenSquilla uses compaction,
bounded history, tool-result projection, and cache-aware prompt placement to
keep long-running tasks moving.

Compaction is separate from memory. Memory is durable recall. Compaction is an
active-session continuity tool.

## What Compaction Does

When session history approaches the configured context budget, OpenSquilla can
compact older transcript entries into a durable summary and keep the recent
tail active.

Compaction input is divided on logical turn boundaries. A user request, its
tool calls and results, and the following assistant response stay together;
an unanswered request is recorded as open rather than answered by the
summarizer.

The goal is to preserve:

- user goal;
- current status;
- open steps;
- changed files and artifacts;
- known failures;
- important tool results;
- next action.

Compaction is not a guarantee that every old word remains model-visible.
Archived transcript rows remain searchable after compaction. An optional,
experimental anchor mode can also attach stable references to selected summary
claims so the agent can expand the exact archived source later.

## Recoverable Compaction Anchors (Experimental)

Enable the feature in Settings > Advanced or configure:

```toml
[compaction]
anchor_enabled = true
```

The setting is off by default and applies to subsequent compactions. When it is
enabled, summaries may contain references such as:

```text
[anchor:2:entry_017]
```

`session_search(anchor="2:entry_017")` resolves that reference inside the
current session and returns the exact archived entry. Keyword
`session_search(query="...")` remains the fallback when no suitable anchor is
available, including for Chinese transcript text.

Keyword search follows a web-search-like contract: natural-language phrases
and space-separated terms are accepted, exact/all-term matches rank first, and
the same call can relax an imperfect term instead of requiring repeated query
rewrites. In an active agent turn the scope defaults to archived entries from
the current session, so messages already present in live context are not echoed
back. Explicitly specifying a session ID retains the broader transcript-search
behavior. Results are bounded to five entries and 4,000 characters per call. A
turn may use at most two keyword searches, four anchor expansions, six calls
total, and 12,000 returned characters. Calls from the same session are executed
serially, and reordered duplicate queries or repeated anchors are blocked for
that turn. The aggregate character budget remains the final bound when
different queries rank the same evidence.

Retrieved snippets are a borrowed view of canonical transcript rows, not new
session facts. Both live compaction input and durable tool history project a
search result to a small receipt with source references instead of copying
snippets. Compaction excludes receipt-only entries from continuity extraction.
An answer based on a receipt reuses its source anchors for borrowed facts
instead of minting anchors for restatements. This keeps recovery one hop at the
storage boundary and prevents a search -> compaction -> search feedback loop
from multiplying old text.

Anchor lookup preserves three distinct outcomes:

- `resolved`: the exact archived row exists and was recovered;
- `declared_unavailable`: the summary declared the anchor, but its source row
  is unavailable;
- `unknown`: neither an exact archived row nor a declaration exists in the
  session. This may indicate a generated or cross-session reference, but is not
  by itself proof of hallucination.

Compaction remains liveness-first. If a durable anchor identity cannot be
established, compaction may continue without recoverable anchors rather than
claiming a reference that cannot be expanded. Anchors are selective, so export
sessions or save files when complete verbatim history is required.

## User-Visible Lifecycle

Depending on surface and trigger, users may see:

- compaction started;
- compaction skipped;
- compaction completed;
- compaction failed.

When no compaction is needed, OpenSquilla uses this stable message:

```text
Already within context budget; no compact was applied
```

That message is a no-op, not a failure.

## When to Compact Manually

Manual compaction is useful when:

- the session is long and you are about to start a new phase;
- a previous tool-heavy turn produced a lot of context;
- the UI indicates context pressure;
- you want the next answer to focus on the current state rather than the whole
  transcript.

Avoid compact loops when the runtime says the session is already within budget.

## Passive Compaction

Passive compaction can happen when OpenSquilla detects context pressure before
or during agent work. The exact trigger depends on model context limits,
configured budgets, current history, and tool output size.

If passive compaction fails, the safest user response is usually:

1. let the current turn finish or fail cleanly;
2. export the session if exact history matters;
3. retry with a narrower request or manually save key artifacts;
4. enable diagnostics if the failure repeats.

## Prompt Cache Continuity

Prompt caching works best when stable prompt parts stay stable. OpenSquilla
tries to keep:

- stable system prompt and tool definitions early;
- current request, volatile runtime context, retrieved history, and tool results
  near the tail;
- model/provider switches visible through diagnostics when they may affect
  cache continuity.

Cache continuity is best-effort. Routing, tools, attachments, provider changes,
or a large new context can reduce cache reuse.

`session_search` evidence is fully visible to the model during the retrieval
turn, then replaced by a source-reference receipt in later turns. That
intentional projection can invalidate the provider's volatile tail cache once
after a search. Earlier stable prompt segments remain reusable, while the
retrieved transcript text does not become permanent context growth.

## Related Commands and Surfaces

Manual compaction is primarily surfaced in chat and Web UI flows. For
inspection and recovery:

```sh
opensquilla sessions show <session-key>
opensquilla sessions export <session-key>
opensquilla diagnostics on
```

For memory repair surfaces related to degraded compaction records:

```sh
opensquilla memory repair list
opensquilla memory repair show --summary-id <id>
opensquilla memory raw-fallbacks list
```

## Best Practices

- Keep important final artifacts in files or published artifacts.
- Use memory for durable preferences and reusable project facts.
- Use session export for exact old transcripts.
- Use manual compaction before a new phase in a very long session.
- Do not repeatedly compact a short or already-within-budget session.

---

[Docs index](../README.md) · [Product guide](../../README.product.md) · [Improve this page](../contributing-docs.md) · [Report a docs issue](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml)
