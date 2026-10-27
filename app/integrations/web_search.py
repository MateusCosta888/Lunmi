"""Cliente pinado e lifecycle da Pesquisa Web V1 (W2/W3), sem browser e sem SDK bruto.

O worker MCP corre no runtime isolado (`free-search-mcp==0.16.0`, Python 3.11), numa thread
própria: `stdio_client`/`ClientSession` são abertos e fechados pela **mesma** tarefa dona, o que
evita cancel scopes partilhados entre turnos. Uma sessão é reutilizada, uma operação de cada
vez, idle-close 120 s e o transporte termina o processo owned ao fechar/cancelar.

Nada aqui devolve payload MCP bruto: só `WebSourceDto` público e conteúdo delimitado para a
síntese. stderr é drenado para um sink que descarta (nunca queries/documentos/URLs em logs).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.integrations.web_policy import WebBlocked, public_url, query_text
from app.integrations.web_quality import (
    WebConstraints,
    attribute_evidence_problem,
    constraints,
    has_quote_value,
    publication_hint,
    rank_sources,
    requested_attribute,
    search_query,
    useful_content,
)
from app.integrations.web_state import (
    MESSAGES,
    WebEnvironment,
    WebPreferences,
    WebPreferencesError,
    WebState,
    apply_preferences,
    load_preferences,
    resolve_environment,
    save_preferences,
)

MAX_SOURCES = 5
MAX_TITLE = 300
MAX_SNIPPET = 600
# Teto por página lida: um artigo científico pode trazer os números só depois do
# antigo corte de 12K (Results/Discussion). A seleção final para o Brain mantém
# os seus orçamentos; aqui só se garante que a página não perde o fim na leitura.
MAX_PAGE_CHARS = 48000
QUICK_READS = 2          # quick explícito e atributos finitos
VERIFIED_READS = 3       # leitura automática de uma pergunta que exige fonte
DEEP_READS = 3           # modo deep explícito e ferramenta research (inalterado)
AUTO_DEEP_READS = 4      # modo automático numa pergunta de profundidade deep
# Critério fechado do quick/auto: dois excertos úteis, cada um com pelo menos 60
# caracteres, e pelo menos 200 caracteres no total. Abaixo disto a search ainda
# tenta fetch_batch até 2 páginas; acima disto devolve só os excertos (read=false).
SNIPPET_MIN_SOURCES = 2
SNIPPET_MIN_CHARS = 60
SNIPPET_MIN_TOTAL = 200

# Orçamento temporal da operação (a profundidade decide), teto de cada tentativa
# HTTP de leitura e prazo de uma página pedida explicitamente. Nenhum valor é
# renovado a meio da operação, e o cancelamento/deadline global da Task continua a
# mandar por cima.
SEARCH_TIMEOUT = 10.0
VERIFIED_TIMEOUT = 45.0
DEEP_TIMEOUT = 60.0
PER_ATTEMPT_HTTP = 15.0
OPEN_TIMEOUT = 30.0
RESEARCH_TIMEOUT = 25.0
INIT_TIMEOUT = 20.0
IDLE_SECONDS = 120.0
CANCEL_GRACE = 2.0

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+Z-]+)?$")


class WebSearchError(Exception):
    """Falha pública e tipada; nunca carrega conteúdo externo ou detalhe interno."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    @property
    def message(self) -> str:
        return MESSAGES.get(self.code, MESSAGES["failed"])


# Política HTTP W0 congelada (`output/web-search-v1/w1/effective.json`). O worker
# recebe estes valores explicitamente; nunca herda o ambiente do produto nem lê
# `.env` global. A lista de ferramentas e o SSRF fazem parte do mesmo limite.
_WORKER_POLICY: dict[str, str] = {
    "SEARCH_MCP_FETCH_STRATEGY": "http",
    "SEARCH_MCP_PREWARM": "false",
    "SEARCH_MCP_CACHE_MAX_MB": "128",
    "SEARCH_MCP_MAX_CONTENT_CHARS": "48000",
    "SEARCH_MCP_DEFAULT_ENGINES": json.dumps(["duckduckgo", "bing", "anysearch"]),
    "SEARCH_MCP_RESERVE_ENGINES": json.dumps(["searx"]),
    "SEARCH_MCP_RESCUE_ENGINES": json.dumps(["searx"]),
    "SEARCH_MCP_FRESH_ENGINES": json.dumps(["googlenews"]),
    "SEARCH_MCP_LOCALE_ENGINES": json.dumps({}),
    "SEARCH_MCP_AUTO_ROUTE_ENABLED": "false",
    "SEARCH_MCP_CATEGORY_ENGINE_LIMIT": "0",
    "SEARCH_MCP_CLAIM_ENGINE_LIMIT": "0",
    "SEARCH_MCP_SEARCH_DEADLINE_SECONDS": "5.0",
    "SEARCH_MCP_RESCUE_TIMEOUT": "4.0",
    "SEARCH_MCP_DOWNLOAD_ENABLED": "false",
    "SEARCH_MCP_TRANSPORT": "stdio",
    "SEARCH_MCP_TOOLS": "search,research,fetch,fetch_batch",
    "SEARCH_MCP_AGENT_BACKEND": "off",
    "SEARCH_MCP_ALLOW_PRIVATE_HOSTS": "false",
    "SEARCH_MCP_SSRF_RESOLVE_ADDRESSES": "always",
    "SEARCH_MCP_REGION": "pt-pt",
    "SEARCH_MCP_MAX_RESPONSE_BYTES": "2097152",
}


def _worker_environment(environment: WebEnvironment) -> tuple[dict[str, str], Path]:
    """Ambiente fechado do worker: só política W0, HOME/CWD/cache/config privados.

    Não parte de `os.environ`: um `.env` ou uma variável global do produto não entra no
    subprocesso. As pastas privadas são criadas antes do launch e ficam dentro de `home`.
    """
    home = environment.home
    work = home / "work"
    config = home / "config"
    profile = home / "profile"
    for path in (work, config, profile, home / "cache", home / "tmp",
                 home / "appdata", home / "localappdata"):
        path.mkdir(parents=True, exist_ok=True)

    env = {
        "LUNMI_WEB_HOME": str(home),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "USERPROFILE": str(profile),
        "HOME": str(profile),
        "APPDATA": str(home / "appdata"),
        "LOCALAPPDATA": str(home / "localappdata"),
        "TEMP": str(home / "tmp"),
        "TMP": str(home / "tmp"),
        "SEARCH_MCP_CACHE_DIR": str(home / "cache"),
        "SEARCH_MCP_CONFIG_DIR": str(config),
        **_WORKER_POLICY,
    }
    system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or ""
    if system_root:
        env["SYSTEMROOT"] = system_root
        env["WINDIR"] = system_root
    path_parts = [str(environment.python.parent)]
    if system_root:
        path_parts.append(str(Path(system_root) / "System32"))
    env["PATH"] = os.pathsep.join(path_parts)
    return env, work


@dataclass
class _Request:
    tool: str
    arguments: dict[str, Any]
    timeout: float
    future: concurrent.futures.Future[Any]


@dataclass
class WebResult:
    tool: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    limited: bool = False
    evidence_status: str = "suff"
    request_status: str = "met"

    @property
    def no_results(self) -> bool:
        return not self.sources


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _iso(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if _ISO_DATE.match(text) else None


def _engines(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:8]:
        name = _clean(item, 40)
        if name and name not in result:
            result.append(name)
    return result


def source_id(url: str) -> str:
    """ID opaco e estável por URL dentro da tarefa; o renderer nunca o deriva de nada."""
    return "src-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _source_from_result(result: dict[str, Any], *, retrieved_at: str,
                        document: dict[str, Any] | None = None) -> dict[str, Any] | None:
    try:
        url = public_url(str(result.get("url") or ""))
    except WebBlocked:
        return None
    doc = document or {}
    published = (_iso(doc.get("published_date")) or _iso(result.get("published_date"))
                 or publication_hint(result, retrieved_at))
    snippet = _clean(doc.get("content") or result.get("snippet") or "", MAX_SNIPPET)
    content = doc.get("content")
    read = bool(isinstance(content, str) and useful_content(content) and not doc.get("error"))
    return {
        "id": source_id(url),
        "title": _clean(doc.get("title") or result.get("title") or url, MAX_TITLE),
        "url": url,
        "snippet": snippet,
        "engines": _engines(result.get("engines")),
        "published_at": published,
        "date_source": ("document" if _iso(doc.get("published_date"))
                        else (result.get("date_source") if published else None)),
        "retrieved_at": doc.get("retrieved_at") if isinstance(doc.get("retrieved_at"), str)
        else retrieved_at,
        "read": read,
        "truncated": bool(doc.get("truncated") is True),
        "cached": bool(doc.get("method") == "cache"),
    }


def _document(result: dict[str, Any], source: dict[str, Any], *, quote: bool = False
              ) -> dict[str, Any] | None:
    content = result.get("content")
    if not isinstance(content, str) or not useful_content(content) or result.get("error"):
        return None
    if quote and not has_quote_value(content):
        return None
    return {
        "id": source["id"],
        "url": source["url"],
        "title": source["title"],
        "content": content[:MAX_PAGE_CHARS],
        "truncated": bool(result.get("truncated") is True or len(content) > MAX_PAGE_CHARS),
        "published_at": _iso(result.get("published_date")),
        "retrieved_at": result.get("retrieved_at") if isinstance(result.get("retrieved_at"), str)
        else source["retrieved_at"],
        "cached": bool(result.get("method") == "cache"),
    }


def _snippets_sufficient(sources: list[dict[str, Any]]) -> bool:
    """Evidência de excertos suficiente para responder sem leitura de páginas."""
    sizes = [len(str(source.get("snippet") or "").strip()) for source in sources]
    useful = [size for size in sizes if size >= SNIPPET_MIN_CHARS]
    return len(useful) >= SNIPPET_MIN_SOURCES and sum(useful) >= SNIPPET_MIN_TOTAL


def _monotonic() -> float:
    """Um único relógio monotónico por operação; nada aqui é renovado a meio."""
    return time.monotonic()


def _reading_level(policy: WebConstraints, mode: str) -> str:
    order = {'quick': 0, 'verified': 1, 'deep': 2}
    level = policy.depth if policy.depth in order else 'quick'
    if order.get(mode, 0) > order[level]:
        level = mode
    return level


def _operation_budget(policy: WebConstraints, mode: str, *, floor: float) -> float:
    """Orçamento total da operação: quick 10 s, verified 45 s, deep 60 s."""
    budget = {'quick': SEARCH_TIMEOUT, 'verified': VERIFIED_TIMEOUT,
              'deep': DEEP_TIMEOUT}[_reading_level(policy, mode)]
    return max(budget, floor)


def _read_candidates(policy: WebConstraints, mode: str, query: str) -> int:
    """Leituras por operação: quick explícito 2, verified 3, deep automático 4.

    Atributos finitos (contagem, cargo, preço, valuation) continuam a ler um
    candidato de cada vez; é a evidência do atributo que decide parar, não um
    número fixo de páginas.
    """
    if mode == 'quick':
        return QUICK_READS
    if requested_attribute(query):
        return DEEP_READS if mode == 'deep' else QUICK_READS
    if policy.freshness:
        # Janela temporal: lê um a um e pára na primeira página com data válida;
        # o filtro de frescura continua a decidir a lista final de fontes.
        return QUICK_READS
    if policy.depth == 'deep':
        return AUTO_DEEP_READS
    if mode == 'deep' or policy.depth == 'verified':
        return VERIFIED_READS
    return QUICK_READS


def _reads_together(policy: WebConstraints, mode: str, query: str) -> bool:
    """`deep` e comparações leem os candidatos em lote; `verified` lê um a um."""
    return (_reading_level(policy, mode) == 'deep'
            or requested_attribute(query) == 'valuation'
            or bool(re.search(
                r'\b(?:compara\w*|compar\w*|várias fontes|varias fontes|duas fontes|'
                r'multiple sources|vantagens e desvantagens)\b', query, re.IGNORECASE)))


_READ_ATTEMPT = {
    'timeout': 'a candidate page read timed out',
    'failed': 'a candidate page read failed',
    'blocked': 'a candidate page read was blocked',
}
_NO_SUBSTANTIVE = 'a candidate page returned no substantive content'
_BUDGET_EXHAUSTED = 'reading stopped: the operation time budget was exhausted'


def _add_warning(warnings: list[str], message: str) -> None:
    if message not in warnings and len(warnings) < 6:
        warnings.append(message)


_ATTRIBUTE_MISSING = {
    'count': 'read pages do not state the requested count',
    'office': 'read pages do not name the current office holder',
    'fixture': 'read pages do not state a dated next fixture',
    'price': 'read pages do not show a comparable price for the requested model and region',
    'valuation': 'read pages do not state a dated valuation amount',
}
_ATTRIBUTE_STALE = {
    'count': 'dated pages do not confirm the current count',
    'office': 'dated pages do not confirm the current office holder',
    'fixture': 'dated pages do not confirm the next fixture',
    'price': 'dated pages do not confirm a current comparable price',
    'valuation': 'dated pages do not confirm a current valuation',
}


def _attribute_warning(query: str, problem: str) -> str:
    attribute = requested_attribute(query)
    if problem == 'missing':
        return _ATTRIBUTE_MISSING.get(attribute, 'read pages do not state the requested fact')
    if problem == 'stale':
        return _ATTRIBUTE_STALE.get(attribute, 'dated pages do not confirm the current value')
    if problem == 'cached_only':
        return 'cached pages cannot confirm a current value'
    if problem == 'single_offer':
        return 'only one comparable price was found; this is not a best-price comparison'
    if problem == 'mixed_valuation':
        return 'valuation figures differ in currency or date; no single current value'
    return ''


def _enough_evidence(query: str, documents: list[dict[str, Any]], policy: WebConstraints,
                    sources: list[dict[str, Any]], read_ids: set[str]) -> bool:
    """Um fetch útil só termina a leitura quando sustenta o atributo pedido."""
    if requested_attribute(query):
        return attribute_evidence_problem(query, documents) == ''
    return any(source['id'] in read_ids and policy.dated(source) for source in sources)


def _warnings(payload: dict[str, Any]) -> list[str]:
    """Motores reais/benched, só nomes e razões fechadas; nunca queries ou URLs."""
    warnings: list[str] = []
    engines = _engines(payload.get("engines"))
    if engines:
        warnings.append("answered via " + ", ".join(engines))
    benched = payload.get("benched_engines")
    if isinstance(benched, dict):
        for name in sorted(benched)[:8]:
            reason = _clean(benched[name] if not isinstance(benched[name], dict)
                            else benched[name].get("reason"), 40)
            warnings.append(f"{_clean(name, 40)} benched" + (f" ({reason})" if reason else ""))
    rescued = payload.get("rescued_via")
    if isinstance(rescued, str) and rescued.strip():
        warnings.append("rescued via " + _clean(rescued, 40))
    errors = payload.get("errors")
    if isinstance(errors, dict):
        names = [_clean(name, 40) for name in sorted(errors)[:8]]
        if names:
            warnings.append("engine errors: " + ", ".join(names))
    return warnings[:10]


class MCPWebClient:
    """Uma sessão stdio MCP, dona única do contexto e do processo, com idle/cancel."""

    def __init__(self, environment: WebEnvironment, *,
                 idle_seconds: float = IDLE_SECONDS,
                 init_timeout: float = INIT_TIMEOUT,
                 reader: Callable[[Any], Any] | None = None) -> None:
        self.environment = environment
        self.idle_seconds = idle_seconds
        self.init_timeout = init_timeout
        self._reader = reader or _tool_payload
        self._thread: threading.Thread | None = None
        self._thread_loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[Any] | None = None
        self._owner: asyncio.Task[Any] | None = None
        self._ready: concurrent.futures.Future[Any] | None = None

    @property
    def started(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._queue is not None)

    async def start(self) -> None:
        if self.started:
            return
        if not self.environment.available:
            raise WebSearchError("unavailable")
        await asyncio.to_thread(self._start_blocking)

    def _start_blocking(self) -> None:
        if self.started:
            return
        ready: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self._ready = ready
        thread = threading.Thread(target=self._run_thread, args=(ready,),
                                  name="lunmi-web-mcp", daemon=True)
        self._thread = thread
        thread.start()
        try:
            ready.result(timeout=self.init_timeout + 3.0)
        except concurrent.futures.TimeoutError:
            self._abort_blocking()
            raise WebSearchError("timeout") from None
        except WebSearchError:
            raise
        except Exception:
            raise WebSearchError("failed") from None

    def _run_thread(self, ready: concurrent.futures.Future[Any]) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._thread_loop = loop
        try:
            loop.run_until_complete(self._serve(ready))
        except asyncio.CancelledError:
            pass
        except Exception:
            if not ready.done():
                ready.set_exception(WebSearchError("failed"))
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            loop.close()
            self._thread_loop = None
            self._queue = None

    async def _serve(self, ready: concurrent.futures.Future[Any]) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._queue = queue
        self._owner = asyncio.current_task()
        env, work = _worker_environment(self.environment)
        params = StdioServerParameters(
            command=str(self.environment.python),
            args=["-I", "-B", str(self.environment.worker)],
            env=env,
            cwd=str(work),
        )
        # O SDK exige um handle OS real em `errlog`; `io.TextIOBase` não tem
        # `fileno()`. Abrimos o dispositivo nulo e fechamo-lo no `finally`, sem
        # guardar stderr do upstream (que pode conter queries/URLs).
        errlog = open(os.devnull, "w", encoding="utf-8")
        try:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), timeout=self.init_timeout)
                    if not ready.done():
                        ready.set_result(True)
                    while True:
                        try:
                            item = await asyncio.wait_for(queue.get(),
                                                          timeout=self.idle_seconds)
                        except asyncio.TimeoutError:
                            break
                        if item is None:
                            break
                        request: _Request = item
                        if request.future.done():
                            continue
                        try:
                            result = await asyncio.wait_for(
                                session.call_tool(request.tool, request.arguments),
                                timeout=request.timeout)
                        except asyncio.TimeoutError:
                            if not request.future.done():
                                request.future.set_exception(WebSearchError("timeout"))
                            break
                        except asyncio.CancelledError:
                            if not request.future.done():
                                request.future.set_exception(WebSearchError("timeout"))
                            raise
                        except Exception:
                            if not request.future.done():
                                request.future.set_exception(WebSearchError("failed"))
                            break
                        else:
                            if not request.future.done():
                                request.future.set_result(result)
        except asyncio.CancelledError:
            if not ready.done():
                ready.set_exception(WebSearchError("timeout"))
            raise
        except Exception:
            if not ready.done():
                ready.set_exception(WebSearchError("failed"))
        finally:
            errlog.close()
            while True:
                try:
                    queued = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if isinstance(queued, _Request) and not queued.future.done():
                    queued.future.set_exception(WebSearchError("failed"))
            self._owner = None

    async def call(self, tool: str, arguments: dict[str, Any], *, timeout: float) -> Any:
        if not self.started or self._thread_loop is None or self._queue is None:
            raise WebSearchError("failed")
        loop = self._thread_loop
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        request = _Request(tool=tool, arguments=dict(arguments), timeout=timeout,
                           future=future)
        loop.call_soon_threadsafe(self._queue.put_nowait, request)
        try:
            result = await asyncio.wait_for(asyncio.wrap_future(future),
                                            timeout=timeout + CANCEL_GRACE)
        except asyncio.TimeoutError:
            self._abort_blocking()
            raise WebSearchError("timeout") from None
        except asyncio.CancelledError:
            self._abort_blocking()
            raise
        except WebSearchError:
            raise
        except Exception:
            raise WebSearchError("failed") from None
        return self._reader(result)

    def _abort_blocking(self) -> None:
        loop, owner = self._thread_loop, self._owner
        if loop is not None and owner is not None:
            try:
                loop.call_soon_threadsafe(owner.cancel)
            except RuntimeError:
                pass

    async def close(self) -> None:
        await asyncio.to_thread(self._close_blocking)

    def _close_blocking(self) -> None:
        loop, queue, thread = self._thread_loop, self._queue, self._thread
        if loop is not None and queue is not None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, None)
            except RuntimeError:
                pass
        if thread is not None:
            thread.join(timeout=CANCEL_GRACE + 2.0)
            if thread.is_alive():
                self._abort_blocking()
                thread.join(timeout=CANCEL_GRACE)
        self._thread = None
        self._queue = None
        self._thread_loop = None


def _tool_payload(result: Any) -> Any:
    """Converte `CallToolResult` em JSON simples; nunca devolve texto bruto ao chamador."""
    if getattr(result, "isError", False):
        raise WebSearchError("failed")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            value = json.loads(text)
        except ValueError:
            continue
        return value
    raise WebSearchError("failed")


def _search_sources(payload: Any, *, prefer: str = "results", limit: int = MAX_SOURCES
                    ) -> tuple[list[dict[str, Any]], str]:
    if not isinstance(payload, dict):
        raise WebSearchError("failed")
    raw_retrieved = payload.get("retrieved_at")
    retrieved = raw_retrieved if isinstance(raw_retrieved, str) else _utc_now()
    # `search` devolve `results`; `research` devolve `sources`. Ambos são fontes públicas e
    # ambos precisam de passar pelo mesmo DTO fechado.
    results = payload.get(prefer)
    if not isinstance(results, list):
        results = payload.get("sources" if prefer == "results" else "results")
    if not isinstance(results, list):
        return [], retrieved
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        source = _source_from_result(item, retrieved_at=retrieved)
        if source is None or source["url"] in seen:
            continue
        seen.add(source["url"])
        sources.append(source)
        if len(sources) >= limit:
            break
    return sources, retrieved


def _merge_documents(sources: list[dict[str, Any]], payload: Any, *, quote: bool = False
                     ) -> list[dict[str, Any]]:
    # `fetch_batch` embrulha a lista em `{"result": [...]}`; `fetch` devolve um objeto.
    if isinstance(payload, dict):
        payload = payload.get("result")
    if not isinstance(payload, list):
        return []
    by_url = {source["url"]: source for source in sources}
    documents: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            url = public_url(str(item.get("url") or ""))
        except WebBlocked:
            continue
        source = by_url.get(url)
        if source is None:
            continue
        document = _document(item, source, quote=quote)
        if document is None:
            source["read"] = False
            continue
        source["read"] = True
        source["truncated"] = document["truncated"]
        source["cached"] = bool(item.get("method") == "cache")
        source["retrieved_at"] = document["retrieved_at"]
        if document.get("title") and document["title"] != source["url"]:
            source["title"] = _clean(document["title"], MAX_TITLE)
        if document.get("published_at"):
            source["published_at"] = document["published_at"]
            source["date_source"] = "document"
        documents.append(document)
    return documents


def _open_source(payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise WebSearchError("failed")
    source = _source_from_result(payload, retrieved_at=_utc_now())
    if source is None:
        raise WebSearchError("blocked")
    document = _document(payload, source)
    if document is None:
        return [source], []
    source["read"] = True
    source["truncated"] = document["truncated"]
    source["cached"] = bool(payload.get("method") == "cache")
    source["retrieved_at"] = document["retrieved_at"]
    source["snippet"] = _clean(document["content"], MAX_SNIPPET)
    if document.get("published_at"):
        source["published_at"] = document["published_at"]
        source["date_source"] = "document"
    return [source], [document]


class WebSearchManager:
    """Estado + lifecycle público; o controlador só vê `run`, `dto` e `configure`."""

    def __init__(self, *, emit: Callable[[dict[str, Any]], None], state_root: str | Path,
                 session_id: str, environment: WebEnvironment | None = None,
                 client_factory: Callable[[WebEnvironment], MCPWebClient] | None = None) -> None:
        self._emit = emit
        self._state_root = Path(state_root)
        self._environment = environment
        self._client_factory = client_factory or (
            lambda env: MCPWebClient(env, idle_seconds=IDLE_SECONDS))
        self._state = WebState(session_id=session_id)
        preferences = load_preferences(self._state_root)
        self._state.set_preferences(preferences)
        self._client: MCPWebClient | None = None
        self._operation = asyncio.Lock()

    # ------------------------------------------------------------------ estado --
    def _environment_now(self) -> WebEnvironment:
        return self._environment if self._environment is not None else (
            resolve_environment(self._state_root))

    @property
    def enabled(self) -> bool:
        return self._state.enabled

    @property
    def mode(self) -> str:
        return self._state.mode

    @property
    def available(self) -> bool:
        return self._environment_now().available

    def dto(self) -> dict[str, Any]:
        return self._state.dto(available=self.available)

    def _emit_state(self, state: str, message: str) -> dict[str, Any]:
        snapshot = self._state.bump(state=state, message=message)
        snapshot["available"] = self.available
        if not snapshot["available"]:
            snapshot["state"], snapshot["message"] = "idle", MESSAGES["unavailable"]
        elif not snapshot["enabled"]:
            snapshot["state"], snapshot["message"] = "idle", MESSAGES["disabled"]
        self._emit(dict(snapshot))
        return snapshot

    async def configure(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = WebPreferences(enabled=self._state.enabled, mode=self._state.mode)
        try:
            preferences = apply_preferences(current, payload)
        except WebPreferencesError as exc:
            raise WebSearchError(exc.code) from None
        try:
            save_preferences(self._state_root, preferences)
        except WebPreferencesError as exc:
            raise WebSearchError(exc.code) from None
        self._state.set_preferences(preferences)
        if not preferences.enabled and not self._operation.locked():
            # Nunca fechar a sessão por baixo de uma operação em curso; o próximo `run`
            # já vê `enabled=false` e o idle-close trata do processo.
            await self._close_client()
        return self._emit_state("idle", MESSAGES["idle"] if preferences.enabled
                                else MESSAGES["disabled"])

    # --------------------------------------------------------------- lifecycle --
    async def _close_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass

    async def _ensure_client(self, env: WebEnvironment) -> MCPWebClient:
        if self._client is not None and self._client.started:
            return self._client
        await self._close_client()
        client = self._client_factory(env)
        await client.start()
        self._client = client
        return client

    async def close(self) -> None:
        async with self._operation:
            await self._close_client()

    # ------------------------------------------------------------------- tools --
    async def run(self, tool: str, arguments: dict[str, Any], *,
                  context_budget: int | None = None, mode: str | None = None) -> WebResult:
        env = self._environment_now()
        if not env.available:
            raise WebSearchError("unavailable")
        if not self._state.enabled:
            raise WebSearchError("disabled")
        mode = self._state.mode if mode is None else mode
        if mode not in {'auto', 'quick', 'deep'}:
            raise WebSearchError('invalid_payload')
        async with self._operation:
            first = self._client is None or not self._client.started
            if first:
                self._emit_state("starting", MESSAGES["starting"])
            started = _monotonic()
            try:
                if tool == "web.open":
                    result = await self._open(env, arguments, started, first)
                elif tool == "web.search":
                    result = await self._search(env, arguments, started, first,
                                                context_budget=context_budget, mode=mode)
                elif tool == "web.research":
                    result = await self._research(env, arguments, started, first,
                                                  context_budget=context_budget, mode=mode)
                else:
                    raise WebSearchError("invalid_payload")
                # Um resultado entregue termina a operação visível: sem isto o spinner
                # ficava em `starting`/`reading` depois de a consulta já ter terminado.
                # Erros, timeout, cancelamento e `disabled` continuam a sair pelos ramos
                # abaixo, sem passar por este idle.
                self._emit_state("idle", MESSAGES["idle"] if self._state.enabled
                                 else MESSAGES["disabled"])
                return result
            except WebSearchError as exc:
                if exc.code in {"timeout", "failed"}:
                    await self._close_client()
                if exc.code == "invalid_payload":
                    raise
                self._emit_state("error", MESSAGES.get(exc.code, MESSAGES["failed"]))
                raise
            except (asyncio.CancelledError, TimeoutError):
                await self._close_client()
                self._emit_state("error", MESSAGES["timeout"])
                raise

    async def _open(self, env: WebEnvironment, arguments: dict[str, Any],
                    started: float, first: bool) -> WebResult:
        try:
            url = public_url(str(arguments.get("url") or ""))
        except WebBlocked as exc:
            reason = str(exc)
            raise WebSearchError("blocked" if reason in {"private_host", "private_address"}
                                 else "invalid_payload") from None
        self._emit_state("reading", MESSAGES["reading"])
        client = await self._ensure_client(env)
        payload = await client.call("fetch", {
            "url": url, "render": "http", "format": "json", "inline": False,
        }, timeout=_remaining(OPEN_TIMEOUT, started))
        sources, documents = _open_source(payload)
        return WebResult(tool="web.open", sources=sources, documents=documents,
                         limited=not bool(documents))

    async def _search(self, env: WebEnvironment, arguments: dict[str, Any],
                      started: float, first: bool, *,
                      context_budget: int | None, mode: str) -> WebResult:
        query = _query(arguments)
        policy = constraints(query)
        budget = _operation_budget(policy, mode, floor=SEARCH_TIMEOUT)
        candidate_limit = 15 if policy.read_required or mode == 'deep' else MAX_SOURCES
        self._emit_state("searching", MESSAGES["searching"])
        client = await self._ensure_client(env)
        payload = await client.call("search", {
            "query": search_query(query, policy), "format": "json",
            "max_results": candidate_limit, "use_cache": True, **policy.parameters(),
        }, timeout=min(SEARCH_TIMEOUT, _remaining(budget, started)))
        sources, _ = _search_sources(payload, limit=candidate_limit)
        sources = rank_sources(query, sources, policy)
        warnings = _warnings(payload if isinstance(payload, dict) else {})
        documents: list[dict[str, Any]] = []
        # O atalho de excertos é só para perguntas quick que não exigem fonte; uma
        # pergunta verified/deep lê páginas mesmo com excertos longos.
        snippets_only = bool(sources and mode != "deep" and policy.depth == "quick"
                             and not policy.read_required
                             and all(policy.dated(source) for source in sources)
                             and _snippets_sufficient(sources))
        if snippets_only:
            warnings.append("search snippets only; pages were not fetched")
        elif sources:
            self._emit_state("reading", MESSAGES["reading"])
            count = _read_candidates(policy, mode, query)
            documents, read_warnings, client_failed = await self._read_pages(
                client, sources, policy, query, count, _reads_together(policy, mode, query),
                budget, started)
            warnings.extend(read_warnings)
            if client_failed:
                await self._close_client()
        evidence = 'suff' if documents or snippets_only else 'insufficient'
        if policy.freshness:
            dated = [source for source in sources if policy.dated(source)]
            sources = dated
            ids = {source['id'] for source in sources}
            documents = [doc for doc in documents if doc['id'] in ids]
            if not dated:
                evidence = 'insufficient'
                warnings.append('publication date not verified for requested time window')
        if policy.official and not documents:
            evidence = 'partial' if sources else 'insufficient'
            warnings.append('official pages were not read')
        if policy.official and not policy.domains:
            evidence = 'partial' if sources else 'insufficient'
            warnings.append('official domain not verified; candidates are not official evidence')
        if policy.quote and sources:
            evidence = 'partial'
            warnings.append('approximate reference only; live market quote not verified')
        problem = attribute_evidence_problem(query, documents)
        if problem:
            evidence = 'partial' if problem in {
                'single_offer', 'stale', 'cached_only', 'mixed_valuation'} else 'insufficient'
            warnings.append(_attribute_warning(query, problem))
        if len(sources) > MAX_SOURCES:
            read_ids = {document['id'] for document in documents}
            sources = sorted(sources, key=lambda source: source['id'] not in read_ids)[:MAX_SOURCES]
        return WebResult(tool="web.search", sources=sources, documents=documents,
                         warnings=warnings, limited=evidence != 'suff', evidence_status=evidence,
                         request_status={'suff': 'met', 'partial': 'partial',
                                         'insufficient': 'unmet'}[evidence])

    async def _read_pages(self, client: MCPWebClient, sources: list[dict[str, Any]],
                          policy: WebConstraints, query: str, count: int, multiple: bool,
                          timeout: float, started: float
                          ) -> tuple[list[dict[str, Any]], list[str], bool]:
        """Lê até `count` candidatos e preserva o que já foi lido se algo falhar.

        Um cookie wall ou um fetch falhado não é `read=true`: o candidato seguinte
        é tentado dentro do mesmo teto. Só `invalid_payload` e cancelamento
        interrompem a operação; os restantes limites ficam em avisos fechados.
        Cada tentativa usa `min(15 s, o que resta do orçamento da operação)`.
        """
        # Deep/comparison requests need several sources read together; `verified`
        # is a maximum, read one at a time until the evidence supports the answer.
        # A valuation is read twice to expose mixed currencies/dates before synthesis.
        documents: list[dict[str, Any]] = []
        warnings: list[str] = []
        client_failed = False
        # Failed candidates are not useful evidence; scan the finite pool under the same operation deadline.
        candidates = sources if policy.read_required and count > QUICK_READS else sources[:count]
        step = count if multiple else 1
        index = 0
        while index < len(candidates):
            batch = candidates[index:index + step]
            try:
                attempt_timeout = min(PER_ATTEMPT_HTTP, _remaining(timeout, started))
            except WebSearchError:
                _add_warning(warnings, _BUDGET_EXHAUSTED)
                break
            try:
                payload = await client.call('fetch_batch', {
                    'urls': [source['url'] for source in batch],
                    'render': 'http', 'format': 'json',
                }, timeout=attempt_timeout)
            except WebSearchError as exc:
                if exc.code == 'invalid_payload':
                    raise
                client_failed = client_failed or exc.code in {'timeout', 'failed'}
                _add_warning(warnings, _READ_ATTEMPT.get(
                    exc.code, 'a candidate page could not be read'))
                index += 1
                step = 1
                continue
            read_before = len(documents)
            documents.extend(_merge_documents(sources, payload, quote=policy.quote))
            # PMC may return only a cookie shell; Europe PMC publishes a separate, public OA copy.
            items = payload.get('result', []) if isinstance(payload, dict) else []
            for candidate in batch:
                match = re.fullmatch(r'https?://pmc\.ncbi\.nlm\.nih\.gov/articles/'
                                     r'(PMC\d{1,12})/?', candidate['url'], re.IGNORECASE)
                item = next((row for row in items if isinstance(row, dict)
                             and row.get('url') == candidate['url']), None) if isinstance(items, list) else None
                if (candidate['read'] or not match or not item or item.get('error')
                        or not isinstance(item.get('content'), str)
                        or useful_content(item['content'])):
                    continue
                alternative = ('https://www.ebi.ac.uk/europepmc/webservices/rest/'
                               + match[1].upper() + '/fullTextXML')
                try:
                    copy_payload = await client.call('fetch_batch', {
                        'urls': [alternative], 'render': 'http', 'format': 'json',
                    }, timeout=min(PER_ATTEMPT_HTTP, _remaining(timeout, started)))
                except WebSearchError as exc:
                    if exc.code == 'invalid_payload':
                        raise
                    client_failed = client_failed or exc.code in {'timeout', 'failed'}
                    _add_warning(warnings, 'public open-access copy could not be read')
                    continue
                copy_source = {**candidate, 'url': alternative, 'id': source_id(alternative), 'engines': []}
                copies = _merge_documents([copy_source], copy_payload, quote=policy.quote)
                if copies:
                    candidate.update(copy_source)
                    documents.extend(copies)
            if len(documents) - read_before < len(batch):
                _add_warning(warnings, _NO_SUBSTANTIVE)
            if len(documents) >= count:
                documents = documents[:count]
                break
            read_ids = {document['id'] for document in documents}
            if not multiple and _enough_evidence(query, documents, policy, sources, read_ids):
                break
            index += len(batch)
        return documents, warnings, client_failed

    async def _research(self, env: WebEnvironment, arguments: dict[str, Any],
                        started: float, first: bool, *,
                        context_budget: int | None, mode: str) -> WebResult:
        query = _query(arguments)
        policy = constraints(query)
        budget = _operation_budget(policy, mode, floor=RESEARCH_TIMEOUT)
        self._emit_state("searching", MESSAGES["searching"])
        client = await self._ensure_client(env)
        payload = await client.call("research", {
            "question": query, "depth": 5 if mode == "deep" else 3,
            "fetch": False, "format": "json", "use_cache": True,
            **policy.parameters(),
        }, timeout=min(RESEARCH_TIMEOUT, _remaining(budget, started)))
        if not isinstance(payload, dict):
            raise WebSearchError("failed")
        sources, _ = _search_sources(payload, prefer="sources")
        sources = rank_sources(query, sources, policy)
        warnings = _warnings(payload)
        documents: list[dict[str, Any]] = []
        if sources:
            self._emit_state("reading", MESSAGES["reading"])
            # `research` lê sempre até 3 páginas; só o orçamento muda com a profundidade.
            documents, read_warnings, client_failed = await self._read_pages(
                client, sources, policy, query, DEEP_READS, True, budget, started)
            warnings.extend(read_warnings)
            if client_failed:
                await self._close_client()
        if policy.freshness:
            sources = [source for source in sources if policy.dated(source)]
            ids = {source['id'] for source in sources}
            documents = [doc for doc in documents if doc['id'] in ids]
        evidence = 'suff' if documents and not policy.quote else ('partial' if sources else 'insufficient')
        if policy.official and not policy.domains:
            evidence = 'partial' if sources else 'insufficient'
            warnings.append('official domain not verified; candidates are not official evidence')
        if policy.quote:
            warnings.append('approximate reference only; live market quote not verified')
        problem = attribute_evidence_problem(query, documents)
        if problem:
            evidence = 'partial' if problem in {
                'single_offer', 'stale', 'cached_only', 'mixed_valuation'} else 'insufficient'
            warnings.append(_attribute_warning(query, problem))
        return WebResult(tool="web.research", sources=sources, documents=documents,
                         warnings=warnings, limited=evidence != 'suff', evidence_status=evidence,
                         request_status={'suff': 'met', 'partial': 'partial',
                                         'insufficient': 'unmet'}[evidence])


def _query(arguments: dict[str, Any]) -> str:
    try:
        return query_text(arguments.get("query"))
    except WebBlocked:
        raise WebSearchError("invalid_payload") from None


def _remaining(timeout: float, started: float) -> float:
    left = timeout - (_monotonic() - started)
    if left <= 0:
        raise WebSearchError("timeout")
    return left
