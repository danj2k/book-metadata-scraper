"""Source registration and discovery.

Source plugins self-register using decorators.  The ``sources/__init__.py``
auto-imports all modules under ``sources/scoped/`` and ``sources/universal/``
so that decorators run at import time.
"""

from book_metadata_scraper.sources.base import BaseScopedSource, BaseUniversalSource

_SCOPED_REGISTRY: dict[str, type[BaseScopedSource]] = {}
_UNIVERSAL_REGISTRY: dict[str, type[BaseUniversalSource]] = {}


def scoped_source(cls: type[BaseScopedSource]) -> type[BaseScopedSource]:
    """Class decorator.  Apply to every BaseScopedSource subclass."""
    _SCOPED_REGISTRY[cls.name] = cls
    return cls


def universal_source(cls: type[BaseUniversalSource]) -> type[BaseUniversalSource]:
    """Class decorator.  Apply to every BaseUniversalSource subclass."""
    _UNIVERSAL_REGISTRY[cls.name] = cls
    return cls


def get_scoped_source(name: str) -> type[BaseScopedSource]:
    """Return a registered scoped source class by name."""
    if name not in _SCOPED_REGISTRY:
        raise KeyError(f"No scoped source named '{name}' is registered")
    return _SCOPED_REGISTRY[name]


def get_universal_source(name: str) -> type[BaseUniversalSource]:
    """Return a registered universal source class by name."""
    if name not in _UNIVERSAL_REGISTRY:
        raise KeyError(f"No universal source named '{name}' is registered")
    return _UNIVERSAL_REGISTRY[name]


def list_scoped_sources() -> list[str]:
    """Return all registered scoped source names."""
    return list(_SCOPED_REGISTRY.keys())


def list_universal_sources() -> list[str]:
    """Return all registered universal source names."""
    return list(_UNIVERSAL_REGISTRY.keys())
