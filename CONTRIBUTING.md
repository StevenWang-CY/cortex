# Contributing to Cortex

Thanks for your interest. Cortex is a personal portfolio project, but
patches, bug reports, and ideas are welcome.

> Cortex is macOS-only. Linux and Windows are not supported and
> patches that add support are out of scope; the whole stack is tied to
> AVFoundation, TCC, and macOS-specific frameworks.

## Prerequisites

Every toolchain version is pinned in the repository and enforced by the
build and release scripts; matching them exactly avoids lockfile drift.

| Requirement | Pin | Install |
|-------------|-----|---------|
| macOS 13+ (Ventura or later) | — | required |
| uv | `0.10.12` (`cortex/pyproject.toml` `tool.uv.required-version`) | install the pinned version and verify `uv --version` |
| Python | `3.11.15` (`.python-version`; CI also validates 3.12.13 on Intel) | `uv python install 3.11.15` |
| Node.js | `22.23.2` (`.node-version`) | your version manager (`fnm`, `mise`, `nvm`) |
| pnpm | `9.15.9` (`packageManager` in the extension `package.json`) | `corepack prepare pnpm@9.15.9 --activate` |
| Claude access (optional) | — | Bedrock bearer token in Keychain, GCP Vertex ADC, or `ANTHROPIC_API_KEY`; the default planner mode makes no network call and the daemon falls back to deterministic plans without credentials |

## One-time setup

```bash
git clone https://github.com/StevenWang-CY/cortex.git
cd cortex
make setup            # uv sync --locked, pnpm/npm frozen installs, seed storage
make precommit        # pre-commit hooks (schema-codegen and token drift gates)
cp cortex/.env.example .env
```

Common shortcuts (see [Makefile](Makefile)):

```bash
make dev              # start the daemon
make test             # pytest
make lint             # ruff
make typecheck        # mypy --strict
make codegen          # regenerate TypeScript schema types
make codegen-check    # the CI drift gate
make ci               # local Python/contract subset of the hosted workflow
./cortex/scripts/run_python_gate.sh  # canonical lint/types/wheel/tests/coverage gate
```

## Schema codegen workflow

This is the project's most important convention. The Pydantic models in
[cortex/libs/schemas/](cortex/libs/schemas/) are the single source of
truth for every shape that crosses the daemon ↔ browser-extension
boundary. The generated TypeScript declarations at
[cortex/apps/browser_extension/types/generated/cortex_schemas.d.ts](cortex/apps/browser_extension/types/generated/cortex_schemas.d.ts)
and [cortex/apps/vscode_extension/src/generated/cortex_schemas.d.ts](cortex/apps/vscode_extension/src/generated/cortex_schemas.d.ts)
are hands-off:

1. Edit a Pydantic model in `cortex/libs/schemas/`.
2. Run `make codegen` to regenerate both `.d.ts` files.
3. Commit the Python change and both regenerated `.d.ts` files
   together.
4. The pre-commit hook and the `schema-codegen-check` CI job both
   run `make codegen-check`, which fails the commit / PR if the
   `.d.ts` is stale.

Don't hand-edit generated declarations; regenerate them from their Python source.

## Algorithm, UI and lifecycle changes

Read the [current implementation record](docs/implementation-2026-10.md),
[model card](docs/model-cards/deterministic-support-v2.md) and
[UI contract](cortex/docs/ui-design.md) before changing their invariants.

- Input availability is distinct from measured zero. Observation gaps cannot
  satisfy dwell, and camera physiology remains outside behavioral inference.
- After changing a declared behavioral component, regenerate/check
  `cortex.scripts.generate_support_model_identity` using the locked interpreter;
  review whether the model version, feature schema and model card must change.
- Design tokens come from `cortex/libs/design/tokens.yaml`; regenerate with
  `cortex.scripts.sync_design_tokens`. Generated configuration references come
  from `make config-sync`. Do not hand-edit generated output.
- Test failure, disconnect, cancellation, stop and restart paths. Use real Qt
  event-loop tests when changing quit/focus lifecycle; a mock `quit()` does not
  establish process exit. Inspect final affected UI states at compact sizes and
  with the relevant accessibility preferences.
- Tests inherit the session HOME/XDG sandbox in `cortex/tests/conftest.py`.
  Never bypass it for normal tests, use real cameras or modify user profiles.
- Run browser TypeScript/Vitest/build and editor lint/compile/Jest/package
  gates when their surfaces change. A local `make ci` does not run all of them.
  Release-sensitive changes also require the [packaged checks](docs/release/README.md).

Use descriptive branch names without `codex` or `agent`. Preserve the intended
commit author and any required DCO sign-off; do not add an AI coauthor trailer.
Keep generated builds, coverage output, local logs and credentials out of Git.

## Adding a new WebSocket message type

1. Add a member to `cortex/libs/schemas/ws_message_types.py::MessageType`.
2. If the daemon dispatches on it, add a case in
   `cortex/services/api_gateway/websocket_server.py::_process_message`
   and update the catalog tests.
3. Run `make codegen` and commit both regenerated `.d.ts` files alongside.
4. The TypeScript dispatch sites in `background.ts` / `popup.tsx` now
   type-check against the new union; their switch's `never` default
   flags any forgotten case at build time.

See [cortex/docs/apis.md](cortex/docs/apis.md) for the full message
catalog.

## Audit-ledger commit convention

Significant fixes that close a known issue use the `audit Fxx:`
prefix and link the corresponding entry in
[audit/findings.md](audit/findings.md), e.g.:

```
audit F19: end-to-end correlation IDs across HTTP + WS + logs
```

Smaller fixes use a Conventional-Commits-flavoured prefix
(`docs:`, `meta:`, `devx:`, `test:`, `fix:`, `audit Fxx:`). See
`git log --oneline` for examples.

## Pull request checklist

Before submitting:

- [ ] The canonical Python gate and relevant browser/editor/build gates pass;
      documentation-only changes pass local link/contract and whitespace checks
- [ ] Behavior changes have meaningful regression coverage
- [ ] If you touched a Pydantic schema, you ran `make codegen` and
      committed both regenerated `.d.ts` files
- [ ] Model/config/token documentation and generated identities remain in sync
- [ ] README examples and current guides describe the implemented behavior;
      historical measurements remain bound to their original milestone
- [ ] If your change relates to an audit finding, the commit subject
      starts with `audit Fxx:` and `audit/execution-log.md` is updated
- [ ] No new `print()` calls (use `structlog` via
      `cortex.libs.logging.event`)
- [ ] No biometric or webcam-frame data added to any LLM prompt
      path (see [SECURITY.md](SECURITY.md))

## What we won't merge

- Linux or Windows support (Cortex is tied to AVFoundation, TCC, and
  macOS-specific frameworks)
- Any change that puts biometrics into an LLM payload
- Removal of exact authorization, durable receipt/restore safeguards, consent
  boundaries or the capability-token gate
- Bypassing the schema codegen drift gate

## Bug reports & ideas

Use the [issue templates](.github/ISSUE_TEMPLATE/). For security
issues, file a [private security advisory](https://github.com/StevenWang-CY/cortex/security/advisories/new)
instead.

## License

By contributing, you agree your contribution is licensed under the
[MIT License](LICENSE).
