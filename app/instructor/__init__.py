"""The Intraday AI Instructor.

Synthesizes `app.strategy.engine.StrategyEngine`'s merged
`ConfluenceResult` into one human-readable BUY/SELL/HOLD recommendation
for manual trading — never an autonomous trading decision, and never a
broker call.

This package exists alongside `app.ai` (the AI Decision Engine), not in
place of it: `app.strategy`'s own docstring anticipates `app.ai` as its
eventual consumer for autonomous decision-making via an LLM. This
package is a separate, deterministic (no LLM call) consumer scoped
specifically to producing recommendations a human reads and decides on
manually — a genuinely different concern from autonomous execution, not
a replacement for `app.ai`'s eventual role.
"""
