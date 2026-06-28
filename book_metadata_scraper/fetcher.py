import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# Session type constants used by source plugins
SESSION_HTTP = "http"
SESSION_STEALTHY = "stealthy"


class SessionManager:
    """Manages two long-lived Scrapling sessions (HTTP and stealthy) behind a
    shared semaphore that caps total concurrent fetches at ``concurrency_limit``.

    Source plugins call ``fetch_http()`` or ``fetch_stealthy()`` depending on
    their needs.  Both methods honour the same semaphore, so the cap applies
    globally regardless of which session type is used.

    Args:
        concurrency_limit -- Max concurrent fetches across both sessions.
        http_rate_limit   -- Minimum seconds between HTTP requests.  ``None``
                             (default) disables rate limiting.  Set to ``1.0``
                             for sources that require polite crawling.
    """

    def __init__(self, concurrency_limit: int = 5, http_rate_limit: float | None = None):
        self._sem = asyncio.Semaphore(concurrency_limit)
        self._http_rate_limit = http_rate_limit
        self._http_last_fetch: float = 0.0
        self._http_lock = asyncio.Lock()  # Serialises rate-limit check + sleep
        self._http_ctx = None   # FetcherSession context manager
        self._http = None       # Inner session object (from __aenter__)
        self._stealthy_ctx = None  # AsyncStealthySession context manager
        self._stealthy = None      # Inner session object (from __aenter__)

    async def start(self) -> None:
        """Open both sessions.  Call once before any fetch operations."""
        from scrapling.fetchers import FetcherSession, AsyncStealthySession

        logger.info("Starting session manager")
        self._http_ctx = FetcherSession()
        self._http = await self._http_ctx.__aenter__()
        self._stealthy_ctx = AsyncStealthySession(headless=True, network_idle=True)
        self._stealthy = await self._stealthy_ctx.__aenter__()
        logger.info("Sessions ready")

    async def stop(self) -> None:
        """Close both sessions.  Call once after all fetch operations complete."""
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
        """
        if not self._stealthy:
            raise RuntimeError("SessionManager.start() has not been called")
        async with self._sem:
            return await self._stealthy.fetch(url, **kwargs)

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *_):
        await self.stop()
