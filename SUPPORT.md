# Support

Cortex is a personal portfolio project maintained by a single author
on a best-effort basis. It is not a commercial product and there is no
support SLA.

**Helpful, in order of preference:**

1. Read the [setup guide](cortex/docs/setup.md) and
   [documentation index](docs/README.md) for the checked-out version.
2. Check [Troubleshooting](wiki/Troubleshooting.md)
   for known issues (camera permission, native messaging,
   stop button, MediaPipe on Apple Silicon, …).
3. Search [closed issues](https://github.com/StevenWang-CY/cortex/issues?q=is%3Aissue+is%3Aclosed)
   before filing a new one.
4. If you still need help, [open an issue](https://github.com/StevenWang-CY/cortex/issues/new/choose)
using the bug-report or feature-request template.

Include the app version, macOS/CPU architecture, installed-versus-source launch
mode, relevant connection/evidence state, and a bounded reproduction. For a
startup failure, include the diagnostic reference and redacted excerpts from
`~/Library/Logs/Cortex/startup.log` or `last-startup-error.txt`. Do not attach
tokens, private workspace content or camera recordings. An unavailable signal
or unconfirmed Stop outcome is useful evidence; do not present it as a success.

The [published wiki](https://github.com/StevenWang-CY/cortex/wiki) may describe an
older release than a development checkout. Current local validation and its
limits are recorded in [the implementation document](docs/implementation-2026-10.md).

For security reports, use **[GitHub Security Advisories](https://github.com/StevenWang-CY/cortex/security/advisories/new)**
— see [SECURITY.md](SECURITY.md).
