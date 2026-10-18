"""Composio consumer OAuth; only DPAPI ciphertext crosses the filesystem."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthMetadata, OAuthToken
from pydantic import AnyHttpUrl, AnyUrl

from app.settings import _protect

ENDPOINT = 'https://connect.composio.dev/mcp'
HOSTS = frozenset({'connect.composio.dev', 'login.composio.dev', 'backend.composio.dev'})


class IntegrationError(Exception):
    """Public code only: never propagate remote errors or credential values."""


def valid_url(value: str) -> bool:
    try:
        url = urlsplit(value)
        return (len(value) <= 16384 and url.scheme == 'https' and url.hostname in HOSTS
                and url.port in (None, 443) and not url.username and not url.password
                and not any(ord(c) < 32 for c in value))
    except ValueError:
        return False


class CredentialStore:
    def __init__(self, path: Path, *, reset_invalid: bool = False) -> None:
        self.path = path
        self.disabled = False
        self.data: dict[str, Any] = {}
        if path.exists():
            try:
                if path.stat().st_size > 131072:
                    raise ValueError('size')
                data = json.loads(_protect(path.read_bytes(), decrypt=True))
                if isinstance(data, dict):
                    self.data = data
            except Exception:
                if not reset_invalid:
                    raise IntegrationError('reauth_required') from None

    def save(self) -> None:
        if self.disabled:
            raise IntegrationError('cancelled')
        encrypted = _protect(json.dumps(self.data).encode('utf-8'))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        try:
            temporary.write_bytes(encrypted)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def clear(self) -> None:
        self.disabled = True
        self.data.clear()
        self.path.unlink(missing_ok=True)

    async def get_tokens(self) -> OAuthToken | None:
        raw = self.data.get('tokens')
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.data['tokens'] = tokens.model_dump(mode='json')
        self.data['expires_at'] = time.time() + tokens.expires_in if tokens.expires_in is not None else None
        self.save()

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self.data.get('client')
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.data['client'] = client_info.model_dump(mode='json')
        self.save()


class PersistentOAuth(OAuthClientProvider):
    """The pinned SDK loads tokens but does not restore their expiry across restarts."""
    async def _initialize(self) -> None:
        await super()._initialize()
        store = self.context.storage
        if isinstance(store, CredentialStore):
            expiry = store.data.get('expires_at')
            if isinstance(expiry, (int, float)):
                self.context.token_expiry_time = float(expiry)
        # Public metadata checked for this fixed consumer endpoint; no arbitrary issuers.
        self.context.oauth_metadata = OAuthMetadata(
            issuer=AnyHttpUrl('https://connect.composio.dev'),
            authorization_endpoint=AnyHttpUrl('https://connect.composio.dev/oauth/authorize'),
            token_endpoint=AnyHttpUrl('https://login.composio.dev/oauth2/token'),
            registration_endpoint=AnyHttpUrl('https://login.composio.dev/oauth2/register'),
            response_types_supported=['code'], code_challenge_methods_supported=['S256'])


class ComposioTransport:
    """An MCP exchange shares its OAuth identity; no automatic tool retry."""
    def __init__(self, store: CredentialStore, browser: Callable[[str], Awaitable[None]]) -> None:
        self.store, self.browser = store, browser
        self.auth: OAuthClientProvider | None = None

    async def _request_guard(self, request: httpx.Request) -> None:
        if not valid_url(str(request.url)):
            raise IntegrationError('unavailable')

    def _client(self, headers: dict[str, str] | None = None, timeout: httpx.Timeout | None = None,
                auth: httpx.Auth | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=headers, timeout=timeout or httpx.Timeout(30), auth=auth,
                                 follow_redirects=False, event_hooks={'request': [self._request_guard]})

    async def connect(self, discover: Callable[[ClientSession], Awaitable[Any]]) -> Any:
        if await self.store.get_client_info() and await self.store.get_tokens():
            try:
                return await self.exchange(discover)
            except IntegrationError as error:
                if str(error) != 'reauth_required':
                    raise
            self.auth = None
        callback: asyncio.Future[tuple[str, str | None]] = asyncio.get_running_loop().create_future()

        async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                line = await asyncio.wait_for(reader.readline(), 3)
                if len(line) > 8192:
                    raise ValueError('size')
                method, target, _ = line.decode('ascii').split(' ', 2)
                parsed = urlsplit(target)
                query = parse_qs(parsed.query, strict_parsing=True)
                if (method != 'GET' or parsed.path != '/callback' or parsed.netloc
                        or set(query) != {'code', 'state'} or any(len(v) != 1 for v in query.values())
                        or not query['code'][0] or not query['state'][0]):
                    raise ValueError('callback')
                if not callback.done():
                    callback.set_result((query['code'][0], query['state'][0]))
                body = b'Pode fechar esta janela e voltar.'
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode()
                             + b'\r\nConnection: close\r\n\r\n' + body)
            except Exception:
                writer.write(b'HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            finally:
                await writer.drain()
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(receive, '127.0.0.1', 0, limit=8192)
        port = server.sockets[0].getsockname()[1]
        redirect = f'http://127.0.0.1:{port}/callback'

        async def open_browser(url: str) -> None:
            if not valid_url(url):
                raise IntegrationError('unavailable')
            await self.browser(url)

        async def receive_callback() -> tuple[str, str | None]:
            return await callback

        # Dynamic ports require a newly registered redirect only when authorization is needed.
        self.store.data.pop('client', None)
        self.store.data.pop('tokens', None)
        self.store.data.pop('expires_at', None)
        self.store.save()
        self.auth = PersistentOAuth(ENDPOINT, OAuthClientMetadata(
            client_name='Lunmi', redirect_uris=[AnyUrl(redirect)], token_endpoint_auth_method='none',
            grant_types=['authorization_code', 'refresh_token'], response_types=['code']),
            self.store, open_browser, receive_callback, timeout=300)
        try:
            async with asyncio.timeout(300), server:
                return await self.exchange(discover)
        finally:
            server.close()
            await server.wait_closed()
            if not callback.done():
                callback.cancel()

    async def exchange(self, action: Callable[[ClientSession], Awaitable[Any]]) -> Any:
        if self.auth is None:
            async def forbidden(_: str) -> None:
                raise IntegrationError('reauth_required')
            client = await self.store.get_client_info()
            redirects = client.redirect_uris if client else [AnyUrl('http://127.0.0.1/callback')]
            self.auth = PersistentOAuth(ENDPOINT, OAuthClientMetadata(
                client_name='Lunmi', redirect_uris=redirects, token_endpoint_auth_method='none'),
                self.store, redirect_handler=forbidden)
        async with streamablehttp_client(ENDPOINT, auth=self.auth, httpx_client_factory=self._client,
                                        timeout=30, sse_read_timeout=30) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await action(session)
