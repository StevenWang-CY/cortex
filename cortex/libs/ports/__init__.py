"""Cortex port-and-adapter boundaries.

This package gathers ``Protocol`` definitions that decouple the
api_gateway (and other producers) from concrete service classes. The
goal is for routes / WS handlers to depend on the *capability* (a
Protocol declared here) rather than the concrete
``cortex.services.intervention_engine`` import chain — which lets us
swap a stub in tests without monkey-patching the import system.

Membership policy
-----------------

Only Protocols belong here. No implementations. No imports from
``cortex.services.*`` (otherwise we re-introduce the coupling). If a
new boundary needs a new Protocol, add it as its own module under this
package and export it from this ``__init__``.
"""

from __future__ import annotations

from cortex.libs.ports.intervention_port import InterventionPort

__all__ = ["InterventionPort"]
