"""
Physio Engine — rPPG Blood Volume Pulse Extraction

Implements three rPPG algorithms for extracting blood volume pulse (BVP)
signals from RGB face traces:

1. POS (Plane Orthogonal to Skin) — primary, best accuracy (MAE < 3 BPM)
2. CHROM (Chrominance-Based) — fallback, better cross-skin-tone performance
3. Green-Channel — simplest baseline reference

All algorithms consume RGB trace windows (10s at 30fps = 300 samples x 3 channels)
and produce a 1D BVP signal suitable for heart rate estimation.

References:
- POS: Wang et al., "Algorithmic Principles of Remote PPG" (2017)
- CHROM: de Haan & Jeanne, "Robust Pulse Rate from Chrominance-Based rPPG" (2013)
"""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

logger = logging.getLogger(__name__)

try:
    import onnxruntime as ort
except Exception:  # pragma: no cover - optional runtime path
    ort = None  # type: ignore[assignment]


class RPPGAlgorithm(StrEnum):
    """Available rPPG extraction algorithms."""
    POS = "pos"
    CHROM = "chrom"
    GREEN = "green"
    TSCAN = "tscan"


_TSCAN_SESSION: ort.InferenceSession | None = None if ort is not None else None
_TSCAN_MODEL_PATH: str | None = None


def _normalize_backend(algorithm: RPPGAlgorithm | str) -> RPPGAlgorithm:
    if isinstance(algorithm, RPPGAlgorithm):
        return algorithm
    try:
        return RPPGAlgorithm(str(algorithm).lower())
    except ValueError:
        logger.warning("Unknown rPPG backend '%s', falling back to POS", algorithm)
        return RPPGAlgorithm.POS


def _load_tscan_session(model_path: str) -> ort.InferenceSession | None:
    global _TSCAN_SESSION, _TSCAN_MODEL_PATH
    if ort is None:
        logger.warning("onnxruntime unavailable; TSCAN backend disabled")
        return None
    path = str(Path(model_path))
    if _TSCAN_SESSION is not None and _TSCAN_MODEL_PATH == path:
        return _TSCAN_SESSION
    if not Path(path).exists():
        logger.warning("TSCAN model missing at '%s'; falling back to POS", path)
        return None
    try:
        _TSCAN_SESSION = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
        _TSCAN_MODEL_PATH = path
        return _TSCAN_SESSION
    except Exception as exc:  # pragma: no cover - model-specific runtime
        logger.warning("Failed loading TSCAN model '%s': %s", path, exc)
        return None


def extract_bvp_tscan(
    rgb_window: NDArray[np.float64],
    *,
    model_path: str = "cortex/models/tscan.onnx",
) -> NDArray[np.float64] | None:
    """
    Run TSCAN ONNX inference to produce a BVP signal.

    If the model is unavailable or input/output shapes mismatch, returns None.
    The caller should fall back to POS/CHROM.
    """
    session = _load_tscan_session(model_path)
    if session is None:
        return None
    if rgb_window.ndim != 2 or rgb_window.shape[1] != 3:
        return None

    inp = session.get_inputs()[0]
    x = rgb_window.astype(np.float32)
    # Center and scale for stable model input.
    x = (x - np.mean(x, axis=0, keepdims=True)) / (np.std(x, axis=0, keepdims=True) + 1e-6)

    shape = inp.shape
    rank = len(shape)
    if rank == 2:
        model_input = x
    elif rank == 3:
        model_input = x[None, ...]  # [1, T, 3]
    elif rank == 4:
        # Best-effort channel-first temporal layout [1, 3, T, 1].
        model_input = np.transpose(x, (1, 0))[None, :, :, None]
    else:
        logger.warning("Unsupported TSCAN input rank: %s", rank)
        return None

    try:
        outputs = session.run(None, {inp.name: model_input})
    except Exception as exc:  # pragma: no cover - model/runtime specific
        logger.warning("TSCAN inference failed: %s", exc)
        return None

    if not outputs:
        return None
    y = np.asarray(outputs[0], dtype=np.float64).reshape(-1)
    if y.size < 2:
        return None
    y = y - np.mean(y)
    return y


def extract_bvp_pos(
    rgb_window: NDArray[np.float64],
    fs: float = 30.0,
    window_length: int = 45,
) -> NDArray[np.float64]:
    """
    POS (Plane Orthogonal to Skin) rPPG algorithm.

    Steps:
    1. Temporally normalize each channel by dividing by running mean
    2. Project onto chrominance axes S1 and S2
    3. Combine using adaptive ratio of standard deviations
    4. Apply overlap-add windowing for continuous BVP

    Args:
        rgb_window: RGB traces, shape (N, 3) where columns are [R, G, B].
                    Values are mean pixel intensities (0-255 range).
        fs: Sampling frequency in Hz.
        window_length: Sub-window length in samples for overlap-add processing.
                      Default 45 (~1.5s at 30fps).

    Returns:
        BVP signal of shape (N,).
    """
    n_samples = rgb_window.shape[0]

    if n_samples < window_length:
        # Fall back to simple processing for short signals
        return _pos_single_window(rgb_window)

    # Overlap-add BVP reconstruction
    bvp = np.zeros(n_samples, dtype=np.float64)
    overlap_count = np.zeros(n_samples, dtype=np.float64)

    stride = window_length // 2  # 50% overlap
    for start in range(0, n_samples - window_length + 1, stride):
        end = start + window_length
        sub_window = rgb_window[start:end]

        sub_bvp = _pos_single_window(sub_window)

        # Apply Hanning window for smooth overlap-add
        hann = np.hanning(window_length)
        bvp[start:end] += sub_bvp * hann
        overlap_count[start:end] += hann

    # Normalize by overlap count
    nonzero = overlap_count > 0
    bvp[nonzero] /= overlap_count[nonzero]

    return bvp


def _pos_single_window(rgb_window: NDArray[np.float64]) -> NDArray[np.float64]:
    """
    Apply POS algorithm to a single temporal window.

    Args:
        rgb_window: Shape (N, 3) with [R, G, B] columns.

    Returns:
        BVP signal of shape (N,).
    """
    n = rgb_window.shape[0]
    if n < 2:
        return np.zeros(n, dtype=np.float64)

    # Step 1: Temporal normalization — divide by running mean
    # Use column-wise mean for the window
    mean_rgb = np.mean(rgb_window, axis=0, keepdims=True)
    mean_rgb = np.maximum(mean_rgb, 1e-6)  # Avoid division by zero
    normalized = rgb_window / mean_rgb

    # Step 2: Project onto chrominance axes
    # POS projection matrix: S1 and S2
    # S1 = G - B (green-blue difference)
    # S2 = G + B - 2*R (complement of red)
    s1 = normalized[:, 1] - normalized[:, 2]  # G - B
    s2 = normalized[:, 1] + normalized[:, 2] - 2.0 * normalized[:, 0]  # G + B - 2R

    # Step 3: Adaptive combination using standard deviation ratio
    std_s1 = np.std(s1)
    std_s2 = np.std(s2)

    if std_s2 < 1e-10:
        # S2 has no variance — use S1 only
        bvp = s1
    else:
        alpha = std_s1 / std_s2
        bvp = s1 + alpha * s2

    # Zero-mean the output
    bvp -= np.mean(bvp)

    return bvp


def extract_bvp_chrom(
    rgb_window: NDArray[np.float64],
    fs: float = 30.0,
) -> NDArray[np.float64]:
    """
    CHROM (Chrominance-Based) rPPG algorithm.

    Better cross-skin-tone performance than POS. Uses fixed chrominance
    projection coefficients derived from skin color model.

    Steps:
    1. Temporally normalize each channel
    2. Compute chrominance signals Xs and Ys
    3. Combine using standard deviation ratio

    Args:
        rgb_window: RGB traces, shape (N, 3) with [R, G, B] columns.
        fs: Sampling frequency in Hz.

    Returns:
        BVP signal of shape (N,).
    """
    n = rgb_window.shape[0]
    if n < 2:
        return np.zeros(n, dtype=np.float64)

    # Temporal normalization
    mean_rgb = np.mean(rgb_window, axis=0, keepdims=True)
    mean_rgb = np.maximum(mean_rgb, 1e-6)
    normalized = rgb_window / mean_rgb

    r_n = normalized[:, 0]
    g_n = normalized[:, 1]
    b_n = normalized[:, 2]

    # CHROM chrominance signals
    # Xs = 3R - 2G (heavily weights red)
    # Ys = 1.5R + G - 1.5B
    xs = 3.0 * r_n - 2.0 * g_n
    ys = 1.5 * r_n + g_n - 1.5 * b_n

    # Combine using standard deviation ratio
    std_xs = np.std(xs)
    std_ys = np.std(ys)

    if std_ys < 1e-10:
        bvp = xs
    else:
        alpha = std_xs / std_ys
        bvp = xs - alpha * ys

    # Zero-mean
    bvp -= np.mean(bvp)

    return bvp


def extract_bvp_green(
    rgb_window: NDArray[np.float64],
    fs: float = 30.0,
) -> NDArray[np.float64]:
    """
    Green-channel baseline rPPG method.

    Simplest approach: uses the green channel directly since hemoglobin
    absorption peaks near 540nm (green wavelength). Serves as a reference
    and lowest-quality fallback.

    Args:
        rgb_window: RGB traces, shape (N, 3) with [R, G, B] columns.
        fs: Sampling frequency in Hz.

    Returns:
        BVP signal of shape (N,).
    """
    n = rgb_window.shape[0]
    if n < 2:
        return np.zeros(n, dtype=np.float64)

    # Green channel is column index 1
    green = rgb_window[:, 1].copy()

    # Temporal normalization (divide by mean)
    mean_g = np.mean(green)
    if mean_g < 1e-6:
        return np.zeros(n, dtype=np.float64)

    green = green / mean_g

    # Zero-mean
    green -= np.mean(green)

    return green


def extract_bvp(
    rgb_window: NDArray[np.float64],
    algorithm: RPPGAlgorithm | str = RPPGAlgorithm.POS,
    fs: float = 30.0,
    *,
    model_path: str = "cortex/models/tscan.onnx",
) -> NDArray[np.float64]:
    """
    Extract BVP signal using the specified algorithm.

    Convenience wrapper that dispatches to the appropriate algorithm.

    Args:
        rgb_window: RGB traces, shape (N, 3).
        algorithm: Which rPPG algorithm to use.
        fs: Sampling frequency in Hz.

    Returns:
        BVP signal of shape (N,).
    """
    backend = _normalize_backend(algorithm)
    if backend == RPPGAlgorithm.POS:
        return extract_bvp_pos(rgb_window, fs)
    elif backend == RPPGAlgorithm.CHROM:
        return extract_bvp_chrom(rgb_window, fs)
    elif backend == RPPGAlgorithm.GREEN:
        return extract_bvp_green(rgb_window, fs)
    elif backend == RPPGAlgorithm.TSCAN:
        tscan_bvp = extract_bvp_tscan(rgb_window, model_path=model_path)
        if tscan_bvp is not None:
            return tscan_bvp
        # HEURISTIC fallback path.
        return extract_bvp_pos(rgb_window, fs)
    else:
        raise ValueError(f"Unknown algorithm: {backend}")
