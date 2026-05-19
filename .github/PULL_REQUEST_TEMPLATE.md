<!--
Thanks for the PR. Cortex enforces a few things automatically:
  - schema-codegen drift gate (pre-commit + CI)
  - mypy --strict + ruff + pytest in CI
  - eval-regression baseline on PRs touching llm_engine/state_engine/eval

So before opening: run `make ci` locally.
-->

## Summary

<!-- 1-3 sentences. What changes, and why? Link the audit-ledger entry
     if applicable (e.g. "Closes audit F32"). -->

## Audit ledger (if relevant)

<!-- Reference the finding in audit/findings.md and update
     audit/execution-log.md if this PR closes a finding. -->

## Test plan

- [ ] `make ci` passes locally (lint + typecheck + tests + codegen check)
- [ ] Added or updated tests for behaviour I changed
- [ ] Manual smoke test on macOS — describe what you actually clicked

## Schema codegen

- [ ] I did not touch any Pydantic schema  
  *— OR —*
- [ ] I ran `make codegen` and committed the regenerated
      `cortex/apps/browser_extension/types/generated/cortex_schemas.d.ts`
      alongside the Python change

## Privacy invariants ([SECURITY.md](../SECURITY.md))

- [ ] My change does not put biometrics into an LLM payload
- [ ] My change does not persist webcam frames to disk
- [ ] My change does not open a network surface beyond `127.0.0.1`
- [ ] My change does not remove or bypass the consent ladder or the
      capability-token gate

## Screenshots / video (UI changes only)

<!-- Drag-and-drop welcome. Before / after if you can. -->
