# Privacy

Cortex is local-first and model-network-off by default.

## Always local

- Raw camera frames, landmarks, waveforms, IBIs, and raw physiological
  observations are not fields in the planner contract and are not ordinarily
  persisted.
- Mouse/keyboard collection uses aggregate timing/rates and does not capture
  key text.
- FastAPI, WebSocket, and launcher endpoints bind to `127.0.0.1` and sensitive
  operations require a capability token.
- Credentials belong in macOS Keychain or the provider's supported credential
  store; they are not bundled in the app.

## Optional external planning

`external_redacted` requires all of the following for each send:

1. revision-bound external mode;
2. explicit selection of each context source;
3. deterministic normalization, minimization, redaction, and size caps;
4. an exact preview of payload, prompt, destination, model, byte count, field
   dispositions, redactions, and retention caveat;
5. one-time confirmation before the handle expires.

Previewing does not contact a provider. The handle is memory-only and burned
before network I/O. Page-body extraction additionally requires an exact active
HTTP(S) origin grant and is disabled in incognito. Revocation clears the
snapshot and scrubs stored content fields.

Redaction is defense in depth, not a guarantee. Do not select credentials,
private keys, regulated records, or third-party confidential material. Cortex
cannot verify an account's provider retention contract, region, logging,
caching, or abuse-monitoring settings.

## Local retention and deletion

SQLite stores bounded operational, intervention, and policy lifecycle data.
Browser resume metadata is capped at 200 records. Export/delete is scoped and
authenticated; destructive deletion requires `DELETE CORTEX DATA` and refuses
to erase unresolved restore evidence. Uninstalling the app alone does not
remove browser/editor storage, native-host manifests, Keychain credentials, or
the configured storage root.

Read the exact [privacy disclosure](cortex/docs/privacy.md) and
[data-flow/deletion map](docs/data-flow.md) before enabling external context.
