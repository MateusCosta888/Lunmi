"""Estado público e preferências da Pesquisa Web (V1 + origem V1).

O runtime pinado vive fora do processo principal:

    <state_root>/.local/web-search/runtime/Scripts/python.exe

`state_root` vem de `Settings` (sandbox incluído). `LUNMI_WEB_HOME` é um override absoluto
apenas para o processo/backend confiável de QA; nada aqui aceita caminhos do renderer.
Este módulo nunca cria o runtime, nunca o importa e nunca abre rede.

A partir dos contratos `.agents/WEB-SEARCH-ORIGIN-V1-CONTRACT.md` e
`.agents/WEB-SEARCH-AUTO-V1-CONTRACT.md`, o mesmo ficheiro `web-search.json` guarda
`enabled`/`mode` (V1) e um mapa `brain_origins` por fingerprint estável do Brain, cujo
valor é a **preferência** (`auto|lunmi|provider|none`; `auto` é o default novo). O manager
(`app/integrations/web_search.py`, Root E) continua a ser o dono do lifecycle; este módulo
é a fonte canónica dos campos públicos e do plano de origem da Task.
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.brain.web import (
    DEFAULT_WEB_PREFERENCE,
    WEB_PREFERENCES,
    BrainIdentity,
    NativeWebCapability,
    WebFallbackReason,
    WebOrigin,
    WebOriginSnapshot,
    WebPreference,
    can_complete_web,
    plan_web_attempts,
)

WEB_HOME_ENV = "LUNMI_WEB_HOME"
WEB_DIRNAME = "web-search"
RUNTIME_RELATIVE = ("runtime", "Scripts", "python.exe")
WORKER_FILENAME = "web_worker.py"
PREFERENCES_FILENAME = "web-search.json"

MODES = ("auto", "quick", "deep")
DEFAULT_MODE = "auto"
# Nome histórico do default: é uma PREFERÊNCIA, não uma origem efetiva.
DEFAULT_ORIGIN = DEFAULT_WEB_PREFERENCE
ORIGINS = WEB_PREFERENCES
MAX_BRAIN_ORIGINS = 64
BRAIN_ID_PATTERN = re.compile(r"^brain-[0-9a-f]{16}$")

REASON_RUNTIME_MISSING = "runtime_missing"
REASON_WORKER_MISSING = "worker_missing"
REASON_OUTSIDE_HOME = "runtime_outside_home"

MESSAGES = {
    "unavailable": "Pesquisa web indisponível: runtime não instalado.",
    "disabled": "Pesquisa web desligada.",
    "idle": "Pesquisa web pronta.",
    "starting": "A preparar a pesquisa web…",
    "searching": "A pesquisar na web…",
    "reading": "A ler as fontes…",
    "error": "A pesquisa web falhou. Tente novamente.",
    "no_results": "Não encontrei fontes públicas para essa pesquisa.",
    "timeout": "A pesquisa demorou demasiado; nenhuma resposta foi concluída.",
    "blocked": "Esse endereço não é público ou não é permitido nesta versão.",
    "failed": "Não consegui concluir a pesquisa web.",
    "rate_limited": ("A pesquisa foi feita, mas o cérebro atingiu um limite de uso. O Groq gratuito "
                     "limita tokens por minuto e por dia; tenta de novo mais tarde."),
    "not_verified": (
        "Não foi possível confirmar a resposta com as fontes desta consulta; "
        "o texto apresentado não está verificado."
    ),
    "limited": "Não foi possível ler as páginas; a resposta está limitada a excertos de pesquisa.",
    "native_unavailable": "Pesquisa nativa indisponível; a usar Free Search.",
    "provider_unavailable": "Pesquisa nativa indisponível; nada foi consultado.",
}


class WebPreferencesError(ValueError):
    """Preferência inválida (payload) ou não gravável; nunca inclui caminho no texto público."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WebEnvironment:
    home: Path
    python: Path
    worker: Path
    reason: str = ""

    @property
    def available(self) -> bool:
        return not self.reason

    @property
    def message(self) -> str:
        if not self.reason:
            return ""
        return MESSAGES["unavailable"]


def web_home(state_root: str | Path) -> Path:
    """Raiz do runtime web: override absoluto confiável ou `<state_root>/.local/web-search`."""
    override = os.environ.get(WEB_HOME_ENV, "").strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
    return (Path(state_root) / ".local" / WEB_DIRNAME).resolve()


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def resolve_environment(
    state_root: str | Path,
    *,
    worker_path: str | Path | None = None,
    runtime_python: str | Path | None = None,
) -> WebEnvironment:
    """Resolve caminhos e diz honestamente se os artefactos existem. Nunca cria nada."""
    home = web_home(state_root)
    python = Path(runtime_python).expanduser().resolve() if runtime_python is not None else (
        home.joinpath(*RUNTIME_RELATIVE).resolve())
    worker = Path(worker_path).expanduser().resolve() if worker_path is not None else (
        Path(__file__).resolve().with_name(WORKER_FILENAME))

    reason = ""
    if not _within(python, home):
        reason = REASON_OUTSIDE_HOME
    elif not python.is_file():
        reason = REASON_RUNTIME_MISSING
    elif not worker.is_file():
        reason = REASON_WORKER_MISSING
    return WebEnvironment(home=home, python=python, worker=worker, reason=reason)


@dataclass(frozen=True)
class WebPreferences:
    """Preferências persistentes; `origin_update` é um delta de `web.set`.

    O manager V1 continua a construir `current` apenas com `enabled`/`mode`, por isso
    `brain_origins` vazio significa "não substituir o mapa existente". Uma leitura completa
    (`load_preferences`) traz o mapa preenchido; um `web.set {brain_id, origin}` traz
    apenas `origin_update`. `save_preferences` e `WebState.set_preferences` sabem distinguir.

    `brain_origins` é o nome histórico do mapa; o valor guardado é uma **preferência**
    (`auto|lunmi|provider|none`), nunca uma origem efetiva.
    """

    enabled: bool = True
    mode: str = DEFAULT_MODE
    brain_origins: dict[str, str] = field(default_factory=dict)
    origin_update: tuple[str, str] | None = None

    def dto(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "mode": self.mode}


def preferences_path(state_root: str | Path) -> Path:
    return Path(state_root) / ".local" / PREFERENCES_FILENAME


def _valid_brain_origins(raw: Any) -> dict[str, str]:
    """Mapa tolerante: ignora entradas inválidas, nunca alarga uma preferência."""
    if not isinstance(raw, dict):
        return {}
    origins: dict[str, str] = {}
    for brain_id, origin in raw.items():
        if len(origins) >= MAX_BRAIN_ORIGINS:
            break
        if (isinstance(brain_id, str) and BRAIN_ID_PATTERN.fullmatch(brain_id)
                and isinstance(origin, str) and origin in WEB_PREFERENCES):
            origins[brain_id] = origin
    return origins


def _load_raw(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_preferences(state_root: str | Path) -> WebPreferences:
    """Ficheiro inválido/truncado devolve defaults seguros; nunca rebenta o arranque.

    Migração explícita: um ficheiro antigo com apenas `enabled`/`mode`, ou sem entrada para
    o Brain atual, fica em `auto` (o default novo). Entradas já gravadas são escolhas
    explícitas e são preservadas tal como estão. Campos desconhecidos são ignorados
    (leitura tolerante); `web.set` continua fechado.
    """
    raw = _load_raw(preferences_path(state_root))
    enabled = raw.get("enabled", True)
    mode = raw.get("mode", DEFAULT_MODE)
    if type(enabled) is not bool or mode not in MODES:
        return WebPreferences()
    return WebPreferences(enabled=enabled, mode=mode,
                          brain_origins=_valid_brain_origins(raw.get("brain_origins")))


def save_preferences(state_root: str | Path, preferences: WebPreferences) -> None:
    """Escrita atómica; preserva o mapa existente quando o payload é só enabled/mode."""
    path = preferences_path(state_root)
    existing = _valid_brain_origins(_load_raw(path).get("brain_origins"))
    origins = dict(existing)
    if preferences.brain_origins:
        origins = dict(preferences.brain_origins)
    if preferences.origin_update is not None:
        brain_id, origin = preferences.origin_update
        origins[brain_id] = origin
    if len(origins) > MAX_BRAIN_ORIGINS:
        # Recusa o 65.º Brain ANTES da escrita: as 64 preferências existentes ficam
        # intactas no disco e na RAM. Nada é despejado nem apagado.
        raise WebPreferencesError("limit")
    data: dict[str, Any] = {
        "enabled": preferences.enabled,
        "mode": preferences.mode,
    }
    if origins:
        data["brain_origins"] = origins
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        raise WebPreferencesError("io_error") from None


def _validated_origin_update(payload: dict[str, Any]) -> tuple[str, str] | None:
    has_brain = "brain_id" in payload
    has_origin = "origin" in payload
    if has_brain != has_origin:
        raise WebPreferencesError("invalid_payload")
    if not has_origin:
        return None
    brain_id = payload.get("brain_id")
    origin = payload.get("origin")
    if (not isinstance(brain_id, str) or not BRAIN_ID_PATTERN.fullmatch(brain_id)
            or not isinstance(origin, str) or origin not in WEB_PREFERENCES):
        raise WebPreferencesError("invalid_payload")
    return brain_id, origin


def apply_preferences(current: WebPreferences, payload: Any) -> WebPreferences:
    """Payload fechado: enabled/mode globais e/ou brain_id+origin por Brain.

    O manager V1 chama isto sem conhecer o mapa; por isso devolvemos `origin_update` em
    vez de tentar reconstruir o mapa completo. Uma atualização de `enabled`/`mode` sem
    origem não apaga escolhas por Brain.
    """
    if not isinstance(payload, dict) or not payload:
        raise WebPreferencesError("invalid_payload")
    if set(payload) - {"enabled", "mode", "brain_id", "origin"}:
        raise WebPreferencesError("invalid_payload")
    enabled = payload.get("enabled", current.enabled)
    mode = payload.get("mode", current.mode)
    if type(enabled) is not bool or mode not in MODES:
        raise WebPreferencesError("invalid_payload")
    update = _validated_origin_update(payload)
    return WebPreferences(enabled=enabled, mode=mode, origin_update=update)


def identity_for_settings(settings: Any, *, role: str = "host") -> BrainIdentity:
    """Identidade não secreta usada na chave de `brain_origins`."""
    return BrainIdentity(
        provider=str(getattr(settings, "provider", "") or ""),
        model=str(getattr(settings, "model", "") or ""),
        endpoint=str(getattr(settings, "base_url", "") or ""),
        role=role,
    )


class WebState:
    """Snapshot público da mesma sessão; revisão monotónica e protegida entre threads.

    O manager V1 possui esta instância. A bridge liga-lhe o Brain atual (fingerprint +
    capacidade) antes de `web.get`/`web.set`; a origem da Task é lida daqui **uma vez**.
    """

    def __init__(self, *, session_id: str) -> None:
        self.session_id = session_id
        self._lock = threading.Lock()
        self._revision = 0
        self._enabled = True
        self._mode = DEFAULT_MODE
        self._state = "idle"
        self._message = MESSAGES["idle"]
        self._brain_origins: dict[str, str] = {}
        self._brain_id = ""
        self._native_web = NativeWebCapability.unknown()

    # ------------------------------------------------------------- preferências --
    def set_preferences(self, preferences: WebPreferences) -> None:
        with self._lock:
            self._enabled = preferences.enabled
            self._mode = preferences.mode
            if preferences.brain_origins:
                self._brain_origins = dict(preferences.brain_origins)
            if preferences.origin_update is not None:
                brain_id, origin = preferences.origin_update
                if (brain_id in self._brain_origins
                        or len(self._brain_origins) < MAX_BRAIN_ORIGINS):
                    self._brain_origins[brain_id] = origin

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    @property
    def brain_id(self) -> str:
        with self._lock:
            return self._brain_id

    @property
    def native_web(self) -> NativeWebCapability:
        with self._lock:
            return self._native_web

    @property
    def preference(self) -> WebPreference:
        with self._lock:
            return self._preference_locked()

    @property
    def brain_origins(self) -> dict[str, str]:
        with self._lock:
            return dict(self._brain_origins)

    def _preference_locked(self) -> WebPreference:
        value = self._brain_origins.get(self._brain_id, DEFAULT_ORIGIN)
        return value if value in WEB_PREFERENCES else DEFAULT_WEB_PREFERENCE  # type: ignore[return-value]

    def preference_for(self, brain_id: str) -> WebPreference:
        with self._lock:
            value = self._brain_origins.get(brain_id, DEFAULT_ORIGIN)
            return value if value in WEB_PREFERENCES else DEFAULT_WEB_PREFERENCE  # type: ignore[return-value]

    # ----------------------------------------------------------------- Brain ----
    def bind_brain(self, brain_id: str, native_web: NativeWebCapability, *,
                   available: bool) -> dict[str, Any]:
        """Liga o Brain atual; uma mudança real incrementa a revisão canónica."""
        with self._lock:
            changed = (brain_id != self._brain_id
                       or native_web != self._native_web)
            self._brain_id = brain_id
            self._native_web = native_web
            if changed:
                self._revision += 1
            return self._snapshot_locked(available=available)

    # ------------------------------------------------------------------ origem --
    def _provider_ready_locked(self) -> bool:
        """Só uma capacidade `selectable` provada autoriza a tentativa nativa."""
        return bool(self._native_web.selectable)

    def _plan_locked(self, available: bool) -> tuple[WebOrigin, ...]:
        if not self._enabled:
            # OFF não tem tentativas: sem isto, uma preferência `auto` com capacidade
            # nativa provada publicaria `origin=provider` num estado desligado.
            return ()
        return plan_web_attempts(
            self._preference_locked(),
            provider_ready=self._provider_ready_locked(),
            lunmi_available=available,
        )

    def _origin_locked(self, available: bool) -> WebOrigin:
        attempts = self._plan_locked(available)
        return attempts[0] if attempts else "none"

    def _fallback_locked(self, available: bool) -> WebFallbackReason:
        if not self._enabled:
            return ""
        preference = self._preference_locked()
        origin = self._origin_locked(available)
        if preference == "provider" and origin != "provider":
            return "provider_unavailable"
        if preference == "lunmi" and origin != "lunmi":
            return "lunmi_unavailable"
        return ""

    def origin(self, *, available: bool) -> WebOrigin:
        with self._lock:
            return self._origin_locked(available)

    def public_origin_fields(self, *, available: bool) -> dict[str, Any]:
        """Campos de origem para enriquecer um snapshot emitido pelo manager V1."""
        with self._lock:
            return {
                "brain_id": self._brain_id,
                "preference": self._preference_locked(),
                "origin": self._origin_locked(available),
                "native_web": self._native_web.dto(),
                "web_fallback": self._fallback_locked(available),
            }

    # ------------------------------------------------------------------ estado --
    def _snapshot_locked(self, *, available: bool) -> dict[str, Any]:
        state = self._state
        message = self._message
        if not available:
            state, message = "idle", MESSAGES["unavailable"]
        elif not self._enabled:
            state, message = "idle", MESSAGES["disabled"]
        return {
            "session_id": self.session_id,
            "revision": self._revision,
            "enabled": self._enabled,
            "mode": self._mode,
            "available": available,
            "state": state,
            "message": message,
            "brain_id": self._brain_id,
            "preference": self._preference_locked(),
            "origin": self._origin_locked(available),
            "native_web": self._native_web.dto(),
            "web_fallback": self._fallback_locked(available),
        }

    def bump(self, *, state: str | None = None, message: str | None = None,
             available: bool = True) -> dict[str, Any]:
        with self._lock:
            if state is not None:
                self._state = state
            if message is not None:
                self._message = message
            self._revision += 1
            return self._snapshot_locked(available=available)

    def dto(self, *, available: bool) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked(available=available)

    def clear_runtime_after_error(self) -> None:
        """Hook pequeno para o manager V1; o estado de erro continua visível."""
        with self._lock:
            if self._state in {"starting", "searching", "reading"}:
                self._state = "error"
                self._message = MESSAGES["error"]


def resolve_task_origin(web: Any, *, brain: Any = None, forbidden: bool = False
                        ) -> WebOriginSnapshot:
    """Congela a decisão no início da Task; nunca inicia runtime nem pesquisa paga.

    A preferência pode ter vindo de `WebState` (manager V1) ou de um fake de teste sem
    estado. Neste último caso, mantém-se o comportamento histórico (Lunmi quando
    disponível). `forbidden` (recusa explícita do utilizador) força `none` e nenhuma
    capacidade nativa.

    Preferências explícitas não alternam: `provider` sem capacidade provada devolve
    `attempts=()` e `fallback="provider_unavailable"`, sem consultar a Lunmi. Apenas `auto`
    produz o plano de duas tentativas (provedor e depois Free Search).
    """
    state = getattr(web, "_state", None)
    if isinstance(state, WebState):
        preference = state.preference
        native = state.native_web
        enabled = state.enabled
    else:
        preference = DEFAULT_ORIGIN
        native = NativeWebCapability.unknown()
        enabled = bool(getattr(web, "enabled", False))
    lunmi_available = bool(getattr(web, "available", False))

    if forbidden:
        return WebOriginSnapshot(
            preference=preference, origin="none", fallback="forbidden",
            native_web=NativeWebCapability.unknown(), lunmi_available=lunmi_available,
        )
    provider_ready = bool(native.selectable and (brain is None or can_complete_web(brain)))
    attempts = (plan_web_attempts(preference, provider_ready=provider_ready,
                                  lunmi_available=bool(enabled and lunmi_available))
                if enabled else ())
    origin: WebOrigin = attempts[0] if attempts else "none"
    fallback: WebFallbackReason = ""
    if enabled and preference == "provider" and origin != "provider":
        fallback = "provider_unavailable"
    elif enabled and preference == "lunmi" and origin != "lunmi":
        fallback = "lunmi_unavailable"
    return WebOriginSnapshot(
        preference=preference, origin=origin, attempts=attempts,
        recovery=len(attempts) == 2, fallback=fallback,
        native_web=native, lunmi_available=lunmi_available,
    )
