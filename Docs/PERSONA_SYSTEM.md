# Persona System

Personas customize how the assistant behaves for different family members.
They control tone, tool access, content filtering, and document scope — all
from a single YAML file.

---

## Persona configuration (`config/personas.yaml`)

```yaml
personas:
  family_assistant:
    description: General family assistant with access to all shared resources
    tone: friendly and helpful
    library_scopes: [family_docs, recipes, household]
    calendar_access: [family, personal]
    blocked_tools: []

  restricted_assistant:
    description: Restricted-access assistant with filtered content and limited tools
    tone: encouraging and simple
    library_scopes: [kids_books, homework]
    calendar_access: [family]
    content_filter: strict
    blocked_tools: [web_search, fetch_page, get_feed_items, search_feeds]

  work_assistant:
    description: Focused on professional tasks and productivity
    tone: professional and concise
    library_scopes: [work_docs, projects]
    calendar_access: [work, personal]
    blocked_tools: []
```

Changes take effect on the next server restart (personas are cached at startup).

---

## Fields reference

| Field | Type | Effect |
|-------|------|--------|
| `description` | string | Shown to the LLM as the persona's identity; appears in the Swift persona picker |
| `tone` | string | Appended to the system prompt: "be {tone}" |
| `library_scopes` | list | Documents the LLM is allowed to surface when using `search_library` |
| `calendar_access` | list | Calendar categories the assistant can read/write |
| `content_filter` | `"strict"` or `""` | `"strict"` adds restricted-access content restrictions to the system prompt |
| `blocked_tools` | list | Tool function names removed from the LLM's schema — model never sees them |

---

## How tool blocking works

`blocked_tools` filters tools **before** they reach the LLM. The model's function-calling schema simply doesn't include blocked tools — this is stronger than a prompt instruction because the model has no way to call a tool it can't see.

```
get_tools_for_user(user_context)
  → built-in tools (search_library, summarize_document)
  → MCP tools from registry
  → filter: remove tools in user_context.blocked_tools
  → return final schema to LiteLLM
```

---

## Default persona per user

Each user record has a `persona` field. Set it at registration or update it via the admin API:

```bash
# Update user 3's persona to restricted_assistant
curl -X PATCH http://localhost:30417/admin/users/3 \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"persona": "restricted_assistant"}'
```

---

## Mid-session persona switching

Any user can switch their active persona for the current session by sending a
chat message starting with `/persona`:

```
/persona work_assistant
```

The switch is persisted to the session record in the database. Subsequent
messages in that session use the new persona's tool set, tone, and scopes.

Switching back:
```
/persona family_assistant
```

Available personas are listed at:
```bash
curl http://localhost:30417/chat/personas
```

---

## Adding a new persona

1. Add an entry to `config/personas.yaml`
2. Restart the backend (`./scripts/start.sh`)
3. The new persona is immediately available via `/persona <name>` and in the Swift persona picker

No code changes required.

---

## Swift app integration

The iOS/macOS app fetches personas from `GET /chat/personas` and displays
them in the **Settings → Persona** picker. Each persona shows:
- Description
- Tone badge
- Filtered-access badge (if `content_filter: strict`)
- Restricted-tools badge (if `blocked_tools` is non-empty)

The selected persona is stored in `UserDefaults` and sent as part of the
session creation payload when a new conversation starts.
