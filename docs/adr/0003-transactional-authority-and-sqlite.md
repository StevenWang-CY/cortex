# ADR 0003: Transactional authority and SQLite lifecycle

- Status: Accepted
- Date: 2026-08-25

## Context

Presentation, consent, mutation, and undo were previously coupled; client-side
effects could occur before exact authority and could not be reconciled after a
crash.

## Decision

An effect follows `PROPOSED → PRESENTED → AUTHORIZED → APPLYING → APPLIED →
VERIFIED → RESTORED`. Authority binds an exact manifest digest, capability,
subject, expiry, consent revision, and one-time nonce. Every adapter emits a
typed receipt with minimal inverse data. A single-owner SQLite database with
full synchronization and checksummed migrations is authoritative.

## Consequences

Downgrade materializes a new proposal; it never executes the old manifest.
Duplicate/reordered messages are harmless. Partial failure is compensated or
reported truthfully. Redis and JSON are compatibility/diagnostic stores only.

Evidence: intervention transaction schemas, storage migrations, fault matrix.
