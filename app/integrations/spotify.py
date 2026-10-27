"""Broker Spotify do produto (I1): host HMAC, pareamento, leitura e concessão one-shot.

Porta o host do kit congelado (`app/integrations/spotify_host`) sem relaxar fences:
um cliente autenticado, HMAC com nonces/epoch, um comando em voo, deadline e sem
replay. Aqui ficam só as decisões de produto:

* segredo de pareamento por instalação (DPAPI) e bundle privado do mesmo utilizador;
* ativação local só por `grant_local_activation(...)` → `consume()` → `activate_local`
  (consumido antes do envio, invalidado por epoch/fecho; reauth não repõe);
* abrir Spotify/página oficial apenas por alvo fechado (`app`/`download`), sem URL
  ou path vindos do renderer;
* nenhum polling de estado musical: o host emite transições de sessão e o broker
  propaga-as ao instalador quando existem.
"""
from __future__ import annotations

import asyncio
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from app.integrations.spotify_host import secretstore
from app.integrations.spotify_host.config import KitError
from app.integrations.spotify_host.server import ActionResult, SpotifyHostBridge

SPOTIFY_URL = "https://www.spotify.com/pt-pt/download/windows/"
SECRET_NAME = "secret.bin"


class SpotifyBrokerError(RuntimeError):
    """Falha pública e limitada do broker; `code` segue o contrato Spotify."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SpotifyActivationGrant:
    """Concessão de UMA ativação local, ligada ao pedido humano atual.

    `consume()` é atómico e marca a concessão antes do envio; um segundo consumo,
    um epoch novo, fecho do broker ou expiração devolvem `False` sem efeito.
    """

    def __init__(self, broker: "SpotifyBroker", *, request_id: str, task_id: str,
                 epoch: int, expires_in: float) -> None:
        self._broker = broker
        self.request_id = request_id
        self.task_id = task_id
        self.epoch = epoch
        self.expires_at = time.monotonic() + max(1.0, float(expires_in))
        self.consumed = False
        self._dispatched = False

    def consume(self) -> bool:
        with self._broker._lock:
            if self.consumed:
                return False
            if self._broker._closed or self.epoch != self._broker.epoch:
                return False
            if time.monotonic() > self.expires_at or self._broker._pending is not None:
                return False
            self.consumed = True
            self._broker._pending = self
            return True


class SpotifyBroker:
    """Host loopback + segredo + leitura/ativação; sem UI e sem decidir música."""

    def __init__(
        self,
        *,
        state_root: Path,
        session_id: str = "",
        port: int = 45871,
        mode: str = "control",
        on_session: Callable[[dict[str, Any]], None] | None = None,
        launcher: Callable[[str], None] | None = None,
    ) -> None:
        self.state_root = Path(state_root)
        self.session_id = session_id
        self.port = int(port)
        self._mode = mode
        self._on_session = on_session
        self._launcher = launcher
        self._ready = threading.Event()
        self._start_error: str | None = None
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_event: asyncio.Event | None = None
        self._host: SpotifyHostBridge | None = None
        self._started = False
        self._closed = False
        self._epoch = 0
        self._pending: SpotifyActivationGrant | None = None
        self._secret: bytes | None = None
        self._secret_path = self.state_root / ".local" / "spotify" / SECRET_NAME
        self._last_session: dict[str, Any] | None = None

    # ------------------------------------------------------------ lifecycle --
    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def started(self) -> bool:
        with self._lock:
            return self._started and not self._closed

    def start(self) -> dict[str, Any]:
        """Sobe o listener loopback; o Spotify pode ainda não estar aberto."""
        with self._lock:
            if self._closed:
                raise SpotifyBrokerError("unavailable")
            if self._started:
                return self.info()
            self._secret = self._load_secret()
            self._loop = asyncio.new_event_loop()
            self._ready = threading.Event()
            self._start_error = None
            self._thread = threading.Thread(
                target=self._serve, name="spotify-broker-loop", daemon=True
            )
            self._thread.start()
            self._ready.wait(timeout=5.0)
            if not self._started:
                error = self._start_error or "unavailable"
                self._join()
                raise SpotifyBrokerError(error)
            self._epoch += 1
            return self.info()

    def stop(self) -> None:
        """Para o host e invalida concessões, mantendo a possibilidade de religar."""
        with self._lock:
            self._pending = None
            loop, stop_event = self._loop, self._stop_event
        if loop is not None and stop_event is not None:
            try:
                loop.call_soon_threadsafe(stop_event.set)
            except RuntimeError:
                pass
        self._join()

    def _join(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None
        self._loop = None
        self._host = None
        self._started = False

    def _serve(self) -> None:
        loop = self._loop
        assert loop is not None
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._serve_until_stop())
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    async def _serve_until_stop(self) -> None:
        stop_event = asyncio.Event()
        self._stop_event = stop_event
        try:
            host = SpotifyHostBridge(
                self._secret or b"",
                mode=self._mode,
                origin_allowlist=("https://xpui.app.spotify.com",),
                default_deadline_ms=5000,
                state={},
            )
            host.session_listener = self._session_changed
            await host.start(self.port)
        except KitError as exc:
            self._start_error = self._public_error(exc.code)
            self._ready.set()
            return
        except Exception:
            self._start_error = "unavailable"
            self._ready.set()
            return
        self._host = host
        self._started = True
        self._ready.set()
        try:
            await stop_event.wait()
        finally:
            await host.aclose()
            self._started = False

    @staticmethod
    def _public_error(code: str) -> str:
        return "port_in_use" if code == "KIT_PORT_IN_USE" else "unavailable"

    def info(self) -> dict[str, Any]:
        host = self._host
        if host is None or not self._started:
            return {"authenticated": False, "mode": self._mode, "device_scope": None,
                    "port": None, "epoch": self._epoch}
        try:
            info = host.session_info
        except Exception:
            info = {"authenticated": False, "mode": self._mode, "device_scope": None}
        return {**info, "port": self.port, "epoch": self._epoch}

    def _session_changed(self, session: Any) -> None:
        # Auth/end é uma fronteira de identidade: refs e grants antigos não
        # sobrevivem a uma sessão nova, mesmo sem fechar o broker. O DTO público
        # leva o epoch novo, nunca nonce/segredo.
        with self._lock:
            self._epoch += 1
            self._pending = None
        callback = self._on_session
        if callback is None:
            return
        try:
            callback(self.info())
        except Exception:
            # Uma transição de UI nunca pode derrubar o host.
            pass

    # -------------------------------------------------------------- pairing --
    def _load_secret(self) -> bytes:
        path = self._secret_path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            secret = secretstore.load_secret(path)
        except (OSError, KitError):
            secret = b""
        if len(secret) != 32:
            secret = secretstore.generate_secret()
            try:
                secretstore.save_secret(path, secret)
            except KitError as exc:
                raise SpotifyBrokerError("unavailable") from exc
        return secret

    def secret_hex(self) -> str:
        if self._secret is None:
            self._secret = self._load_secret()
        return self._secret.hex()

    # ------------------------------------------------------- pairing rotation --
    def rotate_pairing(self) -> str:
        """Gera um segredo novo para repair e devolve o anterior (hex para recuperação)."""
        with self._lock:
            previous = self._secret or self._load_secret()
        self.stop()
        replacement = secretstore.generate_secret()
        try:
            secretstore.save_secret(self._secret_path, replacement)
        except (OSError, KitError) as exc:
            raise SpotifyBrokerError("unavailable") from exc
        with self._lock:
            self._secret = replacement
        return previous.hex()

    def revoke_pairing(self) -> None:
        """Remove o pareamento do produto; nunca toca na conta Spotify."""
        self.stop()
        try:
            self._secret_path.unlink(missing_ok=True)
        except OSError as exc:
            raise SpotifyBrokerError("unavailable") from exc
        with self._lock:
            self._secret = None

    def restore_pairing(self, secret_hex: str | None) -> None:
        """Recupera o par anterior quando uma transação de repair falha."""
        self.stop()
        if not secret_hex:
            with self._lock:
                self._secret = None
            return
        try:
            replacement = bytes.fromhex(secret_hex)
        except ValueError as exc:
            raise SpotifyBrokerError("invalid_payload") from exc
        if len(replacement) != 32:
            raise SpotifyBrokerError("invalid_payload")
        try:
            secretstore.save_secret(self._secret_path, replacement)
        except (OSError, KitError) as exc:
            raise SpotifyBrokerError("unavailable") from exc
        with self._lock:
            self._secret = replacement

    def bundle(self, extension_source: str) -> str:
        """Bundle privado com config; nunca é servido ao renderer Lunmi nem logado."""
        header = (
            "/* LUNMI SPOTIFY - PRIVATE BUNDLE (generated; contains the pairing secret).\n"
            " * Do not share, commit or paste. Rotate by reinstalling/repairing.\n"
            " */\n"
            "globalThis.LUNMI_SPOTIFY_KIT_CONFIG = Object.freeze("
            '{"url":"ws://127.0.0.1:%d","secretHex":"%s"});\n'
            % (self.port, self.secret_hex())
        )
        return header + extension_source

    # ---------------------------------------------------------------- reads --
    def verify_read(self, *, timeout: float = 8.0) -> tuple[bool, str]:
        """HMAC já autenticado + UMA leitura real; ficheiro/ACK não contam."""
        host = self._host
        if host is None or not self._started:
            return False, "not_ready"
        # O Spotify pode estar a arrancar com a extensão: esperar pelo handshake
        # HMAC dentro do deadline, sem polling de estado musical.
        deadline = time.monotonic() + max(0.0, float(timeout))
        while not host.has_active_session and time.monotonic() < deadline:
            time.sleep(0.1)
        if not host.has_active_session:
            return False, "not_ready"
        try:
            result = self._call(host.request("get_player_state"), timeout)
        except Exception:
            return False, "not_ready"
        if result.status != "SUCCESS_VERIFIED" or not (result.evidence or {}).get("observed"):
            return False, "not_ready"
        return True, ""

    def _call(self, coro: Any, timeout: float) -> ActionResult:
        loop = self._loop
        if loop is None:
            raise SpotifyBrokerError("unavailable")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)

    def _action(self, action: str, args: dict[str, Any] | None = None,
                *, timeout: float = 5.0) -> ActionResult:
        """One closed kit action; the host keeps HMAC/deadline/no-replay authority."""
        host = self._host
        if host is None or not self._started:
            raise SpotifyBrokerError("not_ready")
        try:
            return self._call(host.request(action, args or {}, int(max(0.1, timeout) * 1000)), timeout)
        except SpotifyBrokerError:
            raise
        except TimeoutError as exc:
            # A timeout is an uncertain dispatch, never a reason to send it again.
            raise SpotifyBrokerError("interrupted") from exc
        except Exception as exc:
            raise SpotifyBrokerError("unavailable") from exc

    def player_state(self, *, timeout: float = 5.0) -> ActionResult:
        return self._action("get_player_state", timeout=timeout)

    def search_tracks(self, query: str, limit: int = 10, *, timeout: float = 5.0) -> ActionResult:
        return self._action("search_tracks", {"query": query, "limit": limit}, timeout=timeout)

    def play_uri(self, uri: str, *, timeout: float = 5.0) -> ActionResult:
        return self._action("play_uri", {"uri": uri}, timeout=timeout)

    def play(self, *, timeout: float = 5.0) -> ActionResult:
        return self._action("play", timeout=timeout)

    def pause(self, *, timeout: float = 5.0) -> ActionResult:
        return self._action("pause", timeout=timeout)

    def set_volume(self, level: float, *, timeout: float = 5.0) -> ActionResult:
        return self._action("set_volume", {"level": level}, timeout=timeout)

    def next_track(self, *, timeout: float = 5.0) -> ActionResult:
        return self._action("next", timeout=timeout)

    def previous_track(self, *, timeout: float = 5.0) -> ActionResult:
        return self._action("previous", timeout=timeout)

    # ------------------------------------------------------------ activation --
    def grant_local_activation(
        self,
        *,
        request_id: str,
        task_id: str,
        epoch: int,
        expires_in: float = 15.0,
    ) -> SpotifyActivationGrant:
        """Só o controller, para um pedido humano atual; nunca rearmado por reauth."""
        if not isinstance(request_id, str) or not request_id.strip():
            raise SpotifyBrokerError("invalid_payload")
        with self._lock:
            if self._closed or epoch != self._epoch:
                raise SpotifyBrokerError("unavailable")
            if self._pending is not None:
                raise SpotifyBrokerError("unavailable")
        return SpotifyActivationGrant(
            self,
            request_id=request_id[:128],
            task_id=str(task_id or "")[:128],
            epoch=epoch,
            expires_in=expires_in,
        )

    def activate_local(self, grant: SpotifyActivationGrant) -> dict[str, Any]:
        host = self._host
        with self._lock:
            if (
                grant._broker is not self
                or not grant.consumed
                or grant._dispatched
                or self._pending is not grant
                or self._closed
                or grant.epoch != self._epoch
                or time.monotonic() > grant.expires_at
            ):
                if self._pending is grant:
                    self._pending = None
                raise SpotifyBrokerError("unavailable")
            grant._dispatched = True
            self._pending = None
        if host is None or not host.grant_activation_once():
            raise SpotifyBrokerError("not_ready")
        try:
            result = self._call(host.request("activate_local"), 6.0)
        except Exception as exc:
            raise SpotifyBrokerError("not_ready") from exc
        return {"status": result.status, "evidence": result.evidence or {},
                "error_code": result.error_code}

    # ----------------------------------------------------------------- open ---
    def open_url(self) -> dict[str, Any]:
        if self._launcher is not None:
            self._launcher(SPOTIFY_URL)
        else:
            import webbrowser

            if not webbrowser.open(SPOTIFY_URL, new=2):
                return {"opened": False}
        return {"opened": True}

    def open_app(self, executable: Path) -> dict[str, Any]:
        """Abre só o executável Spotify já validado; sem args, shell ou path do renderer."""
        target = Path(executable)
        try:
            resolved = target.resolve(strict=True)
        except OSError:
            raise SpotifyBrokerError("spotify_missing") from None
        if target.is_symlink():
            raise SpotifyBrokerError("invalid_payload")
        if resolved.suffix.lower() != ".exe" or not resolved.is_file():
            raise SpotifyBrokerError("spotify_missing")
        if self._launcher is not None:
            self._launcher(str(resolved))
        else:
            import subprocess

            subprocess.Popen([str(resolved)], close_fds=True)
        return {"opened": True}

    # ------------------------------------------------------------------ stop --
    def close(self) -> None:
        """Fecho da aplicação: sem religar nesta instância; o Spotify fica como estava."""
        with self._lock:
            self._closed = True
            self._pending = None
        self.stop()
        with self._lock:
            self._closed = True


def random_request_token() -> str:
    """Token opaco para correlacionar um pedido de ativação (nunca uma aprovação)."""
    return "act-" + secrets.token_hex(12)
