# Chrome and Edge extension

The Plasmo/React Manifest V3 extension is an authenticated, untrusted client of
the local Cortex daemon. It provides connection controls, proposal UI, focus
sessions, bounded activity/resume metadata, learning-site helpers, and optional
active-page context.

## Authority boundary

Receiving an intervention or break message renders information only. It does
not close/group/hide tabs, block a site, or change a page. An optional browser
effect runs only after the daemon sends an exact, unexpired authorization for
the displayed manifest. The capability executor validates target, digest,
nonce, mode, and idempotency, then returns a typed receipt for verification and
restore.

Production policy is deterministic. The extension does not run a contextual
bandit, infer biology-driven breaks, or autonomously restructure tabs.

## Privacy

- Static content scripts are limited to declared learning sites.
- Incognito exits before initialization and background rejects incognito
  senders.
- Activity records contain sanitized bounded metadata, not page excerpts or
  source code, and are capped at 200.
- Optional page context requires both Cortex consent for the exact origin and
  browser host permission. Revocation clears current content and attempts to
  remove optional permission.
- URL userinfo, fragments, tracking/secret-like query parameters, and
  credentials are removed before storage.

## Install from source

```bash
cd cortex/apps/browser_extension
pnpm install --frozen-lockfile
pnpm exec plasmo build
pnpm exec plasmo build --target=edge-mv3
```

Load `build/chrome-mv3-prod` or `build/edge-mv3-prod` from the browser's
extensions page in Developer mode.

Install the native host once from the repository environment:

```bash
uv run --project cortex --locked python -m cortex.scripts.install_native_host
```

Then fully quit Chrome/Edge with Cmd+Q and reopen it. Reloading the extension
or closing tabs is insufficient after a native-host manifest change. The
installer uses an absolute interpreter in the installed host copy and includes
detected extension origins; the tracked source shebang remains portable.

## Development gates

```bash
pnpm exec tsc --noEmit
pnpm test
pnpm exec plasmo build
pnpm exec plasmo build --target=edge-mv3
```

Never edit `.plasmo/`; it is generated. New capabilities must add required
manifest permissions, generated wire schemas, producer/consumer tests,
authorization/receipt behavior if mutating, and reduced-motion/keyboard/focus
coverage. See [UI design](cortex/docs/ui-design.md) and
[privacy](cortex/docs/privacy.md).
