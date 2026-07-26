"""API layer.

Contains versioned HTTP routers only. Routers must remain thin: they parse
and validate input (via Pydantic schemas), delegate to the service layer,
and translate results/exceptions into HTTP responses. No business logic,
persistence, or third-party client calls belong here.
"""
