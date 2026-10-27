from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app import settings as settings_module

from app.brain.api_brain import (
    PROVIDER_BASE_URLS,
    PROVIDER_PREFERRED_MODELS,
    ApiBrain,
    resolve_base_url,
    translate_provider_error,
)
from app.llm import ProviderError, create_provider
from app.brain.contracts import BrainError, ErrorKind
from app.settings import (
    API_KEY_PROVIDERS,
    CLI_PROVIDERS,
    LOCAL_ENDPOINT_PROVIDERS,
    SUPPORTED_PROVIDERS,
    ConfigurationError,
    Settings,
)


def test_selection_survives_restart_without_saving_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_module, "ROOT", tmp_path)
    for name in ("LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    # An older explicit credential opt-in must not override the new brain/model.
    settings_module.save_provider("openai", "old-model", "explicit-test-key", "")
    protected = (tmp_path / ".local/provider.dpapi").read_bytes()
    settings_module.save_selection("agy", "gemini-3.8-flash-low")
    saved = Settings.load()
    assert (saved.provider, saved.model, saved.api_key, saved.base_url) == (
        "agy", "gemini-3.8-flash-low", "", "")
    assert (tmp_path / ".local/provider.dpapi").read_bytes() == protected
    selection = json.loads((tmp_path / ".local/brain-selection.json").read_text(encoding="utf-8"))
    assert selection == {"provider": "agy", "model": "gemini-3.8-flash-low"}
    assert "explicit-test-key" not in json.dumps(selection)
    # Selecting the same provider again may use its previously consented key.
    settings_module.save_selection("openai", "new-model")
    saved = Settings.load()
    assert (saved.model, saved.api_key) == ("new-model", "explicit-test-key")
    # Explicit environment always wins; mismatched provider never inherits a key/URL.
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "environment-model")
    saved = Settings.load()
    assert (saved.provider, saved.model, saved.api_key, saved.base_url) == (
        "ollama", "environment-model", "", "")


def test_selection_validates_and_corrupt_state_is_a_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_module, "ROOT", tmp_path)
    with pytest.raises(ConfigurationError):
        settings_module.save_selection("unknown", "model")
    with pytest.raises(ConfigurationError):
        settings_module.save_selection("agy", "")
    settings_module.save_selection("agy", "model")
    selection = tmp_path / ".local/brain-selection.json"
    selection.write_text('[]', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="seleção"):
        Settings.load()


def test_app_brain_selection_is_remembered_without_secrets_and_removed_when_cleared(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FASE 3.1: o Brain do AppAgent é opcional, por par não-secreto, e limpo ao desligar."""
    monkeypatch.setattr(settings_module, "ROOT", tmp_path)
    for name in ("LLM_PROVIDER", "LLM_MODEL", "LLM_APP_PROVIDER", "LLM_APP_MODEL",
                 "LLM_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings_module.save_app_selection("qoder", "Qwen3.8-Flash")
    saved = Settings.load()
    assert (saved.provider, saved.app_provider, saved.app_model) == (
        "openai", "qoder", "Qwen3.8-Flash")
    payload = json.loads((tmp_path / ".local" / "app-brain.json").read_text(encoding="utf-8"))
    assert payload == {"provider": "qoder", "model": "Qwen3.8-Flash"}
    # O ambiente vence a escolha salva na UI.
    monkeypatch.setenv("LLM_APP_PROVIDER", "nvidia")
    monkeypatch.setenv("LLM_APP_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
    saved = Settings.load()
    assert (saved.app_provider, saved.app_model) == (
        "nvidia", "nvidia/nemotron-3.5-lightning-30b-a3b")
    # Um LLM_APP_MODEL explícito prevalece sem exigir LLM_APP_PROVIDER.
    monkeypatch.delenv("LLM_APP_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_APP_MODEL", raising=False)
    monkeypatch.setenv("LLM_APP_MODEL", "override-model")
    assert Settings.load().app_model == "override-model"
    monkeypatch.delenv("LLM_APP_MODEL", raising=False)
    # Desligar remove o arquivo em vez de deixar um par pela metade.
    settings_module.save_app_selection("", "")
    assert not (tmp_path / ".local" / "app-brain.json").exists()
    assert Settings.load().app_provider == ""


def test_app_brain_selection_validates_and_reports_corruption(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_module, "ROOT", tmp_path)
    for name in ("LLM_APP_PROVIDER", "LLM_APP_MODEL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigurationError):
        settings_module.save_app_selection("unknown", "model")
    # Provider-only selection is valid when it inherits the main model;
    # Settings.validate still rejects a different provider without its own model.
    settings_module.save_app_selection("qoder", "")
    assert json.loads(settings_module.app_selection_path().read_text(encoding="utf-8")) == {
        "provider": "qoder", "model": ""}
    settings_module.save_app_selection("qoder", "model")
    (tmp_path / ".local" / "app-brain.json").write_text('{"provider": 1}', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="AppAgent"):
        Settings.load()


def test_settings_reject_a_half_configured_app_brain() -> None:
    """Um provider de AppAgent diferente exige o modelo dele; o mesmo pode herdar o principal."""
    with pytest.raises(ConfigurationError, match="AppAgent"):
        Settings(provider="openai", model="m", api_key="k", app_provider="nvidia").validate()
    Settings(provider="openai", model="m", api_key="k", app_provider="openai").validate()
    Settings(provider="kiro", model="auto", app_provider="kiro", app_model="fast").validate()
    with pytest.raises(ConfigurationError, match="AppAgent"):
        Settings(provider="kiro", model="auto", app_provider="nope", app_model="x").validate()


def test_local_endpoints_need_no_key() -> None:
    for provider in ("ollama", "lmstudio", "vllm"):
        Settings(provider=provider, model="llama3").validate()


def test_named_cloud_providers_require_a_key() -> None:
    for provider in ("gemini", "openrouter", "nvidia"):
        with pytest.raises(ConfigurationError, match="chave"):
            Settings(provider=provider, model="model").validate()
        Settings(provider=provider, model="model", api_key="key").validate()


def test_cli_providers_accept_neither_key_nor_base_url() -> None:
    for provider in ("claude", "agy", "qoder", "kilo", "kiro", "opencode", "codex", "freepi"):
        Settings(provider=provider, model="model").validate()
        with pytest.raises(ConfigurationError):
            Settings(provider=provider, model="model", api_key="key").validate()
        with pytest.raises(ConfigurationError, match="URL base"):
            Settings(provider=provider, model="model", base_url="https://example.test").validate()


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        Settings(provider="gemini-2", model="model").validate()


def test_local_endpoint_url_is_still_validated() -> None:
    Settings(provider="ollama", model="llama3", base_url="http://127.0.0.1:11434/v1").validate()
    with pytest.raises(ConfigurationError):
        Settings(provider="ollama", model="llama3", base_url="http://example.com/v1").validate()


def test_provider_groups_partition_the_supported_set() -> None:
    assert API_KEY_PROVIDERS | LOCAL_ENDPOINT_PROVIDERS | CLI_PROVIDERS == SUPPORTED_PROVIDERS
    assert not API_KEY_PROVIDERS & CLI_PROVIDERS
    assert not LOCAL_ENDPOINT_PROVIDERS & CLI_PROVIDERS


@pytest.mark.parametrize("provider,expected", [
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
    ("openrouter", "https://openrouter.ai/api/v1"),
    ("nvidia", "https://integrate.api.nvidia.com/v1"),
    ("ollama", "http://localhost:11434/v1"),
    ("lmstudio", "http://localhost:1234/v1"),
    ("vllm", "http://localhost:8000/v1"),
])
def test_named_providers_use_their_preset(provider: str, expected: str) -> None:
    assert resolve_base_url(provider, "") == expected
    assert resolve_base_url(provider, "http://127.0.0.1:9999/v1") == "http://127.0.0.1:9999/v1"


def test_presets_do_not_change_existing_providers() -> None:
    assert resolve_base_url("openai", "") == ""
    assert resolve_base_url("anthropic", "") == ""
    assert resolve_base_url("custom", "http://localhost:8000/v1") == "http://localhost:8000/v1"
    assert "openai" not in PROVIDER_BASE_URLS
    assert "anthropic" not in PROVIDER_BASE_URLS


def test_api_brain_uses_the_preset_for_completion_and_models() -> None:
    brain = ApiBrain(Settings(provider="ollama", model="llama3"))
    assert brain.provider_id == "ollama"
    assert brain.base_url == "http://localhost:11434/v1"
    url, _ = brain._models_endpoint()
    assert url == "http://localhost:11434/v1/models"
    # O cliente REST interno precisa receber o preset, não o endpoint padrão da OpenAI.
    assert brain._provider.settings.base_url == "http://localhost:11434/v1"


def test_api_brain_rejects_incomplete_configuration_early() -> None:
    with pytest.raises(BrainError) as failure:
        ApiBrain(Settings(provider="gemini", model="gemini-3"))
    assert failure.value.kind is ErrorKind.CONFIGURATION


def _listed_models(*ids: str, provider: str = "nvidia", key: str = "chave-de-teste") -> list[tuple[str, bool]]:
    """Consulta um catálogo falso pelo mesmo caminho REST do provider real."""
    brain = ApiBrain(Settings(provider=provider, model="", api_key=key))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        assert request.headers["Authorization"] == f"Bearer {key}"
        return httpx.Response(200, json={"object": "list", "data": [
            {"id": item, "object": "model", "owned_by": "test"} for item in ids]})

    brain._provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        return [(item.id, item.default) for item in asyncio.run(brain.models())]
    finally:
        asyncio.run(brain.close())


def test_nvidia_key_comes_from_its_own_variable_and_preset() -> None:
    """`NVIDIA_API_KEY` é o nome documentado pela NVIDIA; o preset é o catálogo hospedado."""
    settings = Settings(provider="nvidia", model="", api_key="nvapi-chave-de-teste")
    settings.validate(model=False)  # o diálogo lista o catálogo antes de haver modelo
    brain = ApiBrain(settings)
    assert brain.base_url == "https://integrate.api.nvidia.com/v1"
    url, headers = brain._models_endpoint()
    assert (url, headers["Authorization"]) == (
        "https://integrate.api.nvidia.com/v1/models", "Bearer nvapi-chave-de-teste")
    asyncio.run(brain.close())


def test_a_provider_without_a_model_can_still_be_listed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Trocar de provider e ver o catálogo não pode exigir um modelo já escolhido.

    O diálogo envia o provider com o campo Modelo ainda vazio; exigir modelo aí deixava
    a lista inalcançável ("Escolha um modelo em Configurar API."), que é o mesmo beco
    sem saída já medido noutros providers.
    """
    monkeypatch.setattr(settings_module, "ROOT", tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "nvidia")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-chave-de-teste")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    saved = Settings.load()
    assert (saved.provider, saved.model, saved.api_key) == (
        "nvidia", "", "nvapi-chave-de-teste")
    brain = ApiBrain(saved)
    assert brain.base_url == "https://integrate.api.nvidia.com/v1"
    # Sem modelo, a inferência continua recusada localmente, sem gastar rede.
    with pytest.raises(BrainError) as failure:
        asyncio.run(brain.complete([{"role": "user", "content": "oi"}]))
    assert failure.value.kind is ErrorKind.CONFIGURATION
    asyncio.run(brain.close())


def test_direct_transport_still_requires_a_model() -> None:
    """O relaxamento é do cérebro HTTP, não do transporte usado pelo caminho antigo."""
    with pytest.raises(ConfigurationError):
        create_provider(Settings(provider="openai", model="", api_key="test-key"))


def test_nvidia_preferred_model_is_marked_when_the_catalogue_offers_it() -> None:
    preferred = PROVIDER_PREFERRED_MODELS["nvidia"]
    assert _listed_models("01-ai/yi-large", preferred) == [("01-ai/yi-large", False), (preferred, True)]
    # Um id retirado do catálogo não deixa marcador órfão: vale a ordem do endpoint.
    assert _listed_models("01-ai/yi-large", "nvidia/outro-modelo") == [
        ("01-ai/yi-large", False), ("nvidia/outro-modelo", False)]


@pytest.mark.parametrize("message,kind", [
    ("API retornou HTTP 400. Confira chave, modelo, saldo e endpoint.", ErrorKind.CONFIGURATION),
    ("API retornou HTTP 401. Confira chave, modelo, saldo e endpoint.", ErrorKind.INVALID_CREDENTIALS),
    ("API retornou HTTP 402. Confira chave, modelo, saldo e endpoint.", ErrorKind.QUOTA_EXCEEDED),
    ("API retornou HTTP 403. Confira chave, modelo, saldo e endpoint.", ErrorKind.INVALID_CREDENTIALS),
    ("API retornou HTTP 404. Confira chave, modelo, saldo e endpoint.", ErrorKind.MODEL_UNAVAILABLE),
    ("API retornou HTTP 429. Confira chave, modelo, saldo e endpoint.", ErrorKind.RATE_LIMITED),
    ("API retornou HTTP 500. Confira chave, modelo, saldo e endpoint.", ErrorKind.PROVIDER_UNAVAILABLE),
    ("Timeout ao chamar a API do modelo.", ErrorKind.TIMEOUT),
    ("Falha de conexão com a API do modelo.", ErrorKind.PROVIDER_UNAVAILABLE),
    ("Resposta da API não é JSON válido.", ErrorKind.PROVIDER_UNAVAILABLE),
])
def test_transport_failures_become_typed_brain_errors(message: str, kind: ErrorKind) -> None:
    """O tipo do erro é o que o teste de modelo e a tarefa usam; o texto bruto fica no detalhe."""
    translated = translate_provider_error(ProviderError(message))
    assert translated.kind is kind
    assert translated.message
    assert translated.detail == message


def test_inference_failure_never_echoes_the_response_body() -> None:
    """Uma chave recusada na inferência tem de virar erro tipado, sem ecos do corpo."""
    brain = ApiBrain(Settings(provider="nvidia", model="modelo", api_key="segredo"))
    brain._provider.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(401, text="segredo ecoado pelo provedor")))
    try:
        with pytest.raises(BrainError) as failure:
            asyncio.run(brain.complete([{"role": "user", "content": "oi"}]))
    finally:
        asyncio.run(brain.close())
    assert failure.value.kind is ErrorKind.INVALID_CREDENTIALS
    assert "segredo" not in str(failure.value)


def test_providers_without_a_curated_default_mark_nothing() -> None:
    assert _listed_models("gpt-6.1-sol", "gpt-6.1-air", provider="openai", key="sk-test") == [
        ("gpt-6.1-sol", False), ("gpt-6.1-air", False)]
    assert "openai" not in PROVIDER_PREFERRED_MODELS
