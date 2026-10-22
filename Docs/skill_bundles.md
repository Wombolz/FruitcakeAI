# Skills Manager And Text Bundles

Administrators can manage skills in native **Settings > Intelligence > Skills**.
Choose **Import Skill**, select the folder containing `SKILL.md`, inspect the
documents, choose Everyone or Only Me, then **Validate Preview** and **Install
Skill**. Missing tools and instruction-budget problems block installation.

This feature pairs backend v0.7.45 with native v0.2.6. Migration
`062_skill_bundles` adds one nullable column to `skills`;
normal startup applies it with the existing migration-role helper. Existing
plain-text skills retain their previous storage and prompt budget.

## What Is Imported

- A frozen text copy of `SKILL.md` and supporting `.md`/`.txt` documents, including
  relative subdirectories. The source folder is never edited or watched.
- At most 32 files, 64 KB per file, 512 KB total; main instructions are limited to
  12,000 characters. HTTP imports are capped before JSON parsing. Symlinks,
  executable scripts, binary files, absolute/traversal paths and missing local
  documentation links are rejected. Hidden files such as `.DS_Store` are ignored
  by the native folder picker.
- Preview hashes cover every file plus scope, personal owner and fallback pin.
  Install revalidates current tool availability for the authenticated caller and
  rejects a changed preview. Repeating the identical active import is idempotent;
  different content supersedes the previous active version without deleting it.
- The current text-only implementation does not run skill scripts, install
  dependencies, follow remote links, or automatically grant permissions.

## Prompt And Reference Loading

Selected bundles inject the **complete main instructions**, not the old
250-token excerpt. Separate defaults are `SKILLS_BUNDLE_MAX_TOKENS=4000` and
`SKILLS_BUNDLE_TOTAL_MAX_TOKENS=6000`. References do not all enter the prompt.
Bundles that exceed a configured limit are explicitly excluded rather than
silently truncated; the import preview blocks known oversize main documents.

The scoped `read_skill_file` tool reads only frozen files from bundle IDs selected
for the current turn and visible to the current account. It never reads an OS
path. References are paged with `offset`/`next_offset`; following pages reconstructs
the exact stored file while staying below ordinary tool-output compaction limits.
Disabled, removed, superseded, unselected and other users' private bundles cannot
be read through this tool. Normal blocked-tool lists and execution caps still
apply. Shared instructions do not bypass restricted-persona/tool policies.

Semantic matching remains preferred. Bundles can fall back to description-term
matching if embeddings are unavailable; the optional pin keeps them available
in that offline mode. Pinning does not override permissions or total budgets.

## Managing And Removing

The screen lists installed versions, scope and enabled state. **View Documents**
shows exactly what is stored. Disable retains the frozen content; remove requires
confirmation and deletes the installed record without touching the source folder.
Enabling an older version while another version is active returns a clear conflict.
No backend restart is required after importing or changing a skill.

API: `POST /admin/skill-bundles/preview`, `POST /admin/skill-bundles/install`,
`GET /admin/skill-bundles/{id}`, with existing `/admin/skills` list/update/delete.
All management routes require an administrator; reference reads are independently
account- and selected-turn-scoped. Supporting files are plain data, never trusted
as instructions to the importer itself.

## LLMINA Acceptance Case

`llmina-image-generation` contains five Markdown files. Its full main instructions
fit the default bundle budget (approximately 2,300 estimated tokens), with the
four reference files retained for on-demand reads. Its six required ImageLab MCP
tools must be connected and available before installation; offline/missing-tool
warnings must remain visible rather than be bypassed. Test without rendering an
image by checking the preview, stored document hashes and a paged reference read.

Local acceptance verified all six tools against LLMINA, imported all five files
as a personal skill, selected the complete main instructions for an image query,
and reconstructed the full model-settings reference through paged reads. Source
documents remained unchanged. No image-generation call was made.
