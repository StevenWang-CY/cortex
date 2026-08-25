# Cortex model assets

This directory does **not** contain a neural rPPG model. Cortex supports the
packaged POS, CHROM, and green-channel implementations only. The former
`tscan` setting was removed because no checkpoint, training provenance,
preprocessing contract, license, or checksum shipped with the application;
silently substituting POS made the configured algorithm identity false.

A neural backend may be added only with a versioned backend manifest, exact
pre/post-processing contract, licensed model asset and SHA-256 checksum, plus
the held-out and reference-sensor validation described in `IMPLEMENTATION.md`.
