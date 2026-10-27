"""Small shared boundary for public-web requests; no provider or browser access."""
from __future__ import annotations

import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urlsplit, urlunsplit

HTTP_ENGINES = frozenset({'duckduckgo', 'bing', 'anysearch', 'searx', 'googlenews'})
SECRET = re.compile(r'(?i)(?:(?:authorization|password|passwd|api[_ -]?key|access[_ -]?token)\s*[:=]|'
                    r'\bbearer\s+\S+|\bsk-[a-z0-9_-]{12,}|-----BEGIN .*PRIVATE KEY|[a-z]:\\users\\)')


class WebBlocked(ValueError):
    pass


def public_ip(value: str) -> None:
    address = ipaddress.ip_address(value.split('%', 1)[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if not address.is_global or address.is_multicast:
        raise WebBlocked('private_address')


def public_url(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(ord(c) < 33 for c in value):
        raise WebBlocked('invalid_url')
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').encode('idna').decode('ascii').lower().rstrip('.')
        port = parsed.port
    except (ValueError, UnicodeError):
        raise WebBlocked('invalid_url') from None
    if parsed.scheme not in {'https', 'http'} or not host or parsed.username is not None or parsed.password is not None:
        raise WebBlocked('invalid_url')
    if port not in {None, 80, 443} or '%' in host or '\\' in value:
        raise WebBlocked('invalid_url')
    if host in {'localhost', 'metadata', 'metadata.google.internal', 'instance-data'} or host.endswith(('.localhost', '.local', '.internal', '.home', '.lan')):
        raise WebBlocked('private_host')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if '.' not in host:
            raise WebBlocked('private_host') from None
    else:
        public_ip(host)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or '/', parsed.query, ''))


def resolve_public(value: str) -> tuple[str, int, list[str]]:
    parsed = urlsplit(public_url(value))
    host = parsed.hostname or ''
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    addresses = sorted({str(item[4][0])
                        for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    if not addresses:
        raise WebBlocked('dns_unavailable')
    for address in addresses:
        public_ip(address)
    return host, port, addresses


def query_text(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 600:
        raise WebBlocked('invalid_query')
    if SECRET.search(value) or any(ord(c) < 32 for c in value):
        raise WebBlocked('sensitive_query')
    return value.strip()


def deny_process(event: str, args: Any) -> None:
    if event in {'subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.startfile', 'os.startfile/2'} or event.startswith(('os.spawn', 'os.exec')):
        raise PermissionError('LUNMI_WEB_PROCESS_BLOCKED')
