# Physiology replay datasets

Raw participant video and traces are never committed. A local replay dataset
is described by a v1.1 checksum-bearing JSON manifest next to preprocessed
`.npz` files. Each archive contains finite `rgb_trace` (`samples × 3`) and
`hr_gt` arrays. The manifest records immutable dataset/version/source,
license/terms/citation, reference sensor, clock-alignment method, acquisition
condition, sample rate, pseudonymous subject and sequence IDs, a `development`
or `evaluation` split, relative path, and SHA-256. It must explicitly state
that participant data is not committed.

Subjects may occur in exactly one split. The loader rejects traversal,
duplicates, missing/corrupt files, checksum drift, invalid shapes and split
leakage before evaluation. Reports include coverage/abstention, MAE, RMSE,
correlation when identifiable, bias, 95% limits of agreement, and exact
backend identity. Possession and use of UBFC-rPPG, PURE, or any other dataset
remain subject to its own license and consent terms.

Start from [manifest.example.json](manifest.example.json); the machine-readable
contract is [manifest.schema.json](manifest.schema.json). The example contains
no usable trace or participant data.

Run a held-out report and optionally enforce preregistered gates:

```bash
python -m cortex.scripts.validate_dataset_manifest /secure/path/manifest.json \
  --split evaluation \
  --minimum-coverage 0.90 \
  --maximum-mae-bpm 5 \
  --maximum-absolute-bias-bpm 3 \
  --maximum-p95-error-bpm 10 \
  --output validation-results/hr-pos.json
```

The report is bound to manifest and backend implementation SHA-256 values and
includes pooled plus per-condition coverage/error metrics. Thresholds are
study-specific proposed product gates, not medical standards. A passing replay
does not establish clinical validity or support-state validity.

Example manifest shape:

```json
{
  "schema_version": "1.1",
  "dataset_name": "dataset-name",
  "dataset_version": "declared-version",
  "license_name": "declared-license",
  "license_url": "https://authoritative-source.example/license",
  "source_url": "https://authoritative-source.example/dataset",
  "citation": "required citation",
  "data_use_notes": "approved use and redistribution limits",
  "reference_sensor": "device, firmware, channel, sample rate",
  "clock_alignment_method": "sync signal and residual error",
  "participant_data_committed": false,
  "sequences": [
    {
      "subject_id": "subject-001",
      "sequence_id": "subject-001-rest",
      "split": "evaluation",
      "path": "traces/subject-001-rest.npz",
      "sha256": "64-lowercase-hex-characters",
      "sample_rate_hz": 30.0,
      "condition": "stationary"
    }
  ]
}
```
