Produce a concise morning briefing from the prepared dataset only.

Requirements:
- Use markdown only.
- Start directly with the content. No greeting or sign-off.
- Use sections in this order:
  1. `## Today at a glance`
  2. `## KO market snapshot`
  3. `## Weather`
  4. `## Today in history`
  5. `## Headlines`
  6. `## Worth your attention`
  7. `## Tomorrow at a glance`
- If both calendar events and RSS items are absent, output exactly:
  `Nothing to brief today - no calendar events and no fresh headlines.`

Calendar rules:
- List each event with local time, title, and location if present.
- If an event starts within 2 hours, flag it briefly.
- If there are no events, still include the section and say `No events scheduled today.`

KO market / weather rules:
- Always include these sections.
- If the prepared dataset does not contain grounded information for one of them, say `No update available in prepared data.`
- For weather, prefer Fahrenheit in the final output for U.S. locations and present observed times in clean local time rather than raw UTC timestamps when the prepared dataset includes them.

Today in history rules:
- Always include this section.
- If the prepared dataset contains a grounded history fact, prefer it.
- Otherwise, you may write a short 1 to 2 sentence historical trivia item from general model knowledge for the current calendar date.
- If you cannot provide a reliable history item, say `No update available in prepared data.`

Headline rules:
- Use only URLs from the prepared dataset.
- Include at most 5 headlines.
- Format each as:
  `- **Title** — Source — one-line summary — [Read More](URL)`
- Never invent URLs, titles, or sources.
- Every headline bullet must contain a short one-line summary.

Worth your attention rules:
- Include 2 to 4 short bullets only if there are meaningful cross-source connections.
- Each bullet must be one sentence.
- If there are no meaningful connections, include the section and say `No additional cross-source priorities today.`

Tomorrow rules:
- Always include `## Tomorrow at a glance`.
- Present tomorrow event times in clean local time rather than raw offset notation.
- If there are no tomorrow events, say `No events scheduled tomorrow.`

Length:
- Target under 500 words.
- Never exceed 600 words.
