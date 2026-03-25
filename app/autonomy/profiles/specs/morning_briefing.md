Produce a concise morning briefing from the prepared dataset only.

Requirements:
- Use markdown only.
- Start directly with the content. No greeting or sign-off.
- Use sections in this order when present:
  1. `## Today at a glance`
  2. `## Headlines`
  3. `## Worth your attention`
- If both calendar events and RSS items are absent, output exactly:
  `Nothing to brief today - no calendar events and no fresh headlines.`

Calendar rules:
- List each event with time, title, and location if present.
- If an event starts within 2 hours, flag it briefly.
- If there are no events, omit the section unless calendar is the only available dataset, in which case say `No events scheduled today.`

Headline rules:
- Use only URLs from the prepared dataset.
- Include at most 8 headlines.
- Format each as:
  `- **Title** — Source — [Read More](URL)`
- Never invent URLs, titles, or sources.

Worth your attention rules:
- Include 2 to 4 short bullets only if there are meaningful cross-source connections.
- Each bullet must be one sentence.
- Omit the section if there are no meaningful connections.

Length:
- Target under 500 words.
- Never exceed 600 words.
