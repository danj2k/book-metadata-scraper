import asyncio
import logging
import time

from scrapling.fetchers import FetcherSession, AsyncStealthySession

logger = logging.getLogger(__name__)

# Session type constants used by source plugins
SESSION_HTTP = "http"
SESSION_STEALTHY = "stealthy"

# Chromium flags that reduce baseline memory usage.  These are safe defaults
# that don't affect anti-bot detection — they strip out GPU rendering, browser
# extensions, and background networking that a headless scraper never uses.
_MEMORY_EFFICIENT_FLAGS = [
    "--disable-dev-shm-usage",      # Use /tmp instead of /dev/shm
    "--disable-extensions",          # No browser extensions
    "--disable-background-networking",  # No background network requests
    "--disable-default-apps",        # Don't load default apps
    "--no-first-run",                # Skip first-run wizard
    "--disable-translate",           # No translate popups
]


class SessionManager:
    """Manages two long-lived Scrapling sessions (HTTP and stealthy) behind a
    shared semaphore that caps total concurrent fetches at ``concurrency_limit``.

    Source plugins call ``fetch_http()`` or ``fetch_stealthy()`` depending on
    their needs.  Both methods honour the same semaphore, so the cap applies
    globally regardless of which session type is used.

    The stealthy session is periodically destroyed and recreated (along with
    the underlying Chromium process) to reclaim accumulated browser memory.
    This is controlled by ``stealthy_page_limit`` — after that many stealthy
    fetches the entire session is torn down and a fresh one started.  The
    restart takes a few seconds but keeps the Chromium heap bounded.

    Args:
        concurrency_limit    -- Max concurrent fetches across both sessions.
        http_rate_limit      -- Minimum seconds between HTTP requests.  ``None``
                                (default) disables rate limiting.  Set to ``1.0``
                                for sources that require polite crawling.
        stealthy_page_limit  -- Number of stealthy fetches between session
                                restarts.  Lower values keep memory usage down
                                on resource-constrained VPS instances.  Set to 0
                                to disable recycling (not recommended).
    """

    def __init__(
        self,
        concurrency_limit: int = 5,
        http_rate_limit: float | None = None,
        stealthy_page_limit: int = 20,
    ):
        self._sem = asyncio.Semaphore(concurrency_limit)
        self._http_rate_limit = http_rate_limit
        self._http_last_fetch: float = 0.0
        self._http_lock = asyncio.Lock()  # Serialises rate-limit check + sleep
        self._http_ctx = None   # FetcherSession context manager
        self._http = None       # Inner session object (from __aenter__)

        # Stealthy session — created lazily, recycled periodically.
        self._stealthy_ctx = None  # AsyncStealthySession context manager
        self._stealthy = None      # Inner session object (from __aenter__)
        self._stealthy_page_limit = stealthy_page_limit
        self._stealthy_page_count = 0
        self._stealthy_lock = asyncio.Lock()  # Serialises session restart

    async def start(self) -> None:
        """Open HTTP session.  Stealthy session is started lazily on first use
        (or eagerly if ``stealthy_page_limit`` is set) to avoid holding a
        Chromium process idle while only HTTP sources are running."""
        self._http_ctx = FetcherSession()
        self._http = await self._http_ctx.__aenter__()
        logger.info("HTTP session ready")

    async def _ensure_stealthy(self) -> None:
        """Ensure the stealthy session is running, creating it if needed."""
        if self._stealthy is not None:
            return
        logger.info("Starting stealthy session (Chromium)")
        self._stealthy_ctx = AsyncStealthySession(
            headless=True,
            network_idle=True,
            extra_flags=_MEMORY_EFFICIENT_FLAGS,
        )
        self._stealthy = await self._stealthy_ctx.__aenter__()
        self._stealthy_page_count = 0
        logger.info("Stealthy session ready")

    async def _restart_stealthy(self) -> None:
        """Destroy and recreate the stealthy session to reclaim Chromium memory.

        This tears down the entire Chromium process and starts a fresh one.
        It's called after ``stealthy_page_limit`` stealthy fetches.  The caller
        must already hold ``_stealthy_lock``.

        On a low-memory VPS, this is the primary defence against OOM: rather
        than letting the Chromium heap grow unbounded, we pay a one-time cost
        of ~2-3s to restart with a clean process.
        """
        logger.info(
            "Restarting stealthy session (page %d/%d) to reclaim memory",
            self._stealthy_page_count,
            self._stealthy_page_limit,
        )
        # Tear down the existing session — this kills the Chromium process.
        try:
            await self._stealthy_ctx.__aexit__(None, None, None)
        except Exception:
            logger.warning("Error closing stealthy session", exc_info=True)
        self._stealthy_ctx = None
        self._stealthy = None

        # Start a fresh session.
        self._stealthy_ctx = AsyncStealthySession(
            headless=True,
            network_idle=True,
            extra_flags=_MEMORY_EFFICIENT_FLAGS,
        )
        self._stealthy = await self._stealthy_ctx.__aenter__()
        self._stealthy_page_count = 0
        logger.info("Stealthy session restarted")

    async def stop(self) -> None:
        """Close all sessions.  Call once after all fetch operations complete."""
        if self._http_ctx:
            await self._http_ctx.__aexit__(None, None, None)
            self._http_ctx = None
            self._http = None
        if self._stealthy_ctx:
            await self._stealthy_ctx.__aexit__(None, None, None)
            self._stealthy_ctx = None
            self._stealthy = None
        logger.info("Sessions stopped")

    async def _http_rate_wait(self, min_interval: float | None = None) -> None:
        """Enforce minimum interval between HTTP fetches if configured.

        If *min_interval* is provided it overrides the global
        ``http_rate_limit`` for this single call.
        """
        interval = min_interval if min_interval is not None else self._http_rate_limit
        if interval is None:
            return
        async with self._http_lock:
            now = time.monotonic()
            wait = interval - (now - self._http_last_fetch)
            if wait > 0:
                logger.debug("Rate limit: sleeping %.2fs", wait)
                await asyncio.sleep(wait)
            self._http_last_fetch = time.monotonic()

    async def fetch_http(self, url: str, *, min_interval: float | None = None, **kwargs):
        """Fetch *url* via the plain HTTP session (FetcherSession).

        Use for APIs and sites that do not require stealth.
        ``min_interval`` overrides the global ``http_rate_limit`` for this
        single call when provided.
        ``**kwargs`` are forwarded to the session's ``.get()`` method.
        Returns a Scrapling Response object.
        """
        if not self._http:
            raise RuntimeError("SessionManager.start() has not been called")
        await self._http_rate_wait(min_interval)
        async with self._sem:
            return await self._http.get(url, **kwargs)

    async def fetch_stealthy(self, url: str, **kwargs):
        """Fetch *url* via the stealthy browser session (AsyncStealthySession).

        Use for sites with anti-bot protection or JavaScript-rendered content.
        ``**kwargs`` are forwarded to the session's ``.fetch()`` method.
        Returns a Scrapling Response object.

        The stealthy session is automatically restarted after
        ``stealthy_page_limit`` fetches to reclaim Chromium memory.  This
        adds ~2-3s overhead per restart but prevents OOM on low-memory VPS
        instances.
        """
        async with self._stealthy_lock:
            await self._ensure_stealthy()

        async with self._sem:
            result = await self._stealthy.fetch(url, **kwargs)

        # Increment count and check if we need to restart.
        # The increment + check happens outside the fetch sem but under
        # the stealthy lock, so no other task can interleave a restart.
        if self._stealthy_page_limit > 0:
            async with self._stealthy_lock:
                self._stealthy_page_count += 1
                if self._stealthy_page_count >= self._stealthy_page_limit:
                    await self._restart_stealthy()

        return result

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *_):
        await self.stop()
