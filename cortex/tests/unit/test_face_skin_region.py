"""Whole-face skin sampling: outline minus eyes, brows and lips, rasterised sub-pixel."""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray

from cortex.libs.config.settings import LandmarksConfig
from cortex.services.physio_engine.roi_extractor import (
    _NON_SKIN_FEATURES,
    FACE_OVAL,
    MIN_FACE_SKIN_PIXELS,
    RoiExtractor,
    RoiTrace,
    RoiTraceFrame,
)

SIZE = 320
CX, CY, A, B = 160.0, 150.0, 70.0, 90.0
SKIN_BGR = (80, 120, 180)
FEATURE_BGR = (255, 0, 255)
BACKGROUND_BGR = (10, 200, 10)
PATCH_BGR = (60, 100, 200)
# (centre x, centre y, radius) of left eye, right eye, left brow, right brow, lips.
FEATURE_CIRCLES = ((135, 125, 8), (185, 125, 8), (135, 105, 6), (185, 105, 6), (160, 205, 12))
# Patch ROIs on landmark indices the face mesh topology does not use.
PATCHES = LandmarksConfig(
    forehead=[200, 201, 202, 203],
    left_cheek=[210, 211, 212, 213],
    right_cheek=[220, 221, 222, 223],
)
PATCH_SQUARES = ((150, 70), (125, 160), (195, 160))  # top-left corner, 10 px squares


def _landmarks(*, dx: float = 0.0, scale: float = 1.0) -> NDArray[np.float32]:
    """A 478-point mesh: oval on an ellipse, features as small clusters inside it."""

    points = np.tile(np.asarray([[CX, CY]], dtype=np.float64), (478, 1))
    angles = np.linspace(0.0, 2.0 * np.pi, len(FACE_OVAL), endpoint=False)
    for angle, index in zip(angles, FACE_OVAL, strict=True):
        points[index] = (CX + A * scale * np.sin(angle), CY - B * scale * np.cos(angle))
    for feature, (fx, fy, radius) in zip(_NON_SKIN_FEATURES, FEATURE_CIRCLES, strict=True):
        cluster = np.linspace(0.0, 2.0 * np.pi, len(feature), endpoint=False)
        for angle, index in zip(cluster, feature, strict=True):
            points[index] = (fx + radius * np.cos(angle), fy + radius * np.sin(angle))
    for config_indices, (x0, y0) in zip(
        (PATCHES.forehead, PATCHES.left_cheek, PATCHES.right_cheek), PATCH_SQUARES, strict=True
    ):
        corners = ((x0, y0), (x0 + 10, y0), (x0 + 10, y0 + 10), (x0, y0 + 10))
        for index, corner in zip(config_indices, corners, strict=True):
            points[index] = corner
    points[:, 0] += dx
    return points.astype(np.float32)


def _frame(*, red_gradient: bool = False, patches: bool = False) -> NDArray[np.uint8]:
    frame = np.empty((SIZE, SIZE, 3), dtype=np.uint8)
    frame[:] = BACKGROUND_BGR
    face = np.zeros((SIZE, SIZE), dtype=np.uint8)
    cv2.ellipse(face, (int(CX), int(CY)), (int(A), int(B)), 0, 0, 360, (255,), -1)
    skin = np.empty_like(frame)
    skin[:] = SKIN_BGR
    if red_gradient:
        skin[:, :, 2] = (60 + 0.5 * np.arange(SIZE)).astype(np.uint8)[None, :]
    frame[face > 0] = skin[face > 0]
    for fx, fy, radius in FEATURE_CIRCLES:
        cv2.circle(frame, (fx, fy), radius, FEATURE_BGR, -1)
    if patches:
        for x0, y0 in PATCH_SQUARES:
            frame[y0 - 1 : y0 + 12, x0 - 1 : x0 + 12] = PATCH_BGR
    return frame


def _skin(frame: NDArray[np.uint8], landmarks: NDArray[np.float32]) -> RoiTrace:
    trace = RoiExtractor(PATCHES).extract_face_skin(frame, landmarks)
    assert trace is not None
    return trace


def test_face_skin_mean_is_the_skin_colour_with_features_and_background_excluded() -> None:
    trace = _skin(_frame(), _landmarks())
    assert (trace.r, trace.g, trace.b) == pytest.approx((180.0, 120.0, 80.0))
    assert trace.luma_std == pytest.approx(0.0, abs=1e-9)
    # An order of magnitude more pixels than a forehead/cheek patch.
    assert trace.pixel_count > 10_000


@pytest.mark.parametrize("count", [300, 454, 455, 466])
def test_fewer_landmarks_than_a_full_mesh_return_none(count: int) -> None:
    """Every index the topology uses (up to 466) must exist, or the sample abstains."""

    assert RoiExtractor(PATCHES).extract_face_skin(_frame(), _landmarks()[:count]) is None


def test_degenerate_landmark_arrays_return_none() -> None:
    extractor = RoiExtractor(PATCHES)
    flat = _landmarks().reshape(-1)
    assert extractor.extract_face_skin(_frame(), flat) is None
    broken = _landmarks()
    broken[FACE_OVAL[3]] = (np.nan, np.nan)
    assert extractor.extract_face_skin(_frame(), broken) is None


@pytest.mark.parametrize("scale", [0.05, 0.12], ids=["below-8px-box", "below-pixel-floor"])
def test_tiny_face_returns_none(scale: float) -> None:
    landmarks = _landmarks(scale=scale)
    # Pull the feature clusters in with the outline so only the size differs.
    for feature in _NON_SKIN_FEATURES:
        landmarks[list(feature)] = (CX, CY)
    assert RoiExtractor(PATCHES).extract_face_skin(_frame(), landmarks) is None


def test_quarter_pixel_motion_moves_the_sample_smoothly() -> None:
    """Sub-pixel rasterisation: each quarter-pixel shift moves the mean a quarter step.

    The skin's red channel rises 0.5 per column, so a 0.25 px shift of the
    region should raise the mean red by about 0.125.  A mask snapped to whole
    pixels would instead stay put for some steps and jump a whole column's
    worth on another.
    """

    frame = _frame(red_gradient=True)
    traces = [_skin(frame, _landmarks(dx=0.25 * step)) for step in range(9)]
    reds = np.asarray([trace.r for trace in traces])
    counts = np.asarray([trace.pixel_count for trace in traces])
    steps = np.diff(reds)
    assert np.all(steps > 0.09) and np.all(steps < 0.16), steps
    assert reds[-1] - reds[0] == pytest.approx(0.5 * 2.0, abs=0.05)
    # Pure translation: the area barely changes, never by a face-width row.
    row_width = 2.0 * A * 0.92
    assert np.max(np.abs(np.diff(counts))) < 0.25 * row_width, counts


# ---------------------------------------------------------------------------
# combined_rgb preference
# ---------------------------------------------------------------------------


def _trace(rgb: tuple[float, float, float], *, pixels: int, luma: float) -> RoiTrace:
    return RoiTrace(r=rgb[0], g=rgb[1], b=rgb[2], pixel_count=pixels, luma_mean=luma)


PATCH_A = _trace((100.0, 90.0, 80.0), pixels=150, luma=95.0)
PATCH_B = _trace((110.0, 100.0, 90.0), pixels=140, luma=105.0)


def _roi_frame(face_skin: RoiTrace | None) -> RoiTraceFrame:
    return RoiTraceFrame(
        forehead=PATCH_A, left_cheek=PATCH_B, right_cheek=None, timestamp=1.0, face_skin=face_skin
    )


def test_combined_rgb_prefers_the_face_skin_sample() -> None:
    skin = _trace((180.0, 120.0, 80.0), pixels=MIN_FACE_SKIN_PIXELS, luma=133.0)
    combined = _roi_frame(skin).combined_rgb()
    assert combined is not None
    assert combined.tolist() == [180.0, 120.0, 80.0]


@pytest.mark.parametrize(
    "skin",
    [
        None,
        _trace((180.0, 120.0, 80.0), pixels=MIN_FACE_SKIN_PIXELS - 1, luma=133.0),
        _trace((180.0, 120.0, 80.0), pixels=5_000, luma=15.0),
        _trace((250.0, 250.0, 250.0), pixels=5_000, luma=240.0),
    ],
    ids=["absent", "too-small", "too-dark", "saturated"],
)
def test_combined_rgb_falls_back_to_the_patch_rule(skin: RoiTrace | None) -> None:
    combined = _roi_frame(skin).combined_rgb()
    assert combined is not None
    assert combined.tolist() == pytest.approx([105.0, 95.0, 85.0])


def test_dark_skin_is_a_valid_face_sample_below_the_patch_gate() -> None:
    dark = _trace((40.0, 25.0, 20.0), pixels=5_000, luma=25.0)
    combined = _roi_frame(dark).combined_rgb()
    assert combined is not None and combined.tolist() == [40.0, 25.0, 20.0]


def test_patch_rule_still_fails_closed_with_one_region() -> None:
    single = RoiTraceFrame(
        forehead=PATCH_A, left_cheek=None, right_cheek=None, timestamp=1.0, face_skin=None
    )
    assert single.combined_rgb() is None


# ---------------------------------------------------------------------------
# skin_region switch
# ---------------------------------------------------------------------------


def test_face_mode_extract_samples_the_whole_face() -> None:
    frame = _frame(patches=True)
    roi = RoiExtractor(PATCHES, skin_region="face").extract(frame, _landmarks(), 1.0)
    assert roi.face_skin is not None
    combined = roi.combined_rgb()
    assert combined is not None
    assert combined.tolist() == [roi.face_skin.r, roi.face_skin.g, roi.face_skin.b]


def test_patches_mode_never_computes_the_face_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*_args: object, **_kwargs: object) -> RoiTrace | None:
        raise AssertionError("face skin must not be computed in patches mode")

    extractor = RoiExtractor(PATCHES, skin_region="patches")
    monkeypatch.setattr(extractor, "extract_face_skin", _forbidden)
    roi = extractor.extract(_frame(patches=True), _landmarks(), 1.0)
    assert roi.face_skin is None
    combined = roi.combined_rgb()
    assert combined is not None
    # BGR (60, 100, 200) painted under every patch.
    assert combined.tolist() == pytest.approx([200.0, 100.0, 60.0])


def test_invalid_skin_region_is_rejected() -> None:
    with pytest.raises(ValueError):
        RoiExtractor(PATCHES, skin_region="cheeks")
