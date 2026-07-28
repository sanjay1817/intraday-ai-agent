"""Options-trading infrastructure (Phase 1).

Everything needed to resolve a concrete, tradable option instrument —
fetching an underlying's option chain from a broker, selecting an expiry
and a strike, and building/parsing the resulting trading symbol — lives
in this package. Phase 1 is infrastructure only: nothing here places an
order, evaluates risk, or is wired into any API route. See
`docs/OPTIONS_PHASE1.md` for the full architecture writeup and the
roadmap for what later phases add on top of this.
"""
