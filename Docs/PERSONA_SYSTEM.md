# Persona System

Personas customize assistant identity, tone and behavioral guidance. Server-owned
definitions currently live in YAML and may narrow account policy; selecting a
personality cannot expand the account's grants or remove mandatory restrictions.

## Persona Authoring Branch

The current persona-authoring branch adds **Neutral**, a tool-capable starting
point with no inherited household identity or tone. Its metadata is discovered
by the existing native persona picker; no new client build is required for this
preset. Owner-scoped revision APIs and pinned chat/run snapshots are implemented
locally, with unapplied migrations 060/061. The native Settings creator/editor,
bounded prompt preview, fresh test chats and portrait/turnaround attachments are
implemented locally and await signed-in operator acceptance. See
[Persona Authoring Foundation](persona_authoring.md) for status and test paths.

Account grants and the administrator-assigned legacy persona form a policy
ceiling, with an additional protected floor for restricted-role accounts.
Selecting a different personality can narrow that ceiling, not replace it.
Explicit empty scope lists mean no access, not fallback to the user's grants.
Blocked tools are accumulated and strict content guidance cannot be disabled by
selecting Neutral. Tool dispatch, aliases and approval replay enforce tool caps.

Legacy library topic labels and calendar categories are not a new per-document
or provider-calendar ACL. Existing ownership/integration checks remain in force;
this slice narrows context scope lists and disables library/calendar tool families
when their effective list is empty. Do not interpret a topic label as a new
resource grant or as proof of fine-grained filtering by each legacy adapter.

Existing built-in prompt wording is retained where account grants already cover
the selected persona. Previously overbroad scope context is intentionally narrowed.
Missing account/task persona definitions now fail explicitly rather than silently
falling back to Personal Assistant. Administrators must repair invalid references.

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
| `library_scopes` | list | Legacy context labels, intersected with account/baseline policy; empty disables library tools, not a new per-document ACL |
| `calendar_access` | list | Category hints intersected with account/baseline policy; empty disables calendar tools, not a provider-calendar ACL |
| `content_filter` | `"strict"` or `""` | `"strict"` adds restricted-access content restrictions to the system prompt |
| `blocked_tools` | list | Tool function names removed from the LLM's schema — model never sees them |
| `identity` | string | Optional identity text; explicitly empty omits the inherited household identity |
| `personality_mode` | `default` or `neutral` | Neutral removes generic inherited response-style guidance, not runtime/access rules |

---

## How tool blocking works

`blocked_tools` filters tools **before** they reach the LLM. Dispatch also checks
the effective policy, including resolved aliases and approval replay: omission
from the offered schema is not itself an authorization boundary.

```
get_tools_for_user(user_context)
  → built-in tools (search_library, summarize_document)
  → MCP tools from registry
  → filter: remove tools in user_context.blocked_tools
  → return final schema to LiteLLM
```

---

## Default persona per user

Each user record has an administrator-managed `persona` policy baseline. It is
also the fallback personality until a separate personal default is selected.
Do not rewrite this field when saving a user-owned personality. Administrators
can manage the baseline via the admin API:

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

The switch is persisted with a frozen personality snapshot. Subsequent turns use
that style with current account-bounded permissions. Switching is rejected while
a turn or approval is pending. Custom revisions require explicit API selection;
`/persona` retains bundled-key compatibility.

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

The authoring client uses authenticated `GET /personas` in **Settings > Personas**
to browse bundled and owner-created personalities. Its editor supports versioned
saves, defaults, archive/history, private visual-reference attachments, bounded
prompt/tool preview and fresh test chats. The public `GET /chat/personas` endpoint
remains available to older clients for bundled discovery.

The server is authoritative for the personal default and per-chat selection.
The native authoring surface uses authenticated `/personas` APIs rather
than treating client `UserDefaults` as a persona definition or permission grant.
