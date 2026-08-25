# ADR 0004: Preview-bound external context disclosure

- Status: Accepted
- Date: 2026-08-25

## Context

Workspace context can contain secrets and third-party confidential data.
Schema validation alone cannot make arbitrary text safe or grant authority.

## Decision

The default planner is local and network-off. External planning requires a
revision-bound mode, per-source opt-in, deterministic minimization/redaction,
an exact payload/prompt preview, and a short-lived one-time confirmation handle
burned before network I/O. Browser page content additionally requires an exact
origin grant. Provider retention is disclosed conservatively, never asserted
as zero without external proof.

## Consequences

Changing any field invalidates the preview. Secrets remain the user's final
review responsibility. Model output remains untrusted proposal data and cannot
mint action authority.

Evidence: [privacy disclosure](../../cortex/docs/privacy.md),
[data flow](../data-flow.md).
