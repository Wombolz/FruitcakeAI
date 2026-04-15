---
name: RSS News Research
description: Use Fruitcake RSS tools to answer current-events questions with grounded summaries and timelines. Choose the right RSS retrieval mode, avoid repetitive re-searching, and synthesize from retrieved evidence instead of narrating tool use.
required_tools:
  - search_my_feeds
  - search_my_feeds_timeline
  - list_recent_feed_items
---

# RSS News Research

Use this skill when the user wants a source-grounded answer from their subscribed RSS feeds, especially for:

- latest developments on a topic
- chronology questions over a time window
- short source-backed news summaries

Prefer the shared RSS retrieval tools over ad hoc searching. The main job is to pick the right tool, stop at the right time, and synthesize clearly.

## Tool Selection

Use `search_my_feeds_timeline` when the user asks:

- how something evolved over a period
- what changed from one day to another
- for a weekend recap or timeline

Use `search_my_feeds` when the user asks:

- what the latest developments are on a topic
- for a topical summary without a strict chronology requirement
- for one targeted follow-up after a timeline search is noisy or incomplete

Use `list_recent_feed_items` when the user asks:

- what is newest overall
- what happened since a recent point in time
- for fresh headlines rather than topic retrieval

## Retrieval Workflow

1. Start with one strong retrieval pass.
2. Inspect whether the results already support an answer.
3. If yes, stop searching and synthesize.
4. If no, do at most one narrower follow-up query.
5. After that, answer from the strongest evidence already retrieved.

Do not keep increasing result limits repeatedly just to chase completeness.

## Query Strategy

For timeline questions:

- start with `search_my_feeds_timeline`
- use the clearest topical query, not the broadest possible query
- prefer `US-Iran talks` over just `Iran` when the topic is diplomacy
- keep the date window explicit

For topical questions:

- start with `search_my_feeds`
- use the core topic terms only
- avoid boolean syntax like `OR`, `AND`, or `site:...`

If the first result set is noisy:

- narrow the topic
- do not simply raise `max_total_results` again and again

## Stop Rules

Stop searching and write the answer when any of these are true:

- timeline retrieval already returned usable results across the requested days
- topical search returned several clearly relevant items from trusted sources
- another search would only widen limits without changing the topic or date window

If evidence is incomplete:

- say so directly
- summarize what is supported
- clearly label any cautious inference

## Output Rules

The final answer should:

- lead with the answer, not the search process
- separate verified facts from inference when needed
- cite concrete retrieved items in plain language
- stay concise unless the user asks for depth

Never:

- narrate internal tool usage
- mention tool names in the answer
- include tool-call scaffolding or function syntax
- pretend a timeline is complete when coverage is thin

## Answer Shapes

For a timeline question:

- one-sentence overview
- day-by-day bullets
- short inference section only if needed

For a latest-topic question:

- short overview
- 3 to 5 strongest developments
- brief note on source distribution or limits if relevant

See [rss-query-strategy.md](./references/rss-query-strategy.md) for concrete patterns and examples.
