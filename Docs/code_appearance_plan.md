# Code Appearance And Syntax Highlighting

Recorded October 19, 2026. Approved next slice, not implemented in the review /
inspector / Explorer milestone. Start after the paired branches have landed.

## Goal

Make source syntax colors user-selectable while preserving Fruitcake's application
chrome and the accepted sidebar/titlebar/inspector geometry. This is not general
app theming, an editable IDE, a language server or a browser-testing feature.

## First Delivery

1. Define a versioned internal CodeTheme palette separate from global Theme:
   source foreground/background/gutter/selection/search, syntax token styles and
   distinct diff addition/removal styles. Leave approval/error/status semantics
   outside imported code themes. Retain +/- and labels as non-color signals.
2. Select and evaluate a local syntax tokenizer against actual Swift/AppKit and
   Markdown rendering paths before committing to a dependency. Unknown languages
   and tokenization failures fall back to complete, literal plain text. Keep the
   existing source size/line bounds; no inference or remote highlighting.
3. Ship a small set of original or license-verified bundled presets, including
   Fruitcake default and high contrast, with a saved local preference and live
   preview under Settings > Appearance > Code.
4. Apply the same palette/highlighting contract to inspector files, shared Chat /
   Code fenced blocks and split/unified/pop-out diffs. Preserve strong diff
   contrast and separate finding markers; do not sacrifice readable changed rows
   to syntax colors.

## Follow-On Import / Export

Use a data-only adapter for a documented subset of VS Code color-theme JSON,
mapping editor colors and supported token rules into the internal model. TextMate
`.tmTheme` support is optional later work. Neither format implies complete VS Code
compatibility, semantic analysis or support for arbitrary TextMate grammars.
Validate bounds/colors, report unsupported features and use predictable fallbacks.
Do not install/execute theme extensions or fetch referenced resources.

Reference: https://code.visualstudio.com/api/extension-guides/color-theme

## Acceptance And Verification

- The same Python and Swift source visibly changes syntax palette between themes
  while navigation, sidebar, timestamps and app surfaces remain unchanged.
- File bytes, copy output, line numbers, hashes and review evidence do not change.
- Switching themes preserves selection, Find state, scroll position, tab identity
  and transcript bottom-follow behavior. Stale highlight results cannot publish
  into a different document/version or re-expose content after access revocation.
- Exercise light/dark/high-contrast palettes, multiline tokens, Unicode, empty
  source, unknown languages, large eligible files and malformed theme input.
- Snapshot code/diff views at narrow/wide widths and verify contrast and +/-
  indicators. Use existing fake providers; no real model call is required.
- Native CLI builds/tests require operator authorization under the repo policy;
  never restart the operator's working app as part of verification.

## Sequencing

Close the paired review/inspector branches first. Then create
`codex/code-appearance-client` from the landed native baseline. Keep tokenizer,
palette and bundled-theme work bounded; import/export can be a separate branch.
Browser/testing, Chat directory grants, GitHub delivery and orchestration remain
separately scoped follow-ups. Do not begin their infrastructure in this slice.
