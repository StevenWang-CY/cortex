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
| uv | `0.10.12` (`cortex/pyproject.toml` `tool.uv.required-version`) | `brew install uv` |
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
make ci               # everything CI runs
```

## Schema codegen workflow

This is the project's most important convention. The Pydantic models in
[cortex/libs/schemas/](cortex/libs/schemas/) are the single source of
truth for every shape that crosses the daemon ↔ browser-extension
boundary. The generated TypeScript file at
[cortex/apps/browser_extension/types/generated/cortex_schemas.d.ts](cortex/apps/browser_extension/types/generated/cortex_schemas.d.ts)
is hands-off:

1. Edit a Pydantic model in `cortex/libs/schemas/`.
2. Run `make codegen` to regenerate the `.d.ts`.
3. Commit both the Python change and the regenerated `.d.ts`
   together.
4. The pre-commit hook and the `schema-codegen-check` CI job both
   run `make codegen-check`, which fails the commit / PR if the
   `.d.ts` is stale.

Don't hand-edit the `.d.ts`. The file's header refuses it.

## Adding a new WebSocket message type

1. Add a member to `cortex/libs/schemas/ws_message_types.py::MessageType`.
2. If the daemon dispatches on it, add a case in
   `cortex/services/api_gateway/websocket_server.py::_process_message`
   and update the catalog tests.
3. Run `make codegen` and commit the regenerated `.d.ts` alongside.
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

- [ ] `make ci` passes locally (lint + typecheck + tests + codegen
      drift check)
- [ ] You added or updated tests for any code change
- [ ] If you touched a Pydantic schema, you ran `make codegen` and
      committed the regenerated `.d.ts`
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
- Removal of the consent ladder, undo stack, or capability-token gate
- Bypassing the schema codegen drift gate

## Bug reports & ideas

Use the [issue templates](.github/ISSUE_TEMPLATE/). For security
issues, file a [private security advisory](https://github.com/StevenWang-CY/cortex/security/advisories/new)
instead.

## License

By contributing, you agree your contribution is licensed under the
[MIT License](LICENSE).
