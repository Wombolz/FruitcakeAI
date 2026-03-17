# 🍰 FruitcakeAI — Design Philosophy

> *Put a fruitcake in your home and be ready for anything.*

**Audience**: Contributors, adopters, anyone asking "why is it built this way?"  
**Last Updated**: March 2026  
**Roadmap**: [FruitcakeAI Roadmap](FruitcakeAi Roadmap.md)

---

## The Mental Model

Every design decision in FruitcakeAI flows from one principle: **the system should be more useful than the sum of its parts, and it should keep being useful when parts of the environment stop working.**

That's not a disaster-preparedness stance. It's just good engineering. An assistant that degrades gracefully under poor connectivity, that keeps working when a cloud API is down, that doesn't silently fail when data is stale — that's a better assistant in everyday conditions too. Reliability and resilience are the same property at different scales.

The v3/v4 evolution captures how this thinking developed:

> **v3/v4**: A platform that contains an AI  
> **v5**: An AI agent that has tools  
> **v5 Phase 4+**: An AI agent that knows its people and acts without being prompted

Each step moved orchestration out of hand-written rules and into the model itself, and moved complexity out of the system and into configuration. The result is a system that is easier to reason about, easier to extend, and harder to break in subtle ways.

---

## Why Local First

Most AI products are cloud products with a privacy statement. FruitcakeAI inverts that: it is a local product that can optionally reach the cloud.

Local Ollama is the default. The system is fully functional without a cloud API key. Embeddings run locally via HuggingFace. The document library, memory store, and task scheduler all run on your hardware. A deployment that never touches `.env`'s cloud fields never sends a token outside the network — and that is not a degraded experience, it is the intended one.

Cloud LLMs are available as an opt-in enhancement for users who want a higher reasoning ceiling and are comfortable with the tradeoff. The architecture supports this cleanly via LiteLLM — one env var change, no code changes. But the default is always local.

This matters for three reasons that compound on each other:

**Privacy.** Data about your people, your routines, your documents — none of it leaves unless you send it. For many users this is the primary reason to run self-hosted AI at all.

**Continuity.** A system that depends on cloud availability inherits cloud availability as its floor. A local system's floor is your hardware. These are different reliability guarantees.

**Trust.** Users who don't understand AI infrastructure can understand "it runs on that box in the corner." That's a meaningful thing to be able to say to a non-technical person who is deciding whether to let an AI assistant into their daily life.

---

## Why the LLM Is the Orchestrator

Earlier versions of FruitcakeAI had a `ServiceOrchestrator`, a `PolicyRouter`, and a keyword-based intent detection system. All three were dropped in v5.

The lesson from building them: hand-written orchestration rules are brittle in proportion to how many capabilities the system has. Each new tool required updating routing logic. Each edge case required a new rule. The system got harder to reason about as it got more capable — which is exactly backwards.

The converged answer from every major agent framework is: let the LLM route. GPT-4 function calling, Claude tool use, and the MCP standard all arrived at the same architecture independently. The model understands the query semantically; it picks the right tool; the framework executes it. This is more flexible, more maintainable, and easier to inspect than any rule-based alternative.

The practical consequences in FruitcakeAI:
- New tools are added via `config/mcp_config.yaml` — no routing code to update
- Multi-user scoping is injected context, not middleware — you can read exactly what the model is told
- The agent loop in `app/agent/core.py` is short and auditable — tool dispatch is not magic

---

## Why Memory Is the Core Differentiator

The heartbeat agent — the part of FruitcakeAI that acts without being prompted — is only as useful as what it knows before it starts. A heartbeat that checks a flat markdown file knows what's in the list. A heartbeat that queries a semantic memory store knows what has been relevant for this person lately, what they care about, what their preferences are, what happened last week.

That gap is why memory is treated as a first-class architectural concern rather than an afterthought.

FruitcakeAI uses a three-tier retrieval model on every interaction:

1. **Procedural** — standing rules and preferences, always injected. "Prefers bullet summaries." "Do not schedule before 8am."
2. **Semantic** — importance-ranked facts about the user. Injected when relevant to the current query.
3. **Episodic** — recent events and context, retrieved by pgvector similarity to the current query.

Memories are immutable. When a fact changes, the old memory is deactivated and a new one is created. The full history of what the system knew and when is preserved — both for debugging ("why did it say that?") and for trust ("I can see exactly what it was working from").

---

## Why Multi-User Is Context Injection, Not Middleware

v4 wove user permissions through every layer of the stack — service layer, routing layer, tool layer. This made it very hard to trace why something was or wasn't accessible, and made adding new tools expensive because each one had to understand the permission model.

v5 implements multi-user scoping as injected prompt context. The system prompt for each session tells the model who the user is, what their role is, which tools they can use, and what they can access. The model enforces scope through its understanding of the instruction — not through code.

This is auditable (log the system prompt, see exactly what the model was told), easy to extend (adding a new scope means adding a line to `personas.yaml`), and easy to test (assert that the system prompt for a restricted-access user does not include web research tools).

The tradeoff is that prompt-based enforcement is less formally guaranteed than code-based enforcement. For the use cases FruitcakeAI targets — personal and small-team deployments where users are trusted — this tradeoff is correct.

---

## Why MCP

The Model Context Protocol is the emerging standard for connecting AI agents to external tools and data sources. FruitcakeAI adopted it early and builds around it for one practical reason: **tooling investment compounds**.

An MCP server written for FruitcakeAI works with Cursor, Claude Desktop, and any other MCP-compatible host. An MCP server written for any of those works with FruitcakeAI. The ecosystem grows in all directions simultaneously.

The practical architecture consequence: every capability in FruitcakeAI is either a built-in MCP server (`app/mcp/servers/`) or a Docker stdio MCP server registered in `config/mcp_config.yaml`. There is no tool registry that isn't MCP. This means the extension path is always the same regardless of what you're adding.

**The boundary rule**: FruitcakeAI core must function fully without any optional MCP provider. Every integration is additive. Nothing external is load-bearing.

---

## Why Safety Is a Design Constraint

FruitcakeAI supports multiple users with different roles and access levels, and it acts autonomously on behalf of those users. That combination requires safety controls to be designed in, not bolted on.

Three mechanisms enforce this:

**Persona-scoped tool access.** The `restricted_assistant` persona does not have access to web research tools. This is not a runtime check — the tools are simply not offered to the model in that context. You cannot prompt-inject your way to a blocked tool because the tool is not in the model's schema for that session.

**Approval gates.** Any task marked `requires_approval` pauses before executing irreversible actions, sends a push notification to the user, and waits. The task cannot proceed without explicit confirmation through the API. This is the primary defense against the autonomous agent doing something the user didn't intend.

**Active hours.** The scheduler enforces per-user active hours windows. A task that would fire at 3am is silently skipped and rescheduled. This is stored at the user level and enforced at the runner level, not in task configuration — so it can't be accidentally omitted.

---

## Architectural Hierarchy

Every agent execution resolves through a consistent layered pipeline. This hierarchy prevents architectural drift as the system grows — each layer has one job and one only.

```
Identity / Profile
    ↓  Who is this user?
Long-Term Memory
    ↓  What do we know about them?
Recent Context
    ↓  What matters right now?
Persona
    ↓  How should we behave?
Capability Profile
    ↓  What actions are allowed?
Resolved Tools
    ↓  How are actions executed?
Agent Execution
```

Decisions that feel ambiguous usually become clear when you ask which layer they belong to. A question about tone belongs in Persona. A question about which documents are accessible belongs in Identity/Profile. A question about whether a tool can be called belongs in Capability Profile.

---

## What Gets Dropped and Why

The v5 rebuild explicitly dropped several components that were in earlier versions. The reasoning is worth preserving because the temptation to add them back will recur.

| Dropped | Reason |
|---------|--------|
| `ServiceOrchestrator` | The LLM is the orchestrator — hand-written routing doesn't scale with capability |
| `PolicyRouter` | Replaced by context injection — auditable, testable, maintainable |
| Keyword intent detection | Replaced by LLM semantic understanding — more accurate, zero maintenance |
| Celery / RQ job queue | APScheduler in-process is sufficient; distributed queuing is premature complexity |
| ELK / Prometheus / Grafana | Structured JSON logs are enough at this scale; observability infrastructure has a cost |
| Kubernetes / microservices | Wrong scale for the use case; optimize for developer experience first |
| SOC 2 / enterprise compliance | Belongs in the enterprise fork when warranted, not in the base |

The pattern: infrastructure complexity was added speculatively in anticipation of scale that didn't exist. v5 builds only what the current use case requires and adds complexity only when real friction demands it.

---

## Bring Your Own Fruits and Nuts

FruitcakeAI is a base. The document library, the MCP integrations, the personas, the offline knowledge archives — all of it reflects the people using it and their priorities. The system doesn't prescribe what goes in it; it just makes whatever you put in work well and keep working.

This is both a design principle and a positioning statement. The project is not trying to be everything to everyone out of the box. It is trying to be an excellent foundation that anyone can make their own without forking the core.

What you add, and why, is up to you.

---

## Further Reading

- [README](../README.md) — what's shipped and how to run it
- [Roadmap](FruitcakeAi Roadmap.md) — where development is now and where it's going
- [Adding MCP Tools](ADDING_MCP_TOOLS.md) — how to extend the system
- [Persona System](PERSONA_SYSTEM.md) — configuring users, roles, and access
- [LLM Backends](LLM_BACKENDS.md) — switching between Ollama, Claude, OpenAI

---

*FruitcakeAI — Put a fruitcake in your home and be ready for anything.* 🍰
