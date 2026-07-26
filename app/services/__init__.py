"""Service layer.

Orchestrates use cases: coordinates one or more repositories and domain
entities to fulfill an application operation (e.g. "authenticate user").
Services depend on repository abstractions (protocols), never on ORM
sessions or HTTP concerns directly, so they stay unit-testable.
"""
