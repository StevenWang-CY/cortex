"""
Context Engine — Terminal Adapter

Captures recent terminal output, detects error blocks (stack traces),
condenses errors to essential information, and identifies root-cause
regions.

Error detection patterns:
- Python tracebacks: "Traceback (most recent call last)"
- JS/Node stack traces: "Error: ... at ..."
- Shell errors: "command not found", "permission denied", exit codes
- Rust panics: "thread 'main' panicked"
- Go panics: "goroutine ... [running]"
- Generic: lines starting with "error:", "fatal:", "FAILED"

Usage:
    adapter = TerminalAdapter()
    adapter.feed_lines(["line1", "line2", ...])
    ctx = adapter.get_context()
"""

from __future__ import annotations

import logging
import re
from collections import deque

from cortex.libs.schemas.context import TerminalContext

logger = logging.getLogger(__name__)

# Maximum lines to keep in history
_MAX_HISTORY = 200

# Error block detection patterns
_ERROR_START_PATTERNS = [
    re.compile(r"Traceback \(most recent call last\)", re.IGNORECASE),
    re.compile(r"^\s*(Error|TypeError|ValueError|KeyError|AttributeError|ImportError"
               r"|ModuleNotFoundError|RuntimeError|SyntaxError|NameError"
               r"|FileNotFoundError|OSError|IOError):", re.MULTILINE),
    re.compile(r"^\s*at\s+\S+\s+\(", re.MULTILINE),  # JS stack trace
    re.compile(r"thread\s+'.*'\s+panicked", re.IGNORECASE),  # Rust
    re.compile(r"goroutine\s+\d+\s+\[running\]"),  # Go
    re.compile(r"^FAILED", re.MULTILINE),
    re.compile(r"^fatal:", re.IGNORECASE | re.MULTILINE),
]

# Single-line error patterns
_ERROR_LINE_PATTERNS = [
    re.compile(r"error:", re.IGNORECASE),
    re.compile(r"command not found", re.IGNORECASE),
    re.compile(r"permission denied", re.IGNORECASE),
    re.compile(r"no such file or directory", re.IGNORECASE),
    re.compile(r"exit\s+(code|status)\s+[1-9]", re.IGNORECASE),
    re.compile(r"npm ERR!", re.IGNORECASE),
    re.compile(r"cargo error", re.IGNORECASE),
    re.compile(r"compilation failed", re.IGNORECASE),
    re.compile(r"^E\s+\w+Error:", re.MULTILINE),  # pytest error lines
]

# Command prompt patterns to detect repeated commands
_COMMAND_PATTERNS = re.compile(
    r"^\s*(?:\$|>>>|>\s|\%|#)\s+(.+)$"
)


class TerminalAdapter:
    """
    Adapter for gathering terminal context.

    Maintains a rolling history of terminal lines, detects error blocks,
    identifies repeated commands, and produces TerminalContext.

    Usage:
        adapter = TerminalAdapter(max_lines=200)
        adapter.feed_lines(new_lines)
        ctx = adapter.get_context()
    """

    def __init__(self, max_lines: int = _MAX_HISTORY) -> None:
        self._history: deque[str] = deque(maxlen=max_lines)
        self._commands: deque[str] = deque(maxlen=50)
        self._running_command: str | None = None

    def feed_lines(self, lines: list[str]) -> None:
        """
        Add new terminal output lines to history.

        Args:
            lines: New lines from terminal.
        """
        for line in lines:
            self._history.append(line)
            # Try to detect commands
            match = _COMMAND_PATTERNS.match(line)
            if match:
                self._commands.append(match.group(1).strip())

    def set_running_command(self, command: str | None) -> None:
        """Set the currently running command."""
        self._running_command = command

    def get_context(self, last_n: int = 50) -> TerminalContext:
        """
        Build TerminalContext from current history.

        Args:
            last_n: Number of recent lines to include.

        Returns:
            TerminalContext with detected errors and repeated commands.
        """
        recent = list(self._history)[-last_n:]
        errors = self._detect_errors(list(self._history))
        repeated = self._find_repeated_commands()

        return TerminalContext(
            last_n_lines=recent,
            detected_errors=errors,
            repeated_commands=repeated,
            running_command=self._running_command,
        )

    def _detect_errors(self, lines: list[str]) -> list[str]:
        """
        Detect error blocks in terminal output.

        Returns condensed error messages (not full stack traces).
        """
        errors: list[str] = []
        full_text = "\n".join(lines)

        # Detect multi-line error blocks (e.g., Python tracebacks)
        traceback_blocks = self._extract_python_tracebacks(lines)
        errors.extend(traceback_blocks)

        # Detect single-line errors
        for line in lines:
            line_stripped = line.strip()
            if not line_stripped:
                continue

            for pattern in _ERROR_LINE_PATTERNS:
                if pattern.search(line_stripped):
                    # Avoid duplicating errors already found in tracebacks
                    if not any(line_stripped in tb for tb in traceback_blocks):
                        condensed = self._condense_error(line_stripped)
                        if condensed and condensed not in errors:
                            errors.append(condensed)
                    break

        return errors[:10]  # Limit to 10 errors

    @staticmethod
    def _extract_python_tracebacks(lines: list[str]) -> list[str]:
        """Extract Python traceback blocks and condense to final error line."""
        results: list[str] = []
        i = 0
        while i < len(lines):
            if "Traceback (most recent call last)" in lines[i]:
                # Scan forward to find the error line (first non-indented,
                # non-empty line after the traceback body)
                j = i + 1
                last_error = ""
                while j < len(lines):
                    raw_line = lines[j]
                    stripped = raw_line.strip()
                    if not stripped:
                        break
                    # In Python tracebacks, stack frames are indented.
                    # The final error line starts at column 0 (no indent).
                    if not raw_line[0].isspace():
                        last_error = stripped
                        break
                    j += 1
                if last_error:
                    results.append(last_error)
                i = j + 1
            else:
                i += 1
        return results

    @staticmethod
    def _condense_error(line: str) -> str:
        """Condense an error line to its essential message."""
        # Trim to reasonable length
        if len(line) > 200:
            return line[:200] + "..."
        return line

    def _find_repeated_commands(self) -> list[str]:
        """Find commands that were run multiple times."""
        cmd_counts: dict[str, int] = {}
        for cmd in self._commands:
            cmd_counts[cmd] = cmd_counts.get(cmd, 0) + 1

        return [cmd for cmd, count in cmd_counts.items() if count >= 2]

    def reset(self) -> None:
        """Reset adapter state."""
        self._history.clear()
        self._commands.clear()
        self._running_command = None
