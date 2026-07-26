"""A generic, in-process registry of named platform services.

`app.platform.lifecycle`, `app.platform.health_manager`, and
`app.platform.dependency_graph` all operate over whatever is registered
here — a database session factory, a Redis client, a broker adapter, the
Indicator Engine, or anything else — without needing to know each
other's concrete types. This is a lookup table, not a full dependency-
injection container: this project wires its own dependencies explicitly
via constructor injection, per its existing conventions: no globals.
Registration is expected to happen sequentially during application
startup, so this class is not internally locked against concurrent
writes from multiple tasks — only concurrent reads are safe by default.
"""

from typing import TypeVar

T = TypeVar("T")


class ServiceRegistry:
    """A process-wide registry of named service instances.

    Callers construct their own instance (typically one per application
    process, created and owned by whatever assembles the app) rather
    than reaching for a module-level singleton.
    """

    def __init__(self) -> None:
        self._services: dict[str, object] = {}

    def register(self, name: str, instance: object) -> None:
        """Register `instance` under `name`.

        Raises:
            ValueError: `name` is already registered — a duplicate
                registration is almost always a startup-wiring bug, not
                an intentional replacement (use `unregister` first if a
                replacement is genuinely intended).
        """

        if name in self._services:
            raise ValueError(f"a service named {name!r} is already registered")
        self._services[name] = instance

    def get(self, name: str, expected_type: type[T]) -> T:
        """Look up the service registered under `name`.

        Args:
            name: The registration key.
            expected_type: The type the caller expects back; checked at
                lookup time so a wiring mistake fails loudly here,
                rather than surfacing later as an obscure `AttributeError`
                deep inside whatever borrowed the wrong service.

        Raises:
            KeyError: no service is registered under `name`.
            TypeError: the registered service isn't an `expected_type`.
        """

        try:
            instance = self._services[name]
        except KeyError as exc:
            raise KeyError(f"no service named {name!r} is registered") from exc

        if not isinstance(instance, expected_type):
            raise TypeError(
                f"service {name!r} is a {type(instance).__name__}, not a {expected_type.__name__}"
            )
        return instance

    def unregister(self, name: str) -> None:
        """Remove the service registered under `name`, if any (a no-op
        if it isn't registered).
        """

        self._services.pop(name, None)

    def names(self) -> list[str]:
        """Every currently-registered service name, sorted for stable output."""

        return sorted(self._services)

    def __contains__(self, name: object) -> bool:
        return name in self._services

    def __len__(self) -> int:
        return len(self._services)
