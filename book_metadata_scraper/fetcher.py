import asyncio
import logging

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
    """

    def __init__(self, concurrency_limit: int = 5):
        self._sem = asyncio.Semaphore(concurrency_limit)
        self._http = None  # type: FetcherSession | None
        self._stealthy = None  # type: AsyncStealthySession | None

    async def start(self) -> None:
        """Open both sessions.  Call once before any fetch operations."""
        from scrapling.fetchers import FetcherSession, AsyncStealthySession

        logger.info("Starting session manager (concurrency_limit=%d)", self._sem._value if hasattr(self._sem, '_value') else "?")
        self._http = FetcherSession()
        await self._http.start()
        self._stealthy = AsyncStealthySession(headless=True, network_idle=True)
        await self._stealthy.start()
        logger.info("Both sessions started")

    async def stop(self) -> None:
        """Close both sessions.  Call once after all fetch operations complete."""
        if self._http:
            await self._http.stop()
            self._http = None
        if self._stealthy:
            await self._stealthy.stop()
            self._stealthy = None
        logger.info("Sessions stopped")

    async def fetch_http(self, url: str, **kwargs):
        """Fetch *url* via the plain HTTP session (FetcherSession).

        Use for APIs and sites that do not require stealth.
        ``**kwargs`` are forwarded to the session's fetch method.
        Returns a Scrapling Response object.
        """
        if not self._http:
            raise RuntimeError("SessionManager.start() has not been called")
        async with self._sem:
            return await self._http.fetch(url, **kwargs)

    async def fetch_stealthy(self, url: str, **kwargs):
        """Fetch *url* via the stealthy browser session (AsyncStealthySession).

        Use for sites with anti-bot protection or JavaScript-rendered content.
        ``**kwargs`` are forwarded to the session's fetch method.
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
