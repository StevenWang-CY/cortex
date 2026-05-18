"""API gateway middleware package.

Hosts cross-cutting HTTP middleware (rate limiting, etc.). The
correlation-id middleware remains inline in ``app.py`` because it is the
outermost wrapper and is part of the application's startup contract; the
helpers here are layered after correlation so every rate-limit decision
already carries a bound ``cid`` in its log line.
"""
