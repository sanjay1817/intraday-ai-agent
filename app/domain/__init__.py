"""Domain layer (DDD core).

Framework-agnostic entities, value objects, enums, and domain exceptions
that encode the trading business rules. This layer must not import from
FastAPI, SQLAlchemy, or any other infrastructure package — it is the
innermost layer that everything else depends on, never the reverse.
"""
