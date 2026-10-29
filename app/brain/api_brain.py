"""API-backed brains: wrap the existing REST providers behind the Brain contract."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.brain.contracts import (
    BrainError,
    BrainEvent,
    BrainHealth,
    Capabilities,
    Capability,
    ErrorKind,
    EventKind,
    HealthStatus,
    ModelInfo,
    events_for,
)
from app.llm import Completion, LLMProvider, ProviderError, create_provider
from app.settings import API_KEY_PROVIDERS, ConfigurationError, Settings

# Endpoints compatíveis com OpenAI para providers nomeados, usados quando não há URL própria.
PROVIDER_BASE_URLS: dict[str, str] = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "openrouter": "https://openrouter.ai/api/v1",
    # NVIDIA NIM (build.nvidia.com) expõe Chat Completions e /models no mesmo esquema.
    "nvidia": "https://integrate.api.nvidia.com/v1",
    # Groq: chave gratuita em console.groq.com/keys, sem cartão; o caminho mais curto para a
    # introdução da Lunmi chegar a um cérebro funcional.
    "groq": "https://api.groq.com/openai/v1",
    "ollama": "http://localhost:11434/v1",
    "lmstudio": "http://localhost:1234/v1",
    "vllm": "http://localhost:8000/v1",
}

# Padrão recomendado por provider, usado quando o catálogo do endpoint o oferece.
# A primeira linha de uma listagem raramente é a escolha útil — o catálogo da NVIDIA é
# alfabético e abre em `01-ai/yi-large` (2023) —, então o mesmo critério já aplicado aos
# runtimes de CLI (`CliSpec.preferred_model`) vale aqui. O marcador só é atribuído se o
# modelo aparecer na listagem: um id retirado do catálogo volta à ordem do endpoint.
PROVIDER_PREFERRED_MODELS: dict[str, str] = {
    "gemini": "gemini-3.8-flash",
    "nvidia": "nvidia/nemotron-3-super-120b-a12b",
    # Modelo de produção do Groq (docs/models): forte e rápido no plano gratuito.
    "groq": "openai/gpt-oss-120b",
}

# O endpoint oficial de inferência Gemini é OpenAI-compatible (documentado pela
# Google). O *catálogo* nativo é usado apenas quando o host é inequivocamente o da
# Google: é ele que expõe `supportedGenerationMethods`, `baseModelId` e paginação.
# Um endpoint customizado conserva o contrato OpenAI-compatible anunciado e nunca
# recebe a chave num parâmetro de URL.
GEMINI_NATIVE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_CATALOG_PAGE_SIZE = 1000
GEMINI_CATALOG_MAX_PAGES = 10
GEMINI_CATALOG_TIMEOUT_SECONDS = 30.0
_GEMINI_NON_CHAT_ID = re.compile(
    r"(?:^|[-_/])(embedding|imagen|image|tts|audio|veo|video|live|native-audio|robotics)(?:[-_/]|$)",
    re.IGNORECASE,
)


# O transporte REST fala ProviderError (RuntimeError); a camada de cérebros fala
# BrainError. Sem esta tradução, uma chave recusada *na inferência* chegava como falha
# inesperada, enquanto o mesmo 401 na listagem de modelos já virava
# `invalid_credentials` — e é o tipo do erro que o teste de modelo mostra e que a
# classificação de falha da tarefa usa. Só o código HTTP é interpretado: o corpo da
# resposta nunca é lido nem citado.
_HTTP_STATUS_KINDS: dict[int, ErrorKind] = {
    400: ErrorKind.CONFIGURATION,
    401: ErrorKind.INVALID_CREDENTIALS,
    402: ErrorKind.QUOTA_EXCEEDED,
    403: ErrorKind.INVALID_CREDENTIALS,
    404: ErrorKind.MODEL_UNAVAILABLE,
    413: ErrorKind.REQUEST_TOO_LARGE,
    422: ErrorKind.CONFIGURATION,
    429: ErrorKind.RATE_LIMITED,
}
_HTTP_STATUS_PATTERN = re.compile(r"HTTP (\d{3})")
_KIND_MESSAGES: dict[ErrorKind, str] = {
    ErrorKind.INVALID_CREDENTIALS: "A chave de API foi recusada pelo provedor.",
    ErrorKind.QUOTA_EXCEEDED: "A conta do provedor está sem cota ou saldo para este modelo.",
    ErrorKind.RATE_LIMITED: ("O provedor atingiu um limite de uso. O Groq gratuito limita tokens por "
                             "minuto e por dia; tenta de novo mais tarde ou usa outro cérebro."),
    ErrorKind.MODEL_UNAVAILABLE: "O provedor não reconhece este modelo. Escolha outro na lista.",
    ErrorKind.REQUEST_TOO_LARGE: "O pedido excedeu o limite de tamanho do provedor.",
    ErrorKind.CONFIGURATION: "O provedor recusou o pedido: confira modelo, chave e endpoint.",
    ErrorKind.TIMEOUT: "O provedor não respondeu dentro do tempo limite.",
}


def translate_provider_error(exc: ProviderError) -> BrainError:
    """Transport failure -> the application's own error vocabulary."""
    message = str(exc)
    status = _HTTP_STATUS_PATTERN.search(message)
    if status is None:
        kind = ErrorKind.TIMEOUT if "Timeout" in message else ErrorKind.PROVIDER_UNAVAILABLE
        # Sem código HTTP, a mensagem do transporte já é a informação mais honesta
        # ("Falha de conexão com a API do modelo.") e não carrega corpo nem credencial.
        return BrainError(kind, _KIND_MESSAGES.get(kind, message) if kind is ErrorKind.TIMEOUT else message,
                          detail=message)
    kind = _HTTP_STATUS_KINDS.get(int(status.group(1)), ErrorKind.PROVIDER_UNAVAILABLE)
    return BrainError(kind, _KIND_MESSAGES.get(kind, message), detail=message)


def resolve_base_url(provider: str, base_url: str) -> str:
    """URL base efetiva; vazio quando o provider não é compatível com OpenAI."""
    if provider == "custom":
        return base_url.strip()
    if provider in PROVIDER_BASE_URLS:
        return base_url.strip() or PROVIDER_BASE_URLS[provider]
    return ""


# Capabilities every HTTP model API shares, advertised to the UI without
# constructing a provider.
API_CAPABILITIES = Capabilities.of(
    Capability.SYSTEM_PROMPTS,
    Capability.STRUCTURED_OUTPUT,
    Capability.USAGE,
    Capability.CANCELLATION,
    Capability.MODEL_DISCOVERY,
)


def api_capabilities(provider_id: str) -> Capabilities:
    # Unknown compatible endpoints keep final-only delivery until their SSE is verified.
    if provider_id in {"openai", "anthropic"}:
        return Capabilities(API_CAPABILITIES.values | {Capability.STREAMING})
    return API_CAPABILITIES


class ApiBrain:
    """An HTTP model API (OpenAI, Anthropic or any OpenAI-compatible endpoint).

    Delegates transport to the existing providers; this class only adds
    capabilities, normalization and a *free* readiness probe.
    """

    def __init__(self, settings: Settings) -> None:
        self.provider_id = settings.provider
        if settings.provider == "gemini":
            # O catálogo nativo e os exemplos oficiais usam `gemini-...`; uma seleção
            # antiga gravada como `models/gemini-...` continua válida na inferência.
            settings = replace(settings, model=_normalize_gemini_model(settings.model))
        self.settings = settings
        self.base_url = resolve_base_url(settings.provider, settings.base_url)
        # Preenchido por health() com a listagem que acabou de ser obtida nesta
        # instância; verify reutiliza-a em vez de fazer um segundo GET do catálogo.
        self._fresh_models: tuple[ModelInfo, ...] | None = None
        self._fresh_models_at = 0.0
        self._fresh_failure: BrainError | None = None
        self._fresh_failure_at = 0.0
        try:
            # Falha cedo: configuração inválida nunca deve virar erro de rede mais tarde.
            # O modelo é exigido na inferência, não na construção: listar o catálogo
            # precisa do transporte, e um provider sem modelo ainda pode mostrar a lista.
            settings.validate(model=False)
            inner = settings
            if self.base_url and settings.provider != "custom":
                # Providers nomeados reutilizam o cliente compatível com OpenAI.
                inner = replace(settings, provider="custom", base_url=self.base_url)
            # O transporte não exige modelo (o catálogo precisa dele, não a inferência);
            # `complete` é quem recusa a inferência sem modelo, com erro tipado.
            from app.brain.effort import reasoning_parameters
            parameters = reasoning_parameters(settings)
            self._provider: LLMProvider = create_provider(inner, require_model=False)
            self._provider.reasoning_parameters = parameters
        except ConfigurationError as exc:
            raise BrainError(ErrorKind.CONFIGURATION, str(exc)) from exc
        self.capabilities = api_capabilities(self.provider_id)
        if self.provider_id == "gemini" and self._uses_official_gemini_endpoint():
            # Google documents SSE for the official OpenAI-compatible endpoint, and
            # the existing OpenAI transport already parses it. A custom Gemini host
            # stays final-only because its SSE contract was never verified here.
            self.capabilities = Capabilities(
                self.capabilities.values | {Capability.STREAMING})
        self.call_metrics = self._provider.call_metrics

    async def complete(self, messages: list[dict[str, Any]]) -> Completion:
        if not self.settings.model.strip():
            raise BrainError(ErrorKind.CONFIGURATION,
                             "Nenhum modelo selecionado para este provider.")
        try:
            return await self._provider.complete(messages)
        except ProviderError as exc:
            raise translate_provider_error(exc) from exc

    def events(self, completion: Completion) -> list[BrainEvent]:
        return events_for(self.provider_id, completion)

    async def stream(self, messages: list[dict[str, Any]]) -> AsyncIterator[BrainEvent]:
        incremental = self.capabilities.supports(Capability.STREAMING)
        yield BrainEvent(EventKind.STARTED, {"provider": self.provider_id, "streaming": incremental})
        if not incremental:
            for event in self.events(await self.complete(messages))[1:]:
                yield event
            return
        if not self.settings.model.strip():
            raise BrainError(ErrorKind.CONFIGURATION, "Nenhum modelo selecionado para este provider.")
        try:
            completion = None
            async with aclosing(self._provider.stream(messages)) as events:
                async for value in events:
                    if isinstance(value, str):
                        yield BrainEvent(EventKind.TEXT_DELTA, {"text": value})
                    else:
                        completion = value
            if completion is None:
                raise BrainError(ErrorKind.MALFORMED_RESPONSE, "O provedor não concluiu a resposta.")
            for event in self.events(completion)[1:]:
                yield event
        except ProviderError as exc:
            raise translate_provider_error(exc) from exc

    def fresh_models(self, *, max_age: float = 5.0) -> tuple[ModelInfo, ...] | None:
        """The catalogue this instance fetched moments ago, or ``None``.

        Only the same instance (same provider/endpoint/key selection) can expose it;
        it is never a process-wide cache and never a substitute for authentication.
        """
        if self._fresh_models is None or time.monotonic() - self._fresh_models_at > max_age:
            return None
        return self._fresh_models

    def fresh_failure(self, *, max_age: float = 5.0) -> BrainError | None:
        """The catalogue error health() just saw on this instance, or ``None``."""
        if self._fresh_failure is None or time.monotonic() - self._fresh_failure_at > max_age:
            return None
        return self._fresh_failure

    def _uses_official_gemini_endpoint(self) -> bool:
        """Only the documented Gemini OpenAI-compatible endpoint, as a whole URL.

        A custom endpoint that merely borrows the Google hostname but points at a
        different path (or carries a query/port) is not the official contract: it keeps
        final-only delivery and the OpenAI-style catalogue.
        """
        if self.settings.provider != "gemini":
            return False
        if not self.base_url:
            return True
        parsed = urlsplit(self.base_url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "generativelanguage.googleapis.com"
            and parsed.port in {None, 443}
            and parsed.path.rstrip("/") == "/v1beta/openai"
            and not parsed.query
            and not parsed.fragment
        )

    def _uses_native_gemini_catalog(self) -> bool:
        return self._uses_official_gemini_endpoint()

    def _models_endpoint(self) -> tuple[str, dict[str, str]]:
        settings = self.settings
        if settings.provider == "anthropic":
            base = "https://api.anthropic.com/v1"
        else:
            base = self.base_url or "https://api.openai.com/v1"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if settings.api_key:
            if settings.provider == "anthropic":
                headers["x-api-key"] = settings.api_key
            else:
                headers["Authorization"] = f"Bearer {settings.api_key}"
        if settings.provider == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        return base.rstrip("/") + "/models", headers

    async def models(self) -> list[ModelInfo]:
        """List models without spending inference tokens, when the endpoint allows it."""
        if self._uses_native_gemini_catalog():
            return await self._gemini_catalog()
        url, headers = self._models_endpoint()
        try:
            response = await self._provider.client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise BrainError(ErrorKind.PROVIDER_UNAVAILABLE,
                             "Não foi possível alcançar o endpoint do modelo.", detail=str(exc)) from exc
        if response.status_code in {401, 403}:
            raise BrainError(ErrorKind.INVALID_CREDENTIALS, "A chave de API foi recusada pelo provedor.")
        if response.status_code == 429:
            raise BrainError(ErrorKind.RATE_LIMITED, "Limite de requisições atingido no provedor.")
        if response.status_code != 200:
            raise BrainError(ErrorKind.UNSUPPORTED_FEATURE,
                             f"O endpoint não listou modelos (HTTP {response.status_code}).",
                             detail="catálogo não exposto; tente outro provider ou atualize")
        try:
            payload = response.json()
        except ValueError as exc:
            raise BrainError(ErrorKind.MALFORMED_RESPONSE,
                             "A lista de modelos do provedor não é JSON válido.") from exc
        raw = payload.get("data") if isinstance(payload, dict) else None
        if raw is None and isinstance(payload, dict):
            raw = payload.get("models")
        if not isinstance(raw, list):
            raise BrainError(ErrorKind.MALFORMED_RESPONSE, "Formato de lista de modelos inesperado.")
        models: list[ModelInfo] = []
        preferred = PROVIDER_PREFERRED_MODELS.get(self.settings.provider, "")
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            identifier = entry.get("id") or entry.get("name")
            if isinstance(identifier, str) and identifier:
                label = entry.get("display_name") or entry.get("displayName") or identifier
                models.append(ModelInfo(identifier, str(label), default=identifier == preferred))
        return models

    async def health(self) -> BrainHealth:
        if self.settings.provider in API_KEY_PROVIDERS and not self.settings.api_key:
            return BrainHealth(HealthStatus.MISCONFIGURED, "Configure sua chave de API.")
        if not self.settings.model.strip():
            return BrainHealth(HealthStatus.MISCONFIGURED,
                               "Escolha um modelo no catálogo do provider.")
        try:
            models = await self.models()
        except BrainError as exc:
            self._fresh_failure = exc
            self._fresh_failure_at = time.monotonic()
            if exc.kind is ErrorKind.UNSUPPORTED_FEATURE:
                return BrainHealth(HealthStatus.READY,
                                   "Endpoint alcançado; este provedor não lista modelos.")
            return BrainHealth(HealthStatus.UNAVAILABLE, exc.message)
        self._fresh_failure = None
        self._fresh_models = tuple(models)
        self._fresh_models_at = time.monotonic()
        if not models:
            # Um catálogo vazio não valida modelo nenhum. READY aqui era o falso
            # "conexão ok" que deixava verify passar com model inexistente.
            return BrainHealth(HealthStatus.MISCONFIGURED,
                               "O catálogo não retornou nenhum modelo de chat. "
                               "Atualize a lista ou escolha outro provider.")
        if all(model.id != self.settings.model for model in models):
            return BrainHealth(HealthStatus.MISCONFIGURED,
                               f"O modelo {self.settings.model} não existe neste endpoint.")
        return BrainHealth(HealthStatus.READY, f"{len(models)} modelos disponíveis.")

    async def _gemini_catalog(self) -> list[ModelInfo]:
        """Native Gemini catalogue: paginated, chat-capable, normalized ids.

        The key travels in `x-goog-api-key` (the documented Google header), never in
        the URL or in an error detail. A repeated `nextPageToken` or a page budget
        overrun is a visible malformed/unsupported answer, never silent truncation.
        """
        headers = {"Accept": "application/json"}
        if self.settings.api_key:
            headers["x-goog-api-key"] = self.settings.api_key
        url = GEMINI_NATIVE_BASE_URL + "/models"
        page_token = ""
        models: list[ModelInfo] = []
        seen: set[str] = set()
        seen_tokens: set[str] = set()
        deadline = time.monotonic() + GEMINI_CATALOG_TIMEOUT_SECONDS
        for _page in range(GEMINI_CATALOG_MAX_PAGES):
            params: dict[str, str | int] = {"pageSize": GEMINI_CATALOG_PAGE_SIZE}
            if page_token:
                params["pageToken"] = page_token
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrainError(ErrorKind.TIMEOUT,
                                 "O catálogo do Gemini não terminou dentro do tempo limite.")
            try:
                async with asyncio.timeout(remaining):
                    response = await self._provider.client.get(
                        url, params=params, headers=headers, timeout=remaining)
            except (asyncio.TimeoutError, TimeoutError) as exc:
                raise BrainError(ErrorKind.TIMEOUT,
                                 "O catálogo do Gemini não terminou dentro do tempo limite.") from exc
            except httpx.HTTPError as exc:
                raise BrainError(ErrorKind.PROVIDER_UNAVAILABLE,
                                 "Não foi possível alcançar o catálogo do Gemini.",
                                 detail=type(exc).__name__) from exc

            if response.status_code in {400, 401, 403}:
                # A chave inválida do Gemini é 400 (API_KEY_INVALID), não só 401/403.
                raise BrainError(ErrorKind.INVALID_CREDENTIALS,
                                 "A chave de API foi recusada pelo provedor.")
            if response.status_code == 429:
                raise BrainError(ErrorKind.RATE_LIMITED,
                                 "Limite de requisições atingido no provedor.")
            if response.status_code != 200:
                if response.status_code >= 500 or response.status_code < 400:
                    raise BrainError(ErrorKind.PROVIDER_UNAVAILABLE,
                                     f"O catálogo do Gemini está indisponível (HTTP {response.status_code}).")
                # 404 e restantes 4xx: erro de configuração/consulta, nunca o fallback
                # READY de um endpoint que simplesmente não expõe catálogo.
                raise BrainError(ErrorKind.CONFIGURATION,
                                 f"O catálogo do Gemini recusou a consulta (HTTP {response.status_code}).")
            try:
                payload = response.json()
            except ValueError as exc:
                raise BrainError(ErrorKind.MALFORMED_RESPONSE,
                                 "O catálogo do Gemini não é JSON válido.") from exc
            raw = payload.get("models") if isinstance(payload, dict) else None
            if not isinstance(raw, list):
                raise BrainError(ErrorKind.MALFORMED_RESPONSE,
                                 "Formato de catálogo do Gemini inesperado.")
            for entry in raw:
                option = self._gemini_model_option(entry)
                if option is not None and option.id not in seen:
                    seen.add(option.id)
                    models.append(option)
            token = payload.get("nextPageToken") if isinstance(payload, dict) else None
            if not token:
                return models
            if not isinstance(token, str):
                raise BrainError(ErrorKind.MALFORMED_RESPONSE,
                                 "Paginação do catálogo do Gemini inválida.")
            if token in seen_tokens:
                raise BrainError(ErrorKind.MALFORMED_RESPONSE,
                                 "O catálogo do Gemini repetiu um token de paginação.")
            seen_tokens.add(token)
            page_token = token
        raise BrainError(ErrorKind.MALFORMED_RESPONSE,
                         f"O catálogo do Gemini excedeu {GEMINI_CATALOG_MAX_PAGES} páginas; "
                         "a lista não foi truncada em silêncio.")

    def _gemini_model_option(self, entry: object) -> ModelInfo | None:
        if not isinstance(entry, dict):
            return None
        methods = entry.get("supportedGenerationMethods")
        if not isinstance(methods, list) or "generateContent" not in methods:
            return None
        # Prefer the resource name: `baseModelId` can be shared by distinct versions
        # (`models/gemini-x-001` and `-002`), and collapsing them would hide valid
        # selections. `baseModelId` is only the fallback when `name` is absent.
        raw_id = entry.get("name") or entry.get("baseModelId")
        if not isinstance(raw_id, str):
            return None
        identifier = _normalize_gemini_model(raw_id)
        if not identifier or _GEMINI_NON_CHAT_ID.search(identifier):
            return None
        display = entry.get("displayName")
        label = display.strip() if isinstance(display, str) and display.strip() else identifier
        preferred = PROVIDER_PREFERRED_MODELS.get("gemini", "")
        return ModelInfo(identifier, label, default=identifier == preferred)

    async def close(self) -> None:
        await self._provider.close()


def _normalize_gemini_model(model: str) -> str:
    """`models/gemini-...` is a resource name; generation takes `gemini-...`."""
    value = model.strip()
    return value[len("models/"):] if value.startswith("models/") else value
