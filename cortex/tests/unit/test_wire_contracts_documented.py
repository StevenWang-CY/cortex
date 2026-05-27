"""P2-10: WIRE_CONTRACTS documentation test.

Verifies that the cortex.libs.schemas package docstring contains the
three mandatory contract keywords: 'epoch', 'enum', 'null'.
"""

from __future__ import annotations

import cortex.libs.schemas as schemas_pkg


class TestWireContractsDocumented:
    def test_package_docstring_exists(self) -> None:
        """The schemas package must have a module docstring."""
        assert schemas_pkg.__doc__ is not None, (
            "cortex.libs.schemas must have a module docstring"
        )

    def test_epoch_documented(self) -> None:
        """Docstring must describe the 'epoch' timestamp convention."""
        assert "epoch" in schemas_pkg.__doc__, (
            "WIRE_CONTRACTS doc must mention 'epoch' (timestamp unit)"
        )

    def test_enum_documented(self) -> None:
        """Docstring must describe the enum wire policy."""
        assert "enum" in schemas_pkg.__doc__.lower(), (
            "WIRE_CONTRACTS doc must mention 'enum' (serialisation policy)"
        )

    def test_null_documented(self) -> None:
        """Docstring must describe the null/absent field policy."""
        assert "null" in schemas_pkg.__doc__.lower(), (
            "WIRE_CONTRACTS doc must mention 'null' (optional field policy)"
        )

    def test_all_three_keywords_present(self) -> None:
        """Combined: all three keywords must appear in the docstring."""
        doc = schemas_pkg.__doc__.lower()
        missing = [kw for kw in ("epoch", "enum", "null") if kw not in doc]
        assert not missing, (
            f"WIRE_CONTRACTS doc missing keywords: {missing}"
        )
