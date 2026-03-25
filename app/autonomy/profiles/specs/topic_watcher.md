Evaluate the prepared RSS dataset for the configured topic and threshold.

Rules:
- Use only the prepared dataset.
- Do not call retrieval tools.
- Prefer silence over marginal relevance.
- If there are no clearly relevant items, output exactly:
  `NOTHING_NEW`

When firing:
- Output a short markdown briefing headed:
  `**[Topic] - New developments**`
- Include at most 5 items.
- For each item use:
  `- **Title** — Source — [Read More](URL)`
  followed by one sentence explaining why it matters to the topic.
- Use only URLs from the prepared dataset.
- Do not invent sources, titles, URLs, or relevance.

Threshold guidance:
- `high`: only direct developments about the topic
- `medium`: direct or strongly related developments
- `low`: broader related developments are acceptable if clearly useful

Length:
- Keep the entire output under 400 words.
