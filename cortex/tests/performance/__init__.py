# Cortex performance regression tests (audit Phase-I).
#
# These tests guard the four measurable wins shipped in Phase I:
# capture-loop CPU, broadcast throughput, browser-extension bundle
# size, and daemon startup latency. They run as part of the default
# pytest suite — the thresholds are deliberately loose enough that CI
# noise on a moderately loaded shared runner does not flake them, while
# still catching real regressions.
