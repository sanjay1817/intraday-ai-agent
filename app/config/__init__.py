"""Application configuration.

Single source of truth for environment-driven settings, built on
`pydantic-settings`. Every other package reads configuration through
`app.config.settings.get_settings()` rather than `os.environ` directly,
so behavior stays testable and overridable.
"""
