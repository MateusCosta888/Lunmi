"""Tipos puros da pesquisa web nativa, sem I/O e sem dependências de integrações.

Este ficheiro é a superfície acordada nos contratos
`.agents/WEB-SEARCH-ORIGIN-V1-CONTRACT.md` e `.agents/WEB-SEARCH-AUTO-V1-CONTRACT.md`.
O adaptador Kiro (outro Designer) implementa `native_web_capability()`, `complete_web()` e,
quando mantém processo vivo depois de devolver, `cancel_web()` no mesmo objeto `Brain`; o
controller e a bridge só dependem dos tipos daqui. Não importar `app.integrations`,
`Settings` ou rede: o módulo tem de continuar barato e testável offline.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

# Vocabulário público da PREFERÊNCIA (o que o utilizador escolheu). `auto` não é uma
# origem: é o plano fechado "provedor primeiro, Free Search só em recuperação". `none` é
# OFF explícito. Uma escolha `auto` nunca é gravada como origem efetiva.
WebPreference = Literal["auto", "lunmi", "provider", "none"]
WEB_PREFERENCES: tuple[WebPreference, ...] = ("auto", "lunmi", "provider", "none")
DEFAULT_WEB_PREFERENCE: WebPreference = "auto"

# Vocabulário público da ORIGEM EFETIVA de uma tentativa. `none` não é uma preferência:
# é a ausência de tentativa executável.
WebOrigin = Literal["none", "lunmi", "provider"]
WEB_ORIGINS: tuple[WebOrigin, ...] = ("none", "lunmi", "provider")

WebEvidenceStatus = Literal["suff", "partial", "insufficient"]
WebRequestStatus = Literal["met", "partial", "unmet", "unknown"]
WebFallbackReason = Literal[
    "",
    "forbidden",
    "provider_unavailable",
    "provider_recovered",
    "cleanup_failed",
    "lunmi_unavailable",
]

# Classe fechada da falha da tentativa nativa. `security` cobre URL privada, DNS/SSRF,
# hook/política e perfil OFF; `permission` é a ferramenta recusada pelo CLI. Uma recusa de
# segurança NUNCA é uma permissão web recuperável.
WebProviderFailure = Literal[
    "",
    "cancelled",
    "security",
    "permission",
    "unavailable",
    "timeout",
    "malformed",
    "failed",
    "exhausted",
    "unknown",
]
WEB_PROVIDER_FAILURES: tuple[WebProviderFailure, ...] = (
    "", "cancelled", "security", "permission", "unavailable", "timeout", "malformed",
    "failed", "exhausted", "unknown",
)
# Só estas falhas, e só em `auto`, autorizam a recuperação Free Search. `security` e
# `cancelled` nunca recuperam; `""` e `unknown` também não — ausência de prova não
# autoriza. `exhausted` é a única razão fechada para «as ferramentas web do provedor
# correram e não deixaram evidência utilizável» (observação positiva do adaptador, não a
# simples ausência de fontes estruturadas).
RECOVERABLE_PROVIDER_FAILURES = frozenset({
    "permission", "unavailable", "timeout", "malformed", "failed", "exhausted",
})
# Gate de prazo útil e reserva da tentativa nativa, não timeouts novos: a recuperação
# corre dentro do MESMO deadline da Task. O provedor só começa quando deixa
# `WEB_RECOVERY_RESERVE_S` de margem e a sua tentativa é limitada por
# `deadline_remaining() - WEB_RECOVERY_RESERVE_S`; a recuperação só começa com
# `WEB_RECOVERY_MIN_REMAINING_S`. Nenhum limite global é aumentado.
WEB_RECOVERY_MIN_REMAINING_S = 3.0
WEB_RECOVERY_RESERVE_S = 4.0


class NativeWebState(str, Enum):
    """Estado honesto da capacidade nativa; `UNKNOWN` nunca é oferecido."""

    SUPPORTED = "SUPPORTED"
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class NativeWebOutcome(str, Enum):
    """Desfecho técnico, não uma certificação de verdade factual."""

    SUCCESS = "SUCCESS"
    NO_RESULTS = "NO_RESULTS"
    TIMEOUT = "TIMEOUT"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    NOT_VERIFIED = "NOT_VERIFIED"


@dataclass(frozen=True)
class NativeWebCapability:
    """Pequena capacidade do adaptador; independente de `Capability.TOOL_CALLS`.

    `AVAILABLE` só pode ser devolvido quando o adaptador provou o perfil nativo e o
    perfil OFF. `can_disable` descreve o controlo do turno, não a existência de tools
    genéricas.
    """

    supported: bool = False
    available: bool = False
    can_disable: bool = True
    state: NativeWebState = NativeWebState.UNKNOWN

    def __post_init__(self) -> None:
        try:
            state = NativeWebState(self.state)
        except ValueError:
            state = NativeWebState.UNKNOWN
        object.__setattr__(self, "state", state)

    @property
    def selectable(self) -> bool:
        """A UI só pode oferecer `provider` neste estado, nunca em UNKNOWN/SUPPORTED."""
        return bool(
            self.state is NativeWebState.AVAILABLE
            and self.supported
            and self.available
            and self.can_disable
        )

    def dto(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "available": self.available,
            "can_disable": self.can_disable,
            "state": self.state.value,
        }

    @classmethod
    def unknown(cls) -> NativeWebCapability:
        return cls()

    @classmethod
    def unsupported(cls) -> NativeWebCapability:
        return cls(state=NativeWebState.UNAVAILABLE)

    @classmethod
    def supported_only(cls) -> NativeWebCapability:
        return cls(supported=True, available=False, state=NativeWebState.SUPPORTED)

    @classmethod
    def ready(cls) -> NativeWebCapability:
        return cls(supported=True, available=True, can_disable=True,
                   state=NativeWebState.AVAILABLE)


@dataclass(frozen=True)
class NativeWebSource:
    """Metadata normalizada que o adaptador realmente observou.

    Não contém query, conteúdo integral, credenciais nem IDs inventados. `url` é
    validada/normalizada pelo adaptador; o controller ainda a passa pela política pública
    antes de publicar. `published_at` é uma data ISO ou `None`; nunca se infere de `[n]`.
    """

    title: str
    url: str
    snippet: str = ""
    published_at: str | None = None
    date_source: str | None = None
    retrieved_at: str = ""
    read: bool = False
    truncated: bool = False
    cached: bool = False
    engines: tuple[str, ...] = ()


@dataclass(frozen=True)
class NativeWebResult:
    """Resultado de `complete_web`; a resposta do modelo e a proveniência observada."""

    text: str
    provider_id: str
    tool_names: tuple[str, ...] = ()
    sources: tuple[NativeWebSource, ...] = ()
    outcome: NativeWebOutcome = NativeWebOutcome.SUCCESS
    failure: str = ""
    failure_reason: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        try:
            outcome = NativeWebOutcome(self.outcome)
        except ValueError:
            outcome = NativeWebOutcome.FAILED
        object.__setattr__(self, "outcome", outcome)
        failure = str(self.failure or "")
        if failure not in WEB_PROVIDER_FAILURES:
            failure = "unknown"
        object.__setattr__(self, "failure", failure)

    @property
    def source_count(self) -> int:
        return len(self.sources)


@runtime_checkable
class NativeWebBrain(Protocol):
    """Interface opcional que o mesmo Brain pode implementar além de `Brain`."""

    provider_id: str

    def native_web_capability(self) -> NativeWebCapability:
        ...

    async def complete_web(self, messages: list[dict[str, Any]]) -> NativeWebResult:
        ...

    async def cancel_web(self) -> bool:
        ...


async def stop_native_web(brain: object) -> bool:
    """Confirma que nenhum trabalho provider continua antes de uma recuperação.

    Um adaptador que mantém processo/CLI vivo DEPOIS de `complete_web` devolver TEM de
    implementar `cancel_web` e devolver `True` apenas quando o filho terminou. A ausência
    do método significa que `complete_web` é a dona do ciclo de vida e já terminou o filho
    ao devolver; por isso devolve `True`. `False` ou exceção bloqueiam a recuperação.
    """
    cancel = getattr(brain, "cancel_web", None)
    if not callable(cancel):
        return True
    try:
        return bool(await cancel())
    except Exception:
        return False


def native_web_capability_of(brain: object) -> NativeWebCapability:
    """Lê a capacidade sem executar inferência, processo ou rede.

    Um adaptador antigo/sem este método devolve `UNKNOWN`; nunca se infere capacidade de
    `Capability.TOOL_CALLS`, de uma lista de tools ou de um display name.
    """
    getter = getattr(brain, "native_web_capability", None)
    if not callable(getter):
        return NativeWebCapability.unknown()
    try:
        capability = getter()
    except Exception:
        return NativeWebCapability.unknown()
    if not isinstance(capability, NativeWebCapability):
        return NativeWebCapability.unknown()
    return capability


def can_complete_web(brain: object) -> bool:
    """Confirma a superfície do método antes de a UI/controller prometer `provider`."""
    return callable(getattr(brain, "complete_web", None))


def _normal_endpoint(endpoint: str) -> str:
    """Normaliza endpoint sem credenciais/query/fragment e sem barra final."""
    text = str(endpoint or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text.rstrip("/")
    if not parts.scheme or not parts.hostname:
        return text.rstrip("/")
    host = parts.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port is not None else ""
    path = parts.path.rstrip("/")
    return f"{parts.scheme.lower()}://{host}{port}{path}"


def brain_fingerprint(
    provider: str,
    model: str,
    endpoint: str = "",
    role: str = "host",
) -> str:
    """Fingerprint estável provider/model/endpoint/role, sem chave ou credencial.

    É a chave de `brain_origins`; não inclui `api_key`, display name, token, path de
    utilizador, sessão ou qualquer valor secreto. Mudar a chave não muda o fingerprint.
    """
    material = json.dumps(
        {
            "provider": str(provider or "").strip().lower(),
            "model": str(model or "").strip(),
            "endpoint": _normal_endpoint(endpoint),
            "role": str(role or "host").strip().lower(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"brain-{digest}"


@dataclass(frozen=True)
class BrainIdentity:
    """Identidade do Brain usada pela preferência web; não é uma credencial."""

    provider: str
    model: str
    endpoint: str = ""
    role: str = "host"

    @property
    def fingerprint(self) -> str:
        return brain_fingerprint(self.provider, self.model, self.endpoint, self.role)


def plan_web_attempts(
    preference: str,
    *,
    provider_ready: bool,
    lunmi_available: bool,
) -> tuple[WebOrigin, ...]:
    """Plano fechado e imutável: no máximo duas tentativas, só para a frente.

    `auto` tenta o provedor quando a capacidade foi provada e deixa o Free Search como
    recuperação sequencial. Preferências explícitas NUNCA alternam de origem: um provider
    indisponível devolve `()` e a Task comunica indisponibilidade sem consultar a Lunmi.
    """
    if preference == "none":
        return ()
    if preference == "provider":
        return ("provider",) if provider_ready else ()
    if preference == "lunmi":
        return ("lunmi",) if lunmi_available else ()
    if preference == "auto":
        if provider_ready:
            return ("provider", "lunmi") if lunmi_available else ("provider",)
        return ("lunmi",) if lunmi_available else ()
    return ()


def should_recover_web(
    snapshot: WebOriginSnapshot,
    *,
    outcome: str,
    failure: str,
    remaining_s: float,
) -> bool:
    """A única decisão de recuperação: `auto`, falha recuperável e prazo útil.

    Puro e offline. Nunca recupera de sucesso (evidência suficiente), cancelamento,
    segurança/OFF, `BLOCKED` nem de falha sem prova (`unknown`/vazia). Não altera prazos:
    só lê o que resta do MESMO deadline da Task.
    """
    if not snapshot.recovery or snapshot.preference != "auto":
        return False
    if len(snapshot.attempts) != 2 or snapshot.attempts[1] != "lunmi":
        return False
    if remaining_s < WEB_RECOVERY_MIN_REMAINING_S:
        return False
    if outcome in {"SUCCESS", "BLOCKED", "CANCELLED"}:
        return False
    if outcome not in {"FAILED", "TIMEOUT", "NO_RESULTS", "NOT_VERIFIED"}:
        return False
    # Uma classe FECHADA e observada é obrigatória: `NOT_VERIFIED` genérico, ausência de
    # fontes estruturadas ou `unknown` nunca autorizam uma segunda consulta.
    return failure in RECOVERABLE_PROVIDER_FAILURES


@dataclass(frozen=True)
class WebOriginSnapshot:
    """Decisão congelada no início da Task; fallback só existe antes de iniciar.

    `attempts` é o plano completo: `origin` é sempre `attempts[0]` (ou `none`). `recovery`
    só é verdadeiro em `auto` com as duas tentativas; nenhuma transição fora do plano é
    permitida e nunca se volta à origem anterior.
    """

    preference: WebPreference
    origin: WebOrigin
    attempts: tuple[WebOrigin, ...] = ()
    recovery: bool = False
    fallback: WebFallbackReason = ""
    native_web: NativeWebCapability = field(default_factory=NativeWebCapability.unknown)
    lunmi_available: bool = False

    @property
    def provider(self) -> bool:
        return self.origin == "provider"

    @property
    def lunmi(self) -> bool:
        return self.origin == "lunmi"
