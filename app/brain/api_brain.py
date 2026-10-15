"""API-backed brains: wrap the existing REST providers behind the Brain contract."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

import httpx

from app.brain.contracts import (
    BrainError,
    BrainEvent,
    BrainHealth,
    Capabilities,
    Capability,
    ErrorKind,
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
    "nvidia": "nvidia/nemotron-3-super-120b-a12b",
}


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
    ErrorKind.RATE_LIMITED: "Limite de requisições atingido no provedor; tente de novo em instantes.",
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


class ApiBrain:
    """An HTTP model API (OpenAI, Anthropic or any OpenAI-compatible endpoint).

    Delegates transport to the existing providers; this class only adds
    capabilities, normalization and a *free* readiness probe.
    """

    def __init__(self, settings: Settings) -> None:
        self.provider_id = settings.provider
        self.settings = settings
        self.base_url = resolve_base_url(settings.provider, settings.base_url)
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
            self._provider: LLMProvider = create_provider(inner, require_model=False)
        except ConfigurationError as exc:
            raise BrainError(ErrorKind.CONFIGURATION, str(exc)) from exc
        self.capabilities = API_CAPABILITIES

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

    def _models_endpoint(self) -> tuple[str, dict[str, str]]:
        settings = self.settings
        if settings.provider == "anthropic":
            base = "https://api.anthropic.com/v1"
        else:
            base = self.base_url or "https://api.openai.com/v1"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if settings.api_key:
            headers["Authorization"] = f"Bearer {settings.api_key}"
        if settings.provider == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        return base.rstrip("/") + "/models", headers

    async def models(self) -> list[ModelInfo]:
        """List models without spending inference tokens, when the endpoint allows it."""
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
                             detail="informe o modelo manualmente")
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
            return BrainHealth(HealthStatus.MISCONFIGURED, "Informe o modelo.")
        try:
            models = await self.models()
        except BrainError as exc:
            if exc.kind is ErrorKind.UNSUPPORTED_FEATURE:
                return BrainHealth(HealthStatus.READY,
                                   "Endpoint alcançado; este provedor não lista modelos.")
            return BrainHealth(HealthStatus.UNAVAILABLE, exc.message)
        if models and all(model.id != self.settings.model for model in models):
            return BrainHealth(HealthStatus.MISCONFIGURED,
                               f"O modelo {self.settings.model} não existe neste endpoint.")
        return BrainHealth(HealthStatus.READY, f"{len(models)} modelos disponíveis.")

    async def close(self) -> None:
        await self._provider.close()
