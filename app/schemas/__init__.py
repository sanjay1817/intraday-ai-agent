"""Pydantic v2 schemas: request/response DTOs for the API layer.

Schemas are distinct from ORM models and domain entities by design — they
define the wire contract and must be free to evolve independently of the
storage schema.
"""
