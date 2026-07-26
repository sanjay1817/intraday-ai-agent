"""Cross-cutting application infrastructure.

Houses concerns that every layer depends on but that are not themselves
business logic: logging setup, exception handling, middleware, security
primitives (JWT/password hashing), and the FastAPI application factory
wiring. Nothing in `app.domain` or `app.services` should import from
`app.core`; dependencies flow inward, `core` sits at the edge.
"""
