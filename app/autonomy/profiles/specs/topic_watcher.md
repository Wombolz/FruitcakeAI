Evaluate the prepared RSS dataset for the configured topic and threshold.

Rules:
- Use only the prepared dataset.
- Start from the prepared source inventory and prepared RSS dataset.
- Treat any approved topic memory timeline as continuity context only.
- Do not manage RSS sources during this task run.
- Do not call retrieval or memory tools; the watcher must decide from prepared context only.
- Prefer silence over marginal relevance.
- Do not cite memories as sources; source grounding must come only from the prepared RSS dataset.
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
- If the developments are clearly consequential and durable, append:
  `## Memory candidate`
  with one concise topic-level timeline statement.
- Do not output a memory candidate for weak, noisy, or routine updates.

Threshold guidance:
- `high`: only direct developments about the topic
- `medium`: direct or strongly related developments
- `low`: broader related developments are acceptable if clearly useful

Length:
- Keep the entire output under 400 words.
