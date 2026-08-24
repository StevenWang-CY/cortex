# Physiology replay datasets

Raw participant video and traces are never committed. A local replay dataset
is described by a checksum-bearing JSON manifest next to preprocessed `.npz`
files. Each archive contains finite `rgb_trace` (`samples × 3`) and `hr_gt`
arrays. The manifest records dataset/version/license/source, sample rate,
subject ID, sequence ID, a `development` or `evaluation` split, relative path,
and SHA-256.

Subjects may occur in exactly one split. The loader rejects traversal,
duplicates, missing/corrupt files, checksum drift, invalid shapes and split
leakage before evaluation. Reports include coverage/abstention, MAE, RMSE,
correlation when identifiable, bias, 95% limits of agreement, and exact
backend identity. Possession and use of UBFC-rPPG, PURE, or any other dataset
remain subject to its own license and consent terms.

Example manifest shape:

```json
{
  "schema_version": "1.0",
  "dataset_name": "dataset-name",
  "dataset_version": "declared-version",
  "license_name": "declared-license",
  "source_url": "https://authoritative-source.example/dataset",
  "sequences": [
    {
      "subject_id": "subject-001",
      "sequence_id": "subject-001-rest",
      "split": "evaluation",
      "path": "traces/subject-001-rest.npz",
      "sha256": "64-lowercase-hex-characters",
      "sample_rate_hz": 30.0
    }
  ]
}
```

