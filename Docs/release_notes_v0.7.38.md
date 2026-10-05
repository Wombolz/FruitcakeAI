# Release Notes v0.7.38

## Summary

This release establishes the settings and identity foundation needed for a stronger multi-user Fruitcake deployment. Users now have stable public identity and personal assistant preferences, model capabilities can be managed without restarting the backend, calendar credentials can be owned by individual users, and administrators can control which models each user may access.

## Included Changes

- added stable public user identifiers and versioned assistant preference records
- added layered settings resolution across deployment defaults, user preferences, and explicit request overrides
- added persistent model profiles for capabilities, reasoning levels, tool policy, local keep-alive, and display state
- added hot model-profile refresh so supported changes apply without a backend restart
- added encrypted per-user Apple/CalDAV and Google Calendar integrations
- retained deployment calendar credentials as an explicit fallback for installations that have not migrated yet
- added administrator-managed per-user model access policy
- enforced model access policy in model listings, settings resolution, and chat session model changes
- added migrations `045_user_settings_foundation`, `046_model_profiles`, `047_user_integrations`, and `048_user_model_access`

## Compatibility And Operations

- existing users continue to resolve deployment defaults until personal preferences are saved
- existing deployment-level calendar credentials remain supported as a fallback
- database migrations must be applied before starting the updated backend
- model profiles are seeded from configured models and can then be managed through the administrator API
- no existing chat or task wire contract is removed

## Verification

- focused settings, model-profile, integration, access-policy, and chat-validation suites: 56 passed
- test assertions completed successfully; the known third-party pytest shutdown-thread issue required terminating the finished process afterward
- `git diff --check` passed before release preparation
