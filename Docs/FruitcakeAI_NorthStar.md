# 🍰 FruitcakeAI — North Star

> *Put a fruitcake in your home and be ready for anything.*

**Status**: Strategic Reference  
**Last Updated**: March 2026  
**Audience**: Contributors, project maintainers, potential adopters

---

## What FruitcakeAI Is

FruitcakeAI is a private, local-first AI assistant for individuals, households, and small teams. It runs entirely on your hardware, knows the people it serves by name and by memory, and keeps working when the internet doesn't.

Calendars and recipes are easy first targets. The real purpose is deeper: a system that remains functional, trustworthy, and informed under degraded conditions — intermittent connectivity, unreliable news, or no external services at all.

Most AI assistants are designed for the best case. FruitcakeAI is designed for the common case and the worst case simultaneously.

---

## The Core Design Principles

### 1. Privacy First, Cloud Optional

Local Ollama is the default and the baseline. Data never leaves the house unless the user has explicitly opted in. Cloud LLMs are a capability enhancement available to users who choose to trade some privacy for a higher reasoning ceiling — not the starting point, not the assumption.

Anyone who never configures a cloud API key never sends a token outside their network. That should be a complete, fully-functional experience.

### 2. Resilience by Construction

The architecture degrades gracefully through explicit tiers. Every component knows which tier it's operating in and communicates data freshness and confidence accordingly.

| Tier | Connectivity | LLM | Data Sources | Capability |
|------|-------------|-----|-------------|-----------|
| **1 — Full Local** | Internet available | Ollama (local, default) | Live feeds + local library | Full capability, complete privacy |
| **1+ — Cloud Enhanced** | Internet available + explicit opt-in | Cloud (Claude/GPT) | Live feeds + local library | Higher reasoning ceiling, user-configured |
| **2 — Cached Feeds** | Internet degraded | Ollama (local) | Cached snapshots only | Full local reasoning, freshness-stamped data |
| **3 — Offline** | No connectivity | Ollama (local) | Local library only | Document RAG, memory, calendar, contacts |
| **4 — On-Device** | No connectivity, server down | Apple FoundationModels | Device cache only | Calendar, reminders, contacts via Swift fallback |

When data has an age, say so. Every data-bearing response should carry freshness metadata when it matters.

### 3. Knows Its People

The memory system is the core differentiator. FruitcakeAI maintains persistent, per-user semantic memory across sessions — procedural preferences, episodic facts, long-term knowledge — retrieved and injected into every interaction. The heartbeat agent doesn't just check a checklist; it knows what's been relevant for this person lately.

### 4. Multi-User, Role-Aware, Safe by Default

Designed for multiple users, not single power users. Role-based personas with scoped tool access, content filtering for children, approval gates for irreversible actions. Safety is not a feature layer — it's a design constraint.

### 5. Modular by Default

New capabilities arrive as MCP servers, not code changes. The agent is the orchestrator. Adding a new data source or tool means adding a config entry, not modifying the core.

---

## What Differentiates FruitcakeAI

| Dimension | Generic Home AI | FruitcakeAI |
|-----------|----------------|-------------|
| Default LLM | Cloud API | Local Ollama |
| Memory | Session only or flat file | Persistent per-user pgvector |
| Users | Single | Multi-user, role-scoped |
| Offline capability | Minimal | Explicit degradation tiers |
| Data sovereignty | Cloud-dependent | Air-gapped by default |
| Safety | None or opt-in | Persona-scoped tool blocking, content filters, approval gates |
| Mobile | Web or Telegram | Native Swift, APNs, on-device fallback |
| Knowledge base | Live queries only | Ingestible local library + offline archives |

---

## The Offline Knowledge Layer

A key long-term investment is a curated offline knowledge base — pre-populated reference content that functions identically to user-uploaded documents through the existing RAG pipeline. This is the "bring your own fruits and nuts" layer: the core provides the infrastructure to ingest and query any content; what you put in it is entirely up to you.

**Examples of what you might ingest:**
- Wikipedia via Kiwix `.zim` files — general reference
- Medical and health references
- Local area maps and community information
- Household documents, manuals, and records
- Any reference material worth having available offline

These aren't bundled by default — they're too large and too personal to be universal. The setup flow should make them easy to acquire and ingest. The system doesn't prescribe what's worth keeping; it just makes keeping it useful.

---

## Bring Your Own Fruits and Nuts

FruitcakeAI is a base. The architecture is intentionally open-ended about what you put in it.

New capabilities arrive as MCP servers dropped into a config file — no code changes, no modifications to the core. The agent discovers them, learns their tools, and starts using them. Anyone who wants live data feeds, specialist knowledge bases, home automation integration, or custom monitoring can add any of those as independent MCP providers without touching the FruitcakeAI codebase.

**The rule is simple:** FruitcakeAI core must function fully without any optional provider. Every MCP integration is additive. If it's not configured, nothing breaks — it's just not there. This keeps the base clean and keeps each deployment's configuration its own business.

What you add, and why, is up to you.

---

## Phase Gating

This north star describes the full intended vision. Current development is gated at **Phase 5.4 soak / Phase 6 cloud routing**. Features described here that are not yet implemented are explicitly scoped to later phases.

**Phase 6 entry criteria** (before expanding scope):
- Phase 5.4 hardening complete and system in stable daily use
- MCP execution profiles verified reliable under soak
- Cloud routing implementation proven correct

**Phase 6 scope additions enabled by this north star:**
- Offline archive ingest pipeline (Kiwix integration)
- Degradation tier runtime detection and tier-aware prompting
- Additional optional MCP provider documentation and examples

**Not in Phase 6:**
- Automated tier switching (manual config, detected and communicated, not auto-routed)
- Bundled offline archives (too large and too personal to ship by default)

---

## What This Is Not

- **Not a disaster preparedness app.** The resilience is a property of the architecture, not a product category. Someone using it for daily scheduling benefits from the same design as someone running it in an unreliable environment. Resilience isn't a mode — it's just how it's built.
- **Not a prescribed configuration.** The system doesn't tell you what to put in it. The offline library, the MCP integrations, the personas — all of that reflects your context and your priorities. The base just makes whatever you put in it work well and keep working.
- **Not a single-user power tool.** OpenClaw is excellent for a technical single user who wants maximum connectivity and tool surface. FruitcakeAI optimizes for a different constraint set — trusted by non-technical users, role-aware by default, resilient by construction, private by design.

---

*FruitcakeAI — Put a fruitcake in your home and be ready for anything.* 🍰
