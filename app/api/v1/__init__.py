"""Version 1 of the public API.

Isolating each API version in its own package lets breaking changes ship
as `v2` without touching `v1` consumers, and lets the app factory mount
multiple versions side by side behind a shared prefix scheme.
"""
