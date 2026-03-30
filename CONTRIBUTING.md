# Contributing to FruitcakeAI

FruitcakeAI is open for public use, testing, and bug reports.

It is not currently open to broad collaborative development. This is still an alpha-stage codebase with an active architectural direction, and the project is not yet in a good place to absorb general external pull requests without creating review and maintenance overhead.

## What Is Welcome Right Now

- bug reports
- setup/installability problems
- reproducible chat/task/runtime regressions
- documentation corrections
- security reports via the private process in [`SECURITY.md`](SECURITY.md)

## What Is Not Open Right Now

- unsolicited feature PRs
- broad refactors
- architectural rewrites
- "drive-by" dependency churn
- partnership-style co-development by default

Code contributions may be accepted later, but for now they are generally by maintainer direction or explicit invitation only.

## Before Opening an Issue

Please do the basic checks first:

1. read the relevant section of [`README.md`](README.md)
2. run `./scripts/doctor.sh`
3. confirm you are on the current `main` branch or latest release tag
4. check whether the issue is already documented in the repo

## How to File a Useful Bug Report

Include the following:

- what you expected to happen
- what actually happened
- exact steps to reproduce
- whether this is backend, client, or both
- the commit or tag you are using
- relevant logs, stack traces, or screenshots
- whether you are using local models, cloud models, or mixed routing

Good bug reports are actionable. Vague reports without repro steps usually are not.

## Pull Requests

If you want to work on something substantial, open an issue first and describe the change before writing code.

Until the project is ready for outside development, assume that:

- issues are welcome
- discussion is welcome
- patches may be declined even if technically correct

That is a project-stage constraint, not a judgment on the work.

## Scope Discipline

If you are proposing a fix, keep it narrow:

- one bug
- one behavior change
- one documentation correction

Mixed PRs are hard to review and easy to reject.

## Security

Do not report vulnerabilities in public issues.

Use the process in [`SECURITY.md`](SECURITY.md).
