Evaluate the prepared OpenWeather API contract and use only the first-party API tool.

Rules:
- Use `api_request` only.
- Do not use shell, web search, page fetch, or planning/task-management tools during the run.
- Use the prepared `service`, `endpoint`, `secret_name`, `query_params`, and `response_fields` exactly unless the prepared contract is obviously malformed.
- The backend API adapter already normalizes current weather and task-level dedupe.
- If the API result says there are no current weather conditions, say so plainly and do not invent conditions.
- Keep the response short and factual.

When calling the tool:
- Use:
  - `service: weather`
  - `endpoint: current_conditions`
  - `secret_name`: `openweathermap_api_key` (or the prepared alias, if present)
  - `query_params`: the prepared query params
  - `response_fields`:
    - `location: location`
    - `current_weather: current_weather`

Output:
- Provide a short markdown list with current conditions.
- Include:
  - local time in the prepared display timezone
  - UTC time in parentheses
  - temperature
  - wind speed
  - weather code
- Keep it concise and factual.
