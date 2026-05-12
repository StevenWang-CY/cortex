"""
Sliding Window Manager for Real-Time Signal Processing

Manages circular buffers for real-time streaming of rPPG signals.
Supports configurable window size and stride for overlap-add windowing.

Default configuration: 10-second window at 30 FPS (300 frames) with
1-second stride (30 frames). Each stride produces a new analysis window.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


@dataclass
class WindowConfig:
    """Configuration for the sliding window manager."""

    window_seconds: float = 10.0
    stride_seconds: float = 1.0
    fs: float = 30.0
    n_channels: int = 3  # RGB channels for rPPG

    @property
    def window_samples(self) -> int:
        """Number of samples in the analysis window."""
        return int(self.window_seconds * self.fs)

    @property
    def stride_samples(self) -> int:
        """Number of samples per stride."""
        return int(self.stride_seconds * self.fs)


class SlidingWindowManager:
    """
    Circular buffer-based sliding window for real-time signal streaming.

    Accepts samples one at a time or in chunks. Emits complete windows
    at each stride boundary. Handles the case where processing falls behind
    by allowing callers to check if a window is ready without blocking.

    Usage:
        manager = SlidingWindowManager(WindowConfig())
        for sample in stream:
            manager.push(sample)
            if manager.window_ready():
                window = manager.get_window()
                # process window...
    """

    def __init__(self, config: WindowConfig | None = None) -> None:
        self._config = config or WindowConfig()
        self._buffer: deque[NDArray[np.float64]] = deque(
            maxlen=self._config.window_samples
        )
        self._samples_since_last_emit: int = 0
        self._total_samples: int = 0

    @property
    def config(self) -> WindowConfig:
        """Get the window configuration."""
        return self._config

    @property
    def buffer_length(self) -> int:
        """Current number of samples in the buffer."""
        return len(self._buffer)

    @property
    def is_full(self) -> bool:
        """Whether the buffer has enough samples for a complete window."""
        return len(self._buffer) >= self._config.window_samples

    @property
    def total_samples(self) -> int:
        """Total number of samples pushed since creation/reset."""
        return self._total_samples

    def push(self, sample: NDArray[np.float64]) -> None:
        """
        Push a single sample into the circular buffer.

        Args:
            sample: A 1D array of shape (n_channels,) for multi-channel,
                    or a scalar wrapped in an array for single-channel.
        """
        self._buffer.append(np.asarray(sample, dtype=np.float64))
        self._samples_since_last_emit += 1
        self._total_samples += 1

    def push_chunk(self, chunk: NDArray[np.float64]) -> None:
        """
        Push multiple samples at once.

        Args:
            chunk: Array of shape (n_samples,) for single-channel or
                   (n_samples, n_channels) for multi-channel.
        """
        if chunk.ndim == 1:
            for sample in chunk:
                self.push(np.array([sample]))
        else:
            for sample in chunk:
                self.push(sample)

    def window_ready(self) -> bool:
        """
        Check if a new analysis window is ready.

        A window is ready when:
        1. The buffer is full (has window_samples samples)
        2. At least stride_samples new samples have been added since last emit
        """
        return (
            self.is_full
            and self._samples_since_last_emit >= self._config.stride_samples
        )

    def get_window(self) -> NDArray[np.float64]:
        """
        Get the current analysis window and reset the stride counter.

        Returns:
            2D array of shape (window_samples, n_channels) or
            1D array of shape (window_samples,) for single-channel signals.

        Raises:
            RuntimeError: If window is not ready (buffer not full).
        """
        if not self.is_full:
            raise RuntimeError(
                f"Buffer not full: {len(self._buffer)}/{self._config.window_samples} samples"
            )

        self._samples_since_last_emit = 0
        window = np.array(list(self._buffer), dtype=np.float64)

        # Squeeze single-channel to 1D
        if window.ndim == 2 and window.shape[1] == 1:
            window = window.squeeze(axis=1)

        return window

    def get_window_if_ready(self) -> NDArray[np.float64] | None:
        """
        Get the current window if ready, otherwise return None.

        Convenience method combining window_ready() and get_window().

        Returns:
            Window array if ready, None otherwise.
        """
        if self.window_ready():
            return self.get_window()
        return None

    def reset(self) -> None:
        """Clear the buffer and reset all counters."""
        self._buffer.clear()
        self._samples_since_last_emit = 0
        self._total_samples = 0


class MultiChannelWindowManager:
    """
    Manages sliding windows for multiple independent signal channels.

    Wraps multiple SlidingWindowManager instances, one per named channel,
    allowing synchronized windowing across RGB traces, BVP, etc.

    Usage:
        manager = MultiChannelWindowManager(["R", "G", "B"], config)
        manager.push_all({"R": r_val, "G": g_val, "B": b_val})
        if manager.all_windows_ready():
            windows = manager.get_all_windows()
    """

    def __init__(
        self,
        channel_names: list[str],
        config: WindowConfig | None = None,
    ) -> None:
        self._channel_names = channel_names
        self._config = config or WindowConfig()
        self._managers: dict[str, SlidingWindowManager] = {
            name: SlidingWindowManager(
                WindowConfig(
                    window_seconds=self._config.window_seconds,
                    stride_seconds=self._config.stride_seconds,
                    fs=self._config.fs,
                    n_channels=1,
                )
            )
            for name in channel_names
        }

    @property
    def channel_names(self) -> list[str]:
        """Get the list of channel names."""
        return self._channel_names

    def push(self, channel: str, value: float) -> None:
        """Push a single value to a named channel."""
        if channel not in self._managers:
            raise KeyError(f"Unknown channel: {channel}. Available: {self._channel_names}")
        self._managers[channel].push(np.array([value]))

    def push_all(self, values: dict[str, float]) -> None:
        """Push one value per channel simultaneously."""
        for channel, value in values.items():
            self.push(channel, value)

    def all_windows_ready(self) -> bool:
        """Check if all channels have a window ready."""
        return all(m.window_ready() for m in self._managers.values())

    def any_window_ready(self) -> bool:
        """Check if any channel has a window ready."""
        return any(m.window_ready() for m in self._managers.values())

    def get_all_windows(self) -> dict[str, NDArray[np.float64]]:
        """
        Get windows from all channels.

        Returns:
            Dictionary mapping channel names to 1D window arrays.

        Raises:
            RuntimeError: If any channel buffer is not full.
        """
        return {name: mgr.get_window() for name, mgr in self._managers.items()}

    def get_stacked_window(self) -> NDArray[np.float64]:
        """
        Get all channel windows stacked into a 2D array.

        Returns:
            Array of shape (window_samples, n_channels) where channels
            are ordered as in channel_names.
        """
        windows = self.get_all_windows()
        return np.column_stack([windows[name] for name in self._channel_names])

    def reset(self) -> None:
        """Reset all channel buffers."""
        for mgr in self._managers.values():
            mgr.reset()
