# RSS Query Patterns

## When to use each tool

### `search_my_feeds_timeline`
Use for:
- `how did this evolve over the weekend`
- `what changed from Friday to Sunday`
- `walk me through this by day`

Recommended defaults:
- keep the date window explicit
- start with `max_results_per_day=6`
- start with `max_total_results=18-30`
- expand once only if the first batch is clearly too thin

### `search_my_feeds`
Use for:
- `latest news on Iran`
- `find the strongest recent stories on X`
- topical gap-filling after a timeline search

Recommended defaults:
- start with `max_results=10-20`
- use a bounded `days_back`
- narrow the topic before increasing the batch size

### `list_recent_feed_items`
Use for:
- newest headlines
- latest feed items after a given refresh window
- recency-oriented browsing, not topic reconstruction

## Query refinement patterns

### Weak broad query
- `Iran`

### Better event-focused query
- `Iran talks`
- `US-Iran talks`
- `Islamabad talks Iran`
- `Iran ceasefire talks`

When the first broad query includes too many side stories:
- narrow by the event
- do not just raise the result limit

## Stopping rule

Stop searching when:
- you have usable items across the requested days, or
- one narrower follow-up query has already improved the batch, or
- another search would only raise limits on the same query/window

At that point, synthesize from the strongest retrieved results already in hand.

## Output example

Use a structure like:

```markdown
Weekend summary:
The weekend moved from preparation for direct US-Iran talks on Friday, to fragile negotiations on Saturday, to a breakdown and renewed escalation on Sunday.

Friday, April 10:
- BBC: talks framed as historically significant but burdened by distrust. [Link](https://example.com)
- Al Jazeera: Iranian delegation arrived in Islamabad and security tightened around the talks. [Link](https://example.com)

Saturday, April 11:
- Reuters: talks paused with disagreements unresolved. [Link](https://example.com)
- Al Jazeera: both sides signaled leverage and conflicting expectations. [Link](https://example.com)

Sunday, April 12:
- Reuters and Al Jazeera: talks ended without a deal and the ceasefire looked more fragile. [Link](https://example.com)

Cautious inference:
The strongest pattern in your feeds is that momentum shifted from guarded diplomacy to visible breakdown by Sunday.
```

## Quality checks

Before finishing, confirm:
- the answer is written to the user, not as internal process notes
- the strongest claims have direct links
- chronology is based on retrieved items, not guessed from later aftermath coverage
