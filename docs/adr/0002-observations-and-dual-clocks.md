# ADR 0002: Observation envelopes and dual clocks

- Status: Accepted
- Date: 2026-08-25

## Context

Bare timestamps mixed UNIX and monotonic time, while dropped sensor intervals
could disappear instead of becoming missing evidence.

## Decision

Every scheduled source interval yields a valid, missing, rejected, or stale
observation with reason, quality, sequence, source identity, algorithm version,
`observed_at_unix_ms`, `observed_at_mono_ns`, and `boot_id`. Wall time is for
display/persistence; same-boot monotonic time is for elapsed duration.

## Consequences

Callers cannot preserve stale values as present evidence. Replays and restart
logic must explicitly translate clocks. Compatibility `timestamp` fields are
decode-only and cannot define new domain behavior.

Evidence: `cortex/application/clock.py`, observation schema/property tests.
