"""Pinned Free Search entry point; policy adapter, never edits upstream files."""
from __future__ import annotations

import asyncio
import importlib.metadata
import sys
from pathlib import Path
from typing import Any

# Isolated Python (-I): add only the trusted application root, never the CWD.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.integrations.web_policy import WebBlocked, deny_process, public_url, resolve_public


class PinnedFetchSession:
    """Bind each fetch/redirect to validated DNS; keep hostname/SNI/certificate checks."""

    def __init__(self, **kwargs: Any) -> None:
        self.options = kwargs
        self.clients: list[Any] = []

    async def __aenter__(self) -> PinnedFetchSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await asyncio.gather(*(client.close() for client in self.clients), return_exceptions=True)

    async def get(self, url: str, **kwargs: Any) -> Any:
        from curl_cffi import CurlOpt
        from curl_cffi.requests import AsyncSession

        url = public_url(url)
        host, port, addresses = await asyncio.to_thread(resolve_public, url)
        entries = ','.join(f'[{ip}]' if ':' in ip else ip for ip in addresses)
        options = {**self.options, 'proxy': '', 'trust_env': False, 'allow_redirects': False,
                   'curl_options': {CurlOpt.RESOLVE: [f'{host}:{port}:{entries}'.encode()]}}
        client = AsyncSession(**options)
        self.clients.append(client)
        return await client.get(url, **kwargs)


async def unsupported_asset(*args: Any, **kwargs: Any) -> Any:
    raise WebBlocked('unsupported_resource')


def main() -> None:
    sys.addaudithook(deny_process)
    if importlib.metadata.version('free-search-mcp') != '0.16.0':
        raise RuntimeError('unsupported_web_runtime')
    from search_mcp import fetcher
    from search_mcp.config import settings

    assert settings.fetch_strategy == 'http' and not settings.prewarm
    assert settings.default_engines == ['duckduckgo', 'bing', 'anysearch']
    assert settings.reserve_engines == ['searx'] and settings.rescue_engines == ['searx']
    assert settings.fresh_engines == ['googlenews'] and settings.locale_engines == {}
    assert not settings.auto_route_enabled and settings.category_engine_limit == 0
    assert settings.agent_backend == 'off' and not settings.download_enabled
    assert settings.transport == 'stdio' and not settings.allow_private_hosts
    assert settings.ssrf_resolve_addresses == 'always'
    assert settings.enabled_tools() == {'search', 'fetch', 'research', 'fetch_batch'}
    # These are the three external-fetch seams in the pinned upstream, not a new server.
    fetcher.AsyncSession = PinnedFetchSession
    fetcher._fetch_as_document = unsupported_asset
    fetcher._fetch_as_asset = unsupported_asset
    from search_mcp.server import run
    run()


if __name__ == '__main__':
    main()
