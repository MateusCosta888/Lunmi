from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
# Recursos do produto (código, `vendor/UFO`, ícones): somente leitura no pacote. Dados
# graváveis do utilizador ficam à parte em USER_DATA_ROOT.
UFO_REVISION = "a795552d976c4c019d7c2f778a0effb5cef7de6b"


def _user_data_root() -> Path:
    """Raiz de dados graváveis, separada dos recursos somente-leitura do produto.

    `LUNMI_USER_DATA` é definida pelo processo principal do Electron no pacote (pasta por
    utilizador em `LOCALAPPDATA`); sem ela — código-fonte, `run.bat`, testes — a raiz é o
    próprio checkout, preservando o comportamento histórico de `.local/`, `logs/` e `output/`.
    Uma instalação nunca grava dentro de `resources/`, então o pacote sempre define a variável.

    Resolvida em tempo de chamada para que `ROOT` continue sendo o ponto único de redireção
    em testes e sessões de aceite que o reescrevem.
    """
    override = os.getenv("LUNMI_USER_DATA", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return ROOT


# Valor resolvido no import (o pacote define a variável antes de o Python arrancar).
USER_DATA_ROOT = _user_data_root()


def _local_dir() -> Path:
    """Pasta `.local` do escopo atual: estado pessoal do utilizador, fora dos recursos."""
    return _user_data_root() / ".local"

# Providers que exigem chave de API própria.
API_KEY_PROVIDERS = frozenset({"openai", "anthropic", "gemini", "openrouter", "nvidia", "groq"})
# Endpoints compatíveis com OpenAI que dispensam chave ("custom" exige URL base).
LOCAL_ENDPOINT_PROVIDERS = frozenset({"custom", "ollama", "lmstudio", "vllm"})
# Runtimes de CLI locais: autenticação e sessão pertencem ao próprio CLI.
CLI_PROVIDERS = frozenset({"codex", "claude", "agy", "qoder", "kilo", "kiro", "opencode",
                          "freepi"})
# Todos os ids aceitos em Settings.provider.
SUPPORTED_PROVIDERS = API_KEY_PROVIDERS | LOCAL_ENDPOINT_PROVIDERS | CLI_PROVIDERS


class ConfigurationError(ValueError):
    pass


def _validate_base_url(base_url: str) -> None:
    """HTTPS obrigatório fora de loopback; sem credenciais/query/fragmento."""
    parts = urlsplit(base_url)
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigurationError("Use uma URL base válida, sem credenciais/query/fragmento.")
    if parts.scheme != "https" and not (
        parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ConfigurationError("HTTP permitido somente em loopback; fora dele use HTTPS.")


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _protect(data: bytes, *, decrypt: bool = False) -> bytes:
    """Windows DPAPI, bound to the current Windows user; no UI prompts."""
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ConfigurationError("Não foi possível proteger/ler a chave com Windows DPAPI.")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


@dataclass(frozen=True)
class Settings:
    provider: str = "openai"
    model: str = ""
    # Latency plan P2: a fast brain for the AppAgent (local execution), while the
    # HostAgent keeps reasoning on the main brain. Empty means "same brain for both
    # roles" — the current behaviour. Configured via LLM_APP_PROVIDER/LLM_APP_MODEL.
    app_provider: str = ""
    app_model: str = ""
    # Explicit effort only at documented destinations; empty preserves the model default.
    effort: str = ""
    api_key: str = field(default="", repr=False)
    base_url: str = ""
    max_steps: int = 20
    timeout_seconds: float = 300
    verify_results: bool = True
    # FASE 8: resolve desktop commands whose result a table can prove before asking a
    # model to plan them, and boot the UFO worker while the user is still typing. Both
    # are latency levers, not capabilities: turning them off restores the agent path.
    fast_path: bool = True
    prewarm: bool = True
    # "Autoaprovação de ações": manual by default, the user's choice from then on. It
    # travels with every task, so toggling it in the chat changes the next action without
    # restarting anything. It removes the questions the runtime would have asked because it
    # could not *tell* whether an action was safe; it never removes the ones about actions
    # that are provably sensitive (see app/computer/approval.py).
    auto_approve: bool = False
    # F-AL2/F-AL6: the machine's application index answers which program a name means.
    # `app_search` allows the one bounded search on the request path when the index does
    # not carry the name yet; `app_folders` are places the user keeps portable programs,
    # discovered and remembered like any other source.
    app_index: bool = True
    app_search: bool = True
    app_folders: tuple[str, ...] = ()
    collect_metrics: bool = True
    log_level: str = "INFO"
    log_instruction: bool = False
    ufo_path: Path = ROOT / "vendor" / "UFO"
    logs_path: Path = field(default_factory=lambda: _user_data_root() / "logs")
    output_path: Path = field(default_factory=lambda: _user_data_root() / "output")
    # Where task-time machine state lives (`<state_root>/.local`, today the application
    # index the launcher learns from). A sandboxed bridge points it inside the sandbox, so
    # a test or acceptance session never reads or rewrites the user's own index.
    state_root: Path = field(default_factory=_user_data_root)

    def validate(self, *, credentials: bool = True, model: bool = True) -> None:
        """Check one configuration. ``model=False`` drops only the model requirement.

        Listing a provider's catalogue needs a transport, not a model, so the model
        check is separable from the credential checks it sits between.
        """
        if os.name != "nt":
            raise ConfigurationError("Este aplicativo requer Windows 10/11.")
        if self.provider not in SUPPORTED_PROVIDERS:
            raise ConfigurationError("Provider deve ser um de: " + ", ".join(sorted(SUPPORTED_PROVIDERS)) + ".")
        if not 1 <= self.max_steps <= 100 or not 1 <= self.timeout_seconds <= 3600:
            raise ConfigurationError("Limites inválidos de passos ou timeout.")
        if credentials and model and not self.model.strip():
            raise ConfigurationError("Escolha um modelo no painel Brain.")
        if credentials and self.provider == "codex" and self.api_key:
            raise ConfigurationError("Codex usa login oficial da conta ChatGPT; não aceita chave API neste provider.")
        if credentials and self.provider in API_KEY_PROVIDERS and not self.api_key:
            raise ConfigurationError(
                f"O cérebro {self.provider} não tem chave de API. Adiciona-a no painel Brain.")
        if credentials and self.provider in CLI_PROVIDERS and self.provider != "codex" and self.api_key:
            raise ConfigurationError("Este provider usa a autenticação do próprio CLI; não aceita chave API.")
        if self.app_provider and self.app_provider not in SUPPORTED_PROVIDERS:
            raise ConfigurationError("Provider do AppAgent deve ser um de: "
                                     + ", ".join(sorted(SUPPORTED_PROVIDERS)) + ".")
        if self.app_provider and self.app_provider != self.provider and not self.app_model.strip():
            raise ConfigurationError("Escolha o modelo do AppAgent ao usar outro provider.")
        if self.provider == "codex" and self.base_url:
            raise ConfigurationError("Codex usa o app-server oficial; URL base não se aplica.")
        if self.provider in CLI_PROVIDERS and self.provider != "codex" and self.base_url:
            raise ConfigurationError("Este provider usa o CLI oficial; URL base não se aplica.")
        if self.provider == "custom":
            _validate_base_url(self.base_url)
        elif self.provider in {"ollama", "lmstudio", "vllm", "nvidia"} and self.base_url:
            # Estes aceitam um endpoint próprio no lugar do preset (um NIM local, por
            # exemplo), e o override passa pela mesma validação do provider `custom`.
            _validate_base_url(self.base_url)

    @classmethod
    def load(cls) -> Settings:
        load_dotenv(_user_data_root() / ".env", override=False)
        if _user_data_root() != ROOT:
            # Uma instalação pode ter um `.env` próprio por utilizador; o do checkout é a
            # base de desenvolvimento e não deve desaparecer do caminho.
            load_dotenv(ROOT / ".env", override=False)
        values: dict[str, str] = {}
        saved = _local_dir() / "provider.dpapi"
        if saved.exists():
            try:
                values = json.loads(_protect(saved.read_bytes(), decrypt=True))
            except (OSError, ValueError) as exc:
                raise ConfigurationError("Configuração salva inválida; reconfigure a API.") from exc
        approval = _load_approval()
        selection: dict[str, str] = {}
        selected = _local_dir() / "brain-selection.json"
        if selected.exists():
            try:
                selection = json.loads(selected.read_text(encoding="utf-8"))
                if not isinstance(selection, dict) or set(selection) != {"provider", "model"} or any(
                        not isinstance(value, str) for value in selection.values()):
                    raise ValueError("invalid selection")
                _validate_selection(selection["provider"], selection["model"])
            except (OSError, ValueError) as exc:
                raise ConfigurationError("A seleção salva é inválida; reconfigure o cérebro.") from exc
        # Latency plan P2 / FASE 3.1: the optional AppAgent brain is remembered the same
        # way as the main one — provider+model only, never a secret. An empty file means
        # "reuse the main brain".
        app_selection: dict[str, str] = {}
        app_selected = _local_dir() / "app-brain.json"
        if app_selected.exists():
            try:
                app_selection = json.loads(app_selected.read_text(encoding="utf-8"))
                if not isinstance(app_selection, dict) or set(app_selection) != {"provider", "model"} \
                        or any(not isinstance(value, str) for value in app_selection.values()):
                    raise ValueError("invalid app selection")
                if app_selection["provider"] and app_selection["provider"] not in SUPPORTED_PROVIDERS:
                    raise ValueError("invalid app provider")
            except (OSError, ValueError) as exc:
                raise ConfigurationError("A seleção do Brain do AppAgent é inválida; reconfigure-a.") from exc
        # Explicit environment/.env overrides the UI-saved settings.
        provider = os.getenv("LLM_PROVIDER", selection.get("provider", values.get("provider", "openai"))).lower()
        app_provider = (os.getenv("LLM_APP_PROVIDER", "").strip().lower()
                        or str(app_selection.get("provider") or "").strip().lower())
        app_model = (os.getenv("LLM_APP_MODEL", "").strip()
                     or str(app_selection.get("model") or "").strip())
        key_name = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                    "gemini": "GEMINI_API_KEY", "openrouter": "OPENROUTER_API_KEY",
                    "nvidia": "NVIDIA_API_KEY", "groq": "GROQ_API_KEY"}.get(provider, "LLM_API_KEY")
        try:
            return cls(
                provider=provider,
                model=os.getenv("LLM_MODEL", selection.get("model", values.get("model", ""))),
                app_provider=app_provider,
                app_model=app_model,
                effort=os.getenv("BRAIN_EFFORT", "").strip(),
                api_key="" if provider in CLI_PROVIDERS else os.getenv(
                    key_name, values.get("api_key", "") if values.get("provider") == provider else ""),
                base_url="" if provider in CLI_PROVIDERS else os.getenv(
                    "LLM_BASE_URL", values.get("base_url", "") if values.get("provider") == provider else ""),
                max_steps=int(os.getenv("MAX_STEPS", "20")),
                timeout_seconds=float(os.getenv("TASK_TIMEOUT_SECONDS", "300")),
                verify_results=os.getenv("VERIFY_RESULTS", "true").lower() == "true",
                fast_path=os.getenv("FAST_PATH", "true").lower() == "true",
                prewarm=os.getenv("PREWARM_DESKTOP", "true").lower() == "true",
                auto_approve=_env_flag("AUTO_APPROVE", approval),
                app_index=os.getenv("APP_INDEX", "true").lower() == "true",
                app_search=os.getenv("APP_SEARCH", "true").lower() == "true",
                app_folders=tuple(item for item in
                                  (piece.strip() for piece in os.getenv("APP_FOLDERS", "")
                                   .split(os.pathsep)) if item),
                collect_metrics=os.getenv("COLLECT_METRICS", "true").lower() == "true",
                log_level=os.getenv("LOG_LEVEL", "INFO"),
                log_instruction=os.getenv("LOG_INSTRUCTIONS", "false").lower() == "true",
                ufo_path=Path(os.getenv("UFO_PATH", str(ROOT / "vendor" / "UFO"))).resolve(),
                output_path=Path(os.getenv("OUTPUT_DIRECTORY", str(_user_data_root() / "output"))).resolve(),
            )
        except ValueError as exc:
            raise ConfigurationError("Valores numéricos inválidos no .env.") from exc


def _env_flag(name: str, default: bool) -> bool:
    """An explicit environment flag wins; anything else leaves the saved preference."""
    raw = os.getenv(name, "").strip().lower()
    if raw in {"true", "1", "yes"}:
        return True
    if raw in {"false", "0", "no"}:
        return False
    return default


def _approval_path() -> Path:
    return _local_dir() / "approval.json"


def _load_approval() -> bool:
    """The saved mode, read as a boolean this application wrote itself.

    Anything else in the file (a hand-edited value, a truncated write) means the default,
    which is manual: a broken preference must never widen what runs without asking.
    """
    path = _approval_path()
    if not path.exists():
        return False
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return saved.get("auto_approve") is True if isinstance(saved, dict) else False


def save_auto_approve(enabled: bool) -> None:
    """Persist the user's mode immediately; the toggle is not a form to submit."""
    path = _approval_path()
    path.parent.mkdir(exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"auto_approve": bool(enabled)}, ensure_ascii=False),
                         encoding="utf-8")
    temporary.replace(path)


def _validate_selection(provider: str, model: str) -> None:
    if provider not in SUPPORTED_PROVIDERS or not isinstance(model, str) or not model.strip():
        raise ConfigurationError("Escolha um cérebro e modelo válidos.")


def selection_path() -> Path:
    """Where the nonsecret main selection is remembered (read back after apply)."""
    return _local_dir() / "brain-selection.json"


def save_selection(provider: str, model: str) -> None:
    """Remember only nonsecret provider/model; never change DPAPI credentials."""
    _validate_selection(provider, model)
    path = selection_path()
    path.parent.mkdir(exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"provider": provider, "model": model.strip()}, ensure_ascii=False),
                         encoding="utf-8")
    temporary.replace(path)


def app_selection_path() -> Path:
    return _local_dir() / "app-brain.json"


def save_app_selection(provider: str, model: str) -> None:
    """Remember declared AppAgent fields; only both empty means full inheritance."""
    path = app_selection_path()
    provider = str(provider or "").strip().lower()
    model = str(model or "").strip()
    if not provider and not model:
        if path.exists():
            path.unlink()
        return
    if provider and provider not in SUPPORTED_PROVIDERS:
        raise ConfigurationError("Escolha um cérebro válido para o AppAgent.")
    path.parent.mkdir(exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"provider": provider, "model": model}, ensure_ascii=False),
                         encoding="utf-8")
    temporary.replace(path)


def save_provider(provider: str, model: str, api_key: str, base_url: str) -> None:
    if provider in CLI_PROVIDERS:
        settings = Settings(provider=provider, model=model.strip())
        settings.validate()
        path = _local_dir() / "provider.dpapi"
        path.parent.mkdir(exist_ok=True)
        temporary = path.with_suffix(".tmp")
        payload = json.dumps({"provider": provider, "model": settings.model, "api_key": "", "base_url": ""}).encode()
        temporary.write_bytes(_protect(payload))
        temporary.replace(path)
        return
    settings = Settings(provider=provider, model=model.strip(), api_key=api_key.strip(), base_url=base_url.strip())
    settings.validate()
    path = _local_dir() / "provider.dpapi"
    path.parent.mkdir(exist_ok=True)
    temporary = path.with_suffix(".tmp")
    payload = json.dumps({"provider": settings.provider, "model": settings.model,
                          "api_key": settings.api_key, "base_url": settings.base_url}).encode()
    temporary.write_bytes(_protect(payload))
    temporary.replace(path)
