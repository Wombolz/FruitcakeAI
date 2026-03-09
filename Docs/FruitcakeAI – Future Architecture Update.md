FruitcakeAI – Future Architecture Updates (Captured Ideas)

This document captures several architectural improvements discussed during design review so they are not lost while continuing active development. The goal is to record what to build later and in what order, prioritizing low‑risk additions that avoid major refactors.

The ordering below reflects safest implementation sequence, starting with changes that introduce abstraction layers and guardrails, and ending with features that alter runtime behavior or memory semantics.

⸻

Recommended Implementation Order
	1.	Toolsets (Capability Layer)
	2.	Memory Budgets
	3.	Layers of Memory
	4.	Event‑Driven Heartbeat Triggers
	5.	Memory Consolidation (“Dream Cycle”)

Each section below explains the concept, why it is useful, and why it appears in this order.

⸻

1. Toolsets (Capability Layer)

Purpose

Introduce an abstraction layer between the agent and raw MCP tools so the system reasons about capabilities instead of individual tools.

Current pattern in most systems:

Agent → Tools → MCP Servers

Desired pattern:

Agent → Capability / Toolset Resolver → Tools → MCP Servers

Example

Capabilities might look like:

web_research
calendar_read
calendar_write
document_lookup
notifications

Each capability maps to multiple underlying tools.

Example:

web_research
  - web_search
  - fetch_page
  - rss_fetch

Benefits
	•	Reduces tool selection chaos as MCP servers grow
	•	Makes persona configuration cleaner
	•	Allows easy addition of new MCP servers
	•	Enables future RBAC‑style tool policies

Minimal Implementation Strategy

Add a resolver layer without changing runner behavior.

Example:

def resolve_execution_profile(task, user):
    return {
        "persona": ...,
        "capabilities": [...],
        "allowed_tools": [...]
    }

Initially, allowed tools can still be derived from persona config.

This change introduces a stable extension seam without forcing a full persona/capability refactor.

Why This Comes First

It creates the structural boundary needed for future tool growth without touching the agent loop.

Low risk. High long‑term payoff.

⸻

2. Memory Budgets

Purpose

Prevent memory retrieval from growing unbounded and degrading reasoning quality.

Without budgets, memory retrieval eventually becomes noisy as similar memories accumulate.

Example Budget Strategy

Example limits injected into prompts:

procedural memories: max 10
semantic memories: max 20
episodic memories: max 10

Or token‑based budgets:

procedural: 150 tokens
semantic: 200 tokens
episodic: 250 tokens

Benefits
	•	Prevents context explosion
	•	Improves reasoning signal
	•	Makes retrieval deterministic
	•	Prepares system for later consolidation

Implementation Location

Inside MemoryService.retrieve_for_context().

Budgets can be enforced after ranking and before context assembly.

Why This Comes Second

Budgets stabilize memory usage before introducing more complex memory structures.

Low risk change to retrieval logic only.

⸻

3. Layers of Memory

Purpose

Formalize different types of long‑term memory and how they are retrieved.

Current memory model already distinguishes:

episodic
semantic
procedural

Future layering expands this slightly.

Proposed Layers

Identity / Profile Layer

Stable personal facts.

Examples:

name
role
family members
organization

Rarely changes.

Procedural Layer

Behavior rules.

Examples:

prefers bullet summaries
prefers morning notifications

Always injected into prompts.

Semantic Layer

Long‑term knowledge about the user.

Examples:

works in athletics AV
uses Daktronics scoreboards

Retrieved by relevance.

Episodic Layer

Recent events and temporary context.

Examples:

Dentist appointment Thursday
Project deadline Friday

Often expires automatically.

Benefits
	•	Cleaner reasoning context
	•	Better memory ranking
	•	Reduced duplication

Implementation Strategy

Prefer retrieval‑layer changes first instead of major schema refactors.

Focus on how memories are selected, not necessarily how they are stored.

Why This Comes Third

Memory budgets should exist first so layered retrieval has predictable limits.

⸻

4. Event‑Driven Heartbeat Triggers

Purpose

Allow the assistant to evaluate situations when relevant events occur instead of relying solely on interval heartbeats.

Current system:

heartbeat every 30 minutes

Future system:

interval heartbeat
+ event triggers

Example Triggers

calendar change
new email
pending approval created
task completed
important webhook event

Design Rule

Event triggers should invoke the same judgment path as the heartbeat, not bypass it.

Example:

event → heartbeat evaluation → action or HEARTBEAT_OK

Safety Features
	•	trigger debouncing
	•	event coalescing
	•	duplicate suppression

Why This Comes Fourth

It modifies runtime behavior and scheduling logic, which makes it riskier than structural improvements.

Best implemented after memory and capability layers stabilize.

⸻

5. Memory Consolidation (“Dream Cycle”)

Purpose

Prevent long‑term memory fragmentation by merging similar memories into cleaner representations.

Example before consolidation:

prefers bullet points
likes concise summaries
prefers structured answers

After consolidation:

Prefers concise bullet‑point summaries.

Concept

Periodic background job that compresses overlapping memories.

Inspired by biological “dream cycles” that consolidate human memory.

Example Process

find clusters of similar memories
merge into summarized memory
deactivate originals
insert consolidated memory

Important Safety Rules
	•	never overwrite memories
	•	always preserve lineage
	•	only deactivate originals

Initial Conservative Scope

Start with consolidation of:
	•	procedural duplicates
	•	semantic duplicates

Avoid consolidating episodic memories initially.

Why This Comes Last

Consolidation modifies the knowledge base itself.

It requires stable:
	•	retrieval patterns
	•	memory budgets
	•	memory layer definitions

Implementing it too early risks damaging useful knowledge.

⸻

Summary

Safest evolution path for FruitcakeAI:
	1.	Toolsets (capability abstraction)
	2.	Memory budgets (retrieval guardrails)
	3.	Layers of memory (structured memory model)
	4.	Event‑driven heartbeat triggers (reactive evaluation)
	5.	Memory consolidation (long‑term knowledge maintenance)

This sequence minimizes risky refactors while enabling long‑term extensibility.

⸻

Design Principle

Future features should follow this philosophy:

stabilize interfaces first
add intelligence later

Structural layers (capabilities, budgets) reduce risk before introducing behaviors that alter system reasoning.

⸻

Additional Architectural Notes

Persona vs Capability Separation (Future Direction)

Personas should eventually represent behavior and reasoning style, not tool permissions.

Example responsibilities:

Persona:
	•	tone
	•	reasoning approach
	•	response style

Capability Profile:
	•	categories of allowed actions
	•	safety policies
	•	read vs write access

Example resolution pipeline:

Task
 ↓
Persona
 ↓
Capability Profile
 ↓
Resolved Toolset
 ↓
Agent Execution

This separation prevents “persona explosion” where many nearly identical personas exist only to control tool access.

⸻

Example Toolset Configuration

Possible future toolsets.yaml structure:

web_research:
  tools:
    - web_search
    - fetch_page
    - rss_fetch

calendar_read:
  tools:
    - calendar_list_events
    - calendar_get_event

calendar_write:
  tools:
    - calendar_create_event
    - calendar_update_event

document_lookup:
  tools:
    - library_search
    - summarize_document

Personas or capability profiles then reference these toolsets instead of individual tools.

⸻

Event Trigger Architecture

Future event-driven triggers should reuse the heartbeat evaluation pipeline.

Architecture:

Event
 ↓
Trigger Router
 ↓
Heartbeat Evaluation
 ↓
Action or HEARTBEAT_OK

Possible triggers:
	•	calendar event created or modified
	•	webhook received
	•	task status changed
	•	new document ingested
	•	external system notification

Safety rules:
	•	debounce repeated triggers
	•	suppress duplicate evaluations
	•	coalesce multiple events

Interval heartbeat should remain as a fallback safety mechanism.

⸻

Memory Consolidation Algorithm (Concept)

Future “dream cycle” job may operate as follows:

retrieve similar memories
cluster by embedding similarity
summarize cluster into single memory
insert consolidated memory
deactivate originals

Important safeguards:
	•	maintain lineage references
	•	never overwrite original memory content
	•	log consolidation actions for auditability

Example transformation:

Before:

prefers bullet points
likes concise summaries
prefers structured answers

After:

Prefers concise bullet-point summaries.


⸻

Long-Term Goal

The architecture should evolve toward a layered reasoning model:

Identity / Profile
 ↓
Long-Term Memory
 ↓
Recent Context
 ↓
Agent Reasoning
 ↓
Capabilities / Tools

This structure enables assistants that adapt over time while remaining predictable and safe.

⸻

Memory Lifecycle Model

Future memory behavior should follow a predictable lifecycle so the system remains explainable and maintainable.

Lifecycle stages:

Creation
 ↓
Retrieval
 ↓
Reinforcement
 ↓
Consolidation
 ↓
Archival / Deactivation

Creation

Memories are created by:
	•	agent tool (create_memory)
	•	nightly extraction
	•	explicit user input

Retrieval

MemoryService.retrieve_for_context() selects memories based on:
	•	similarity
	•	importance
	•	recency
	•	type

Reinforcement

Each retrieval increases access_count and slightly increases importance.

Example:

importance = min(1.0, importance + 0.02)

Consolidation

A periodic background process merges clusters of similar memories into more stable representations.

Archival / Deactivation

Memories may be deactivated when:
	•	expired
	•	superseded
	•	consolidated

Original records remain for auditability.

⸻

Agent Execution Profile Resolution

Agent execution should always resolve through a deterministic pipeline.

Task
 ↓
Persona
 ↓
Capability Profile
 ↓
Toolsets
 ↓
Allowed Tools
 ↓
Agent Execution

Task

Defines the instruction and optional explicit persona.

Persona

Defines behavioral style and reasoning pattern.

Capability Profile

Defines categories of actions allowed for the task.

Toolsets

Maps capabilities to groups of concrete MCP tools.

Allowed Tools

Expanded list of specific tools provided to the agent.

This layered resolution prevents the agent from reasoning over an excessively large raw tool list.

⸻

Observability and Debugging Hooks

As the system becomes autonomous, observability becomes critical.

Recommended logging layers:

heartbeat evaluations
task executions
memory retrieval sets
memory creation events
tool invocation history

Example debugging output for a task run:

resolved persona: news_researcher
capabilities: [web_research, rss_read]
tools exposed: [web_search, fetch_page, rss_fetch]
memories injected: 6

These logs dramatically simplify diagnosing unexpected agent behavior.

⸻

Safety Guardrails for Autonomous Behavior

Autonomous assistants must include explicit safety boundaries.

Key mechanisms already planned or recommended:
	•	approval gates for destructive tools
	•	active-hours enforcement
	•	event trigger debouncing
	•	tool capability restrictions
	•	session isolation for task runs

Future improvements may include:
	•	per-user capability policies
	•	rate limits for autonomous actions
	•	anomaly detection on task behavior

⸻

Long-Term Architectural Principle

The system should evolve around a consistent hierarchy:

Identity / Profile
 ↓
Long-Term Memory
 ↓
Recent Context
 ↓
Persona
 ↓
Capabilities
 ↓
Tools

Each layer answers a different question:

Layer	Question Answered
Identity	Who is this user?
Memory	What do we know about them?
Context	What matters right now?
Persona	How should we behave?
Capabilities	What actions are allowed?
Tools	How are actions executed?

Maintaining this hierarchy prevents architectural drift as new features are added.

⸻

Reference Diagram

FruitcakeAI Execution Architecture

Identity / Profile
    ↓
Long-Term Memory
(semantic / procedural / episodic)
    ↓
Recent Context
(active tasks, recent events, current time, trigger source)
    ↓
Persona
(role, tone, reasoning style)
    ↓
Capability Profile
(action categories allowed)
    ↓
Toolsets
(grouped tool capabilities)
    ↓
Resolved Tools
(actual MCP tools exposed for this run)
    ↓
Agent Execution
(chat / task / heartbeat)
    ↓
Actions / Output
(response, task result, push, approval request, HEARTBEAT_OK)

Memory Side Loop

Conversation / Task / Event
    ↓
Memory Creation
(agent tool / extraction / explicit user input)
    ↓
Memory Store
    ↓
Retrieval / Reinforcement
    ↓
Consolidation / Archival

Autonomy Side Loop

Interval Heartbeat or Event Trigger
    ↓
Judgment Evaluation
    ↓
No action  → HEARTBEAT_OK
or
Action needed → task / push / approval flow


⸻

End of captured future architecture updates.