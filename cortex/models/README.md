# Cortex rPPG Models

`tscan.onnx` is the optional deep-learning backend artifact for rPPG (`CORTEX_SIGNAL__RPPG__BACKEND=tscan`).

Runtime behavior:
- If `tscan.onnx` is present and loadable, Cortex uses it.
- If missing or invalid, Cortex automatically falls back to POS.

To keep repository size manageable, teams may replace this with their own ONNX-exported TSCAN checkpoint.
