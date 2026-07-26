"""Repository layer.

Encapsulates all persistence access behind narrow, typed interfaces. Only
this layer is allowed to construct SQLAlchemy queries; services and
domain code depend on repository abstractions, not on `AsyncSession`.
"""
