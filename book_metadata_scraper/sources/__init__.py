"""Auto-import all source modules so that @scoped_source / @universal_source
decorators run at import time.  Adding a new plugin file is the only step
needed to make it available.
"""
import importlib
import pkgutil

import book_metadata_scraper.sources.scoped as _scoped
import book_metadata_scraper.sources.universal as _universal

for _pkg in (_scoped, _universal):
    for _info in pkgutil.iter_modules(_pkg.__path__):
        importlib.import_module(f"{_pkg.__name__}.{_info.name}")
