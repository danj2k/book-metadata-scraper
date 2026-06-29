import tomllib
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ScraperConfig:
    """Runtime configuration for the scraper."""

    db_path: str = "book_metadata.db"
    concurrency_limit: int = 5
    http_rate_limit: float | None = None  # Minimum seconds between HTTP fetches (None = no limit)
    stealthy_page_limit: int = 20  # Chromium context recycling frequency (0 = disabled)
    log_file: str = "book-metadata-scraper.log"
    log_level: str = "INFO"  # DEBUG | INFO | WARNING | ERROR
    enabled_scoped_sources: list[str] = field(default_factory=list)
    enabled_universal_sources: list[str] = field(default_factory=list)
    # Per-source config blobs passed through to source constructors
    source_config: dict[str, dict] = field(default_factory=dict)


def load_config(path: str | None = None) -> ScraperConfig:
    """Read a TOML config file and return a ScraperConfig.

    Falls back to defaults for any missing key.  If *path* is None or the
    file does not exist, a fully-default config is returned.
    """
    config_path = Path(path) if path else Path("scraper.toml")

    if not config_path.exists():
        logger.info("No config file at %s — using defaults", config_path)
        return ScraperConfig()

    logger.info("Loading config from %s", config_path)
    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    kwargs: dict = {}

    # Simple scalar fields
    for key in ("db_path", "concurrency_limit", "http_rate_limit", "stealthy_page_limit", "log_file", "log_level"):
        if key in raw:
            kwargs[key] = raw[key]

    # List fields
    for key in ("enabled_scoped_sources", "enabled_universal_sources"):
        if key in raw:
            kwargs[key] = raw[key]

    # Per-source config blobs
    if "source_config" in raw:
        kwargs["source_config"] = raw["source_config"]

    return ScraperConfig(**kwargs)
