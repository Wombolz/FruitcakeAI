Evaluate the prepared ISS API contract and use only the first-party API tool.

Rules:
- Use `api_request` only.
- Do not use shell, web search, page fetch, or planning/task-management tools during the run.
- Use the prepared `service`, `endpoint`, `secret_name`, and `query_params` exactly unless the prepared contract is obviously malformed.
- The backend API adapter already normalizes ISS pass results and task-level dedupe.
- Delayed pre-event notifications are not supported in this profile yet. Report qualifying upcoming passes now.
- If the API result says there are no new pass changes or no visible passes found, say so plainly and do not invent alerts.

When calling the tool:
- Use:
  - `service: n2yo`
  - `endpoint: iss_visual_passes`
  - `secret_name`: the prepared secret name
  - `query_params`: the prepared query params

Output:
- If qualifying passes exist, provide a short markdown list.
- For each pass include:
  - local time in America/New_York
  - UTC time in parentheses
  - duration
  - max elevation
- Keep it concise and factual.
