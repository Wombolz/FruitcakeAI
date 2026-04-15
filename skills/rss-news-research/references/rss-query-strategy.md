# RSS Query Strategy

This reference supports the `rss-news-research` skill with concrete retrieval patterns.

## Choosing The First Query

### Chronology / Weekend / "How did this evolve?"

Use `search_my_feeds_timeline`.

Good:

- `US-Iran talks`
- `Islamabad talks Iran`
- `Trump Iran talks`

Too broad:

- `Iran`
- `war`
- `Middle East`

If the topic is clearly about diplomacy, negotiations, ceasefire talks, or a named event, include that in the query from the start.

### Latest Developments

Use `search_my_feeds`.

Good:

- `US-Iran talks`
- `Iran peace talks`
- `Hormuz blockade Iran`

Avoid:

- `Iran OR Tehran OR IRGC`
- quoted boolean constructions
- `site:news` style syntax

## Follow-Up Query Rules

Use at most one follow-up query when:

- the first timeline set is noisy because the topic was too broad
- the first topical search missed the obvious event framing

Preferred follow-up moves:

- narrow from `Iran` to `US-Iran talks`
- narrow from `trade` to `China tariffs`
- narrow from a person name to the actual event or policy

Do not:

- repeatedly raise `max_total_results`
- repeatedly raise `max_results_per_day`
- re-run the same date-bounded query with only tuning changes

## Timeline Answer Template

Use this structure:

1. `Overview`
   A one-sentence summary of what changed across the window.

2. `By day`
   One bullet set per day with the strongest retrieved developments.

3. `What we can infer`
   Only if needed, and label it as inference rather than confirmed fact.

Example skeleton:

- `Overview:` Talks advanced into direct negotiations over the weekend, then stalled on Sunday without a final deal.
- `Friday:` Delegations arrived, expectations were set, and lead-up coverage focused on distrust and the stakes of the talks.
- `Saturday:` Negotiations were underway, but reporting emphasized unresolved disagreements and mixed public messaging.
- `Sunday:` Multiple outlets reported that talks ended without a deal and attention shifted to possible next steps and escalation.
- `Inference:` The weekend arc appears to be movement from setup, to active negotiation, to breakdown without resolution.

## Latest-Topic Answer Template

Use this structure:

1. `Top line`
2. `Key developments`
3. `Coverage limits` if needed

Example skeleton:

- `Top line:` Your feeds show that the talks remain active but are under pressure from unresolved sanctions and shipping concerns.
- `Key developments:`
  - Reuters reported ...
  - BBC reported ...
  - NPR reported ...
- `Coverage limits:` Most of the strongest items are from the last 24 hours; there is less direct reporting from regional outlets in this batch.

## When To Stop

Stop and synthesize when:

- you have at least a few clearly relevant articles for the requested period
- you can already explain the arc of the story
- the next search would mostly broaden volume rather than improve relevance

When coverage is genuinely thin:

- say that clearly
- summarize the evidence you do have
- avoid filling the gap with confident speculation
