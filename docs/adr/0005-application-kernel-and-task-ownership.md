# ADR 0005: Application kernel and structured task ownership

- Status: Accepted
- Date: 2026-08-25

## Context

The runtime daemon, browser worker, popup, and desktop transports had become
parallel orchestrators with divergent behavior and difficult shutdown paths.

## Decision

Cortex remains a local modular monolith. A typed application kernel binds
commands and publishes isolated events; bounded coordinators own sensing,
inference, interventions, policy, context, and lifecycle tasks. Interfaces are
transport adapters. Browser and desktop view models are separated from
connection/effect controllers. Compatibility facades contain migration only.

## Consequences

No network microservice split is introduced. Start is rollback-safe, stop is
idempotent, named tasks are drained, and one desktop domain router serves both
in-process and WebSocket modes.

Evidence: `cortex/application/`, coordinator boundary and shutdown tests.
