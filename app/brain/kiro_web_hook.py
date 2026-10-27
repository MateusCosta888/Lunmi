"""PreToolUse guard for Kiro's native web tools (stdlib only).

Kiro runs this script before ``web_search``/``web_fetch``. The script validates the
tool input, writes one sanitized receipt line (tool name + denied flag only), and exits
2 on any doubt. It never receives or records the prompt, page text, query or URL beyond
the in-memory validation, and it never performs a fetch itself.

The receipt is the canonical proof of the real tool name: V3 tool events carry only
``toolOrigin``, and ``title`` is presentation. The adapter reads the receipt file named
by ``LUNMI_KIRO_RECEIPT``; if the environment variable is absent or the write fails, the
call is denied (fail closed).
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import sys
from typing import Any, Callable
from urllib.parse import urlsplit

MAX_QUERY_CHARS = 600
MAX_URL_CHARS = 2048
ALLOWED_TOOLS = frozenset({"web_search", "web_fetch"})
# V3's ACP search arrives with this exact literal name. It is an alias, not a
# second tool: canonicalize it and keep the permission surface unchanged.
REMOTE_SEARCH_ALIAS = "remote_web_search"
# Mirrors app/integrations/web_policy.py SECRET. The hook is stdlib-only and runs
# isolated, so it cannot import integrations; this closed pattern keeps the same
# sensitive-query boundary without logging or rewriting the query.
SENSITIVE_QUERY = re.compile(
    r"(?i)(?:(?:authorization|password|passwd|api[_ -]?key|access[_ -]?token)\s*[:=]|"
    r"\bbearer\s+\S+|\bsk-[a-z0-9_-]{12,}|-----BEGIN .*PRIVATE KEY|[a-z]:\\users\\)"
)
LOCAL_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa",
                  ".localdomain", ".lan")
Resolver = Callable[..., list[Any]]


def _public_ip(value: str) -> bool:
    """True only for a globally routable address; IPv4-mapped IPv6 is unwrapped."""
    try:
        address = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return _public_ip(str(address.ipv4_mapped))
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def url_syntax_problem(url: Any) -> str:
    """Return a short reason for a non-public/invalid URL, or ``""`` when acceptable.

    This is deliberately syntax-only: it never resolves DNS and never fetches. The
    runtime reuses it to filter search-result URLs without opening the network. The hook
    adds the DNS check for ``web_fetch`` before Kiro executes the call.
    """
    if not isinstance(url, str):
        return "url"
    text = url.strip()
    if not 1 <= len(text) <= MAX_URL_CHARS:
        return "url"
    if any(ord(character) < 32 for character in text):
        return "url"
    try:
        parts = urlsplit(text)
    except ValueError:
        return "url"
    if parts.scheme.lower() not in {"http", "https"}:
        return "scheme"
    if parts.username is not None or parts.password is not None:
        return "credentials"
    try:
        port = parts.port
    except ValueError:
        return "port"
    if port not in {None, 80, 443}:
        return "port"
    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        return "host"
    if host == "localhost" or any(host.endswith(suffix) for suffix in LOCAL_SUFFIXES):
        return "host"
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return "" if _public_ip(host) else "host"
    # A single-label host is not a public DNS name. IP literals were handled above.
    if "." not in host:
        return "host"
    try:
        host.encode("idna")
    except UnicodeError:
        return "host"
    return ""


def fetch_problem(url: Any, resolver: Resolver = socket.getaddrinfo) -> str:
    """Validate a fetch URL syntactically and require every DNS address to be public."""
    problem = url_syntax_problem(url)
    if problem:
        return problem
    parts = urlsplit(str(url).strip())
    host = (parts.hostname or "").strip().rstrip(".").lower()
    try:
        dns_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return "host"
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    try:
        infos = resolver(dns_host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError, ValueError):
        return "dns"
    if not infos:
        return "dns"
    for info in infos:
        try:
            sockaddr = info[4]
            address = sockaddr[0]
        except (IndexError, TypeError):
            return "dns"
        if not _public_ip(str(address)):
            return "address"
    return ""


def canonical_tool_name(tool_name: Any) -> str | None:
    """Return the canonical web tool name, or ``None`` for anything unknown.

    Only the one literal observed in Kiro V3 is normalized. Matching is exact and
    case-sensitive; no other remote name or title is inferred.
    """
    if tool_name == REMOTE_SEARCH_ALIAS:
        return "web_search"
    if isinstance(tool_name, str) and tool_name in ALLOWED_TOOLS:
        return tool_name
    return None


def _query_problem(query: Any) -> str:
    if not isinstance(query, str):
        return "query"
    if not 1 <= len(query.strip()) <= MAX_QUERY_CHARS:
        return "query"
    if SENSITIVE_QUERY.search(query) or any(ord(character) < 32 for character in query):
        return "query"
    return ""


def validate_tool(tool_name: Any, tool_input: Any, *,
                  resolver: Resolver = socket.getaddrinfo) -> str:
    """Return ``""`` when the call is acceptable, else a short denial reason."""
    name = canonical_tool_name(tool_name)
    if name is None:
        return "tool"
    tool_name = name
    if not isinstance(tool_input, dict):
        return "input"
    if tool_name == "web_search":
        return _query_problem(tool_input.get("query"))
    return fetch_problem(tool_input.get("url"), resolver=resolver)


def _clean_name(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    name = "".join(character for character in value
                   if character.isprintable() and character not in "\r\n")
    return name[:40] or "unknown"


def write_receipt(tool_name: Any, denied: bool,
                  path: str | os.PathLike[str] | None = None) -> bool:
    """Append one sanitized receipt line; no query/URL/content is ever written."""
    target = path if path is not None else os.environ.get("LUNMI_KIRO_RECEIPT", "")
    if not target:
        return False
    name = canonical_tool_name(tool_name) or tool_name
    record = json.dumps({"tool_name": _clean_name(name), "denied": bool(denied)},
                        ensure_ascii=True, separators=(",", ":")) + "\n"
    try:
        descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, record.encode("utf-8"))
        finally:
            os.close(descriptor)
    except OSError:
        return False
    return True


def main() -> int:
    # Keep a slow resolver from hanging the hook; Kiro's own timeout remains the final
    # bound, and any timeout/error is a denial (exit 2), never an allow.
    socket.setdefaulttimeout(2.0)
    try:
        payload = json.load(sys.stdin)
    except (ValueError, TypeError, OSError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    tool_name = payload.get("tool_name")
    problem = validate_tool(tool_name, payload.get("tool_input"))
    allowed = not problem
    # A receipt is required for the adapter to prove the real tool name. If it cannot
    # be written, the call is denied instead of executed without provenance.
    if not write_receipt(tool_name, not allowed):
        allowed = False
    if not allowed:
        print("LUNMI_POLICY_DENIED", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
