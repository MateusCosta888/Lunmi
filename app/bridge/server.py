"""Servidor stdio JSONL v1 da ponte Lunmi (`python -m app.bridge.server`).

Regras de isolamento:
- `stdout` emite **exclusivamente** linhas JSONL UTF-8 do protocolo.
- Qualquer log ou diagnóstico vai para `stderr` (sem segredos/payloads sensíveis).
- Em modo `--handshake-only` (ou quando `BridgeServer(..., dispatcher=None, service=None)` é
  instanciado em testes unitários de transporte), não instancia `TaskController` nem abre o banco
  SQLite no construtor, e `capabilities` anuncia honestamente `converse/execute/memory/brain=False`.
- Em execução padrão do produto (`python -m app.bridge.server`), o `BridgeService` real é
  inicializado sob demanda no primeiro comando de domínio (ou quando `enable_service=True`),
  anunciando apenas as capabilities realmente suportadas por handlers registrados.
- Quando `--sandbox-dir` é fornecido, o diretório raiz de estado/preferências/memória fica
  confinado ao sandbox desde a partida.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable

from app.bridge import protocol
from app.bridge.service import BridgeService, filter_trusted_host_pids
from app.settings import ConfigurationError, Settings


class BridgeServer:
    """Servidor JSONL stdio com handshake `session.hello` -> `session.ready` e `app.shutdown`."""

    def __init__(
        self,
        *,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | None = None,
        stderr: BinaryIO | None = None,
        sandbox_dir: Path | None = None,
        dispatcher: Callable[[protocol.Envelope, Callable[[dict[str, Any]], None]], dict[str, Any]] | None = None,
        supported_commands: Iterable[str] | None = None,
        service: BridgeService | None = None,
        enable_service: bool = False,
        trusted_root_pid: int | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._stdin = stdin if stdin is not None else sys.stdin.buffer
        self._stdout = stdout if stdout is not None else sys.stdout.buffer
        self._stderr = stderr if stderr is not None else sys.stderr.buffer
        self._stdout_lock = threading.Lock()
        self.sandbox_dir = Path(sandbox_dir).resolve() if sandbox_dir is not None else None
        self.session_id = f"sess-{uuid.uuid4().hex[:16]}"
        self.handshake_completed = False
        self.shutting_down = False
        self.trusted_root_pid = trusted_root_pid if trusted_root_pid is not None else os.getppid()
        self.host_pids: set[int] = filter_trusted_host_pids([], trusted_root_pid=self.trusted_root_pid)
        self._dispatcher = dispatcher
        self._service = service
        self._settings = settings
        self._enable_service = enable_service or (service is not None)
        if supported_commands is not None:
            self._supported_commands: frozenset[str] = frozenset(supported_commands)
        elif service is not None or enable_service:
            self._supported_commands = BridgeService.SUPPORTED_COMMANDS
        else:
            self._supported_commands = frozenset()
        self._framer = protocol.JsonlFramer(max_line_bytes=protocol.MAX_LINE_BYTES)

    def _ensure_service(self) -> BridgeService:
        if self._service is None:
            self._service = BridgeService(
                emit=self.emit_event,
                sandbox_dir=self.sandbox_dir,
                trusted_root_pid=self.trusted_root_pid,
                settings=self._settings,
            )
            self._service.update_host_pids(sorted(self.host_pids))
        return self._service

    def _log(self, message: str) -> None:
        try:
            self._stderr.write((f"[bridge] {message}\n").encode("utf-8", errors="replace"))
            self._stderr.flush()
        except OSError:
            pass

    def send_message(self, message: dict[str, Any]) -> None:
        raw = protocol.encode_message(message).encode("utf-8")
        with self._stdout_lock:
            self._stdout.write(raw)
            self._stdout.flush()

    def emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.send_message(protocol.make_event(event_type, payload))

    def handle_raw_line(self, raw_line: bytes) -> None:
        try:
            env = protocol.parse_envelope(raw_line)
        except protocol.ProtocolError as exc:
            self.send_message(protocol.make_error(exc.request_id, exc.code, exc.public_message))
            return

        if env.type == "session.hello":
            try:
                self._handle_hello(env)
            except protocol.ProtocolError as exc:
                self.send_message(protocol.make_error(env.id, exc.code, exc.public_message))
            return

        if env.type == "app.shutdown":
            self._handle_shutdown(env)
            return

        if env.type not in protocol.COMMAND_TYPES:
            self.send_message(
                protocol.make_error(
                    env.id,
                    "unknown_command",
                    f"Comando desconhecido no contrato v1: {env.type}",
                )
            )
            return

        if not self.handshake_completed:
            self.send_message(
                protocol.make_error(
                    env.id,
                    "handshake_required",
                    "Envie session.hello antes de outros comandos.",
                )
            )
            return

        if self._dispatcher is not None:
            if self._supported_commands and env.type not in self._supported_commands:
                self.send_message(
                    protocol.make_error(
                        env.id,
                        "not_implemented",
                        f"Comando {env.type} não suportado nesta sessão.",
                    )
                )
                return
            try:
                result_payload = self._dispatcher(env, self.send_message)
                self.send_message(protocol.make_result(env.id, result_payload))
            except protocol.ProtocolError as exc:
                self.send_message(protocol.make_error(env.id, exc.code, exc.public_message))
            except Exception as exc:
                self._log(f"Erro interno em {env.type}: {type(exc).__name__}")
                self.send_message(
                    protocol.make_error(
                        env.id,
                        "internal_error",
                        f"Falha ao processar {env.type}.",
                    )
                )
            return

        if self._enable_service:
            try:
                svc = self._ensure_service()
                result_payload = svc.dispatch(env, self.send_message)
                self.send_message(protocol.make_result(env.id, result_payload))
            except protocol.ProtocolError as exc:
                self.send_message(protocol.make_error(env.id, exc.code, exc.public_message))
            except Exception as exc:
                self._log(f"Erro interno em {env.type}: {type(exc).__name__}")
                self.send_message(
                    protocol.make_error(
                        env.id,
                        "internal_error",
                        f"Falha ao processar {env.type}.",
                    )
                )
            return

        self.send_message(
            protocol.make_error(
                env.id,
                "not_implemented",
                f"Comando {env.type} ainda não habilitado nesta instância da ponte.",
            )
        )

    def _handle_hello(self, env: protocol.Envelope) -> None:
        raw_pids = env.payload.get("host_pids")
        validated_pids = protocol.validate_host_pids(raw_pids)
        self.host_pids = filter_trusted_host_pids(
            validated_pids,
            trusted_root_pid=self.trusted_root_pid,
        )
        if self._service is not None:
            self._service.update_host_pids(sorted(self.host_pids))

        self.handshake_completed = True
        caps = protocol.capabilities_for_commands(
            self._supported_commands,
            web_search=False,
            browser_use=False,
        )
        ready_payload: dict[str, Any] = {
            "v": protocol.PROTOCOL_VERSION,
            "session_id": self.session_id,
            "pid": os.getpid(),
            "capabilities": caps,
            "limits": {
                "max_line_bytes": protocol.MAX_LINE_BYTES,
                "max_import_bytes": protocol.MAX_IMPORT_BYTES,
                "max_chat_text_chars": protocol.MAX_CHAT_TEXT_CHARS,
                "max_memory_text_chars": protocol.MAX_MEMORY_TEXT_CHARS,
                "max_id_chars": protocol.MAX_ID_CHARS,
                "max_type_chars": protocol.MAX_TYPE_CHARS,
                "max_host_pids": protocol.MAX_HOST_PIDS,
            },
        }
        self.emit_event("session.ready", ready_payload)
        self.send_message(protocol.make_result(env.id, ready_payload))

    def _handle_shutdown(self, env: protocol.Envelope) -> None:
        self.shutting_down = True
        if self._service is not None:
            try:
                self._service.close()
            except Exception as exc:
                self._log(f"Aviso ao encerrar BridgeService: {type(exc).__name__}")
        if self._dispatcher is not None:
            try:
                self._dispatcher(env, self.send_message)
            except Exception as exc:
                self._log(f"Aviso ao encerrar serviço na ponte: {type(exc).__name__}")
        self.send_message(
            protocol.make_result(
                env.id,
                {"status": "shutting_down", "session_id": self.session_id},
            )
        )

    def run(self) -> int:
        try:
            while not self.shutting_down:
                read_fn = getattr(self._stdin, "read1", None)
                chunk = read_fn(65536) if callable(read_fn) else self._stdin.read(65536)
                if not chunk:
                    break
                outcomes = self._framer.feed_outcomes(chunk)
                for item in outcomes:
                    if item.error is not None:
                        self.send_message(
                            protocol.make_error(
                                item.error.request_id,
                                item.error.code,
                                item.error.public_message,
                            )
                        )
                        continue
                    if item.raw is not None:
                        self.handle_raw_line(item.raw)
                        if self.shutting_down:
                            break
        finally:
            if self._service is not None:
                try:
                    self._service.close()
                except Exception:
                    pass
        return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lunmi IPC Bridge Server (stdio JSONL v1)")
    parser.add_argument(
        "--sandbox-dir",
        type=Path,
        default=None,
        help="Diretório de sandbox isolado (nunca toca estado pessoal quando definido).",
    )
    parser.add_argument(
        "--handshake-only",
        action="store_true",
        help="Modo mínimo (sem serviço de domínio): responde apenas session.hello e app.shutdown.",
    )
    parser.add_argument(
        "--settings-in-ram",
        action="store_true",
        help=(
            "Carrega a configuração real (provider/modelo/credencial já configurados) SOMENTE em "
            "RAM e mantém toda a persistência confinada ao --sandbox-dir. Nada de configuração "
            "pessoal é gravado; prewarm/índice de aplicativos/autoaprovação saem desligados para "
            "uma bateria de aceite não provocar efeito colateral. Uso: aceite e depuração."
        ),
    )
    return parser.parse_args(argv)


def _sandbox_ram_settings(sandbox_dir: Path | None) -> Settings | None:
    """Configuração real em RAM com os desligamentos que uma bateria de aceite exige.

    Lê o que o usuário já tem (sem escrever nada) e devolve uma cópia em memória com prewarm,
    índice de aplicativos, busca de aplicativos e autoaprovação desligados. A persistência
    continua sendo do sandbox, porque é o `sandbox_dir` do serviço que decide onde gravar.
    """
    import dataclasses

    try:
        base = Settings.load()
    except ConfigurationError as exc:
        print(f"[bridge] configuração real indisponível ({exc}); seguindo com o padrão do sandbox.",
              file=sys.stderr)
        return None
    overrides: dict[str, Any] = {
        "prewarm": False,
        "app_index": False,
        "app_search": False,
        "auto_approve": False,
    }
    if sandbox_dir is not None:
        overrides["logs_path"] = sandbox_dir / "logs"
        overrides["output_path"] = sandbox_dir / "output"
    return dataclasses.replace(base, **overrides)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings: Settings | None = None
    if args.sandbox_dir is not None:
        args.sandbox_dir.mkdir(parents=True, exist_ok=True)
    if args.settings_in_ram:
        if args.sandbox_dir is None:
            print("[bridge] --settings-in-ram exige --sandbox-dir; ignorando.", file=sys.stderr)
        else:
            settings = _sandbox_ram_settings(args.sandbox_dir)
    server = BridgeServer(
        sandbox_dir=args.sandbox_dir,
        enable_service=not bool(args.handshake_only),
        settings=settings,
    )
    return server.run()


if __name__ == "__main__":
    raise SystemExit(main())
