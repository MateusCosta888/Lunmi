"""Testes do protocolo e servidor stdio JSONL v1 da ponte (F0 + reparos da revisão F0).

Cobre:
- Envelope obrigatório (v=1, id, type, payload)
- Versão inválida recusada no handshake antes de mutar estado
- Rejeição estrita de NaN/Infinity/-Infinity na entrada e na saída
- Rejeição estrita de UTF-8 inválido e surrogates
- Validação de padrão ASCII para id (1..128) e type (1..64) e MAX_HOST_PIDS=64
- Capabilities verdadeiras: sem dispatcher/service anuncia converse/execute/memory/brain=False;
  com service habilitado anuncia True e responde às rotas reais
- Frames parciais, múltiplos frames num mesmo chunk, linhas vazias
- Linha oversized seguida de frame válido no mesmo chunk: reporta message_too_large para a linha
  gigante e preserva/processa o frame válido seguinte sem perda silenciosa
- Unicode (acentos, CJK, emojis) preservado byte-a-byte em UTF-8
- EOF limpo e shutdown ordenado
- stdout contém exclusivamente JSONL válido (diagnóstico em stderr)
- Subprocesso real `python -m app.bridge.server` sem instanciar controller/banco pessoal no hello
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.bridge import protocol
from app.bridge.server import BridgeServer


def test_constants_and_capabilities_contract() -> None:
    assert protocol.PROTOCOL_VERSION == 1
    assert protocol.MAX_IMPORT_BYTES == 4 * 1024 * 1024
    assert protocol.MAX_LINE_BYTES == 5 * 1024 * 1024
    assert protocol.MAX_LINE_BYTES >= protocol.MAX_IMPORT_BYTES
    assert protocol.MAX_CHAT_TEXT_CHARS == 8000
    assert protocol.MAX_MEMORY_TEXT_CHARS == 800
    assert protocol.MAX_ID_CHARS == 128
    assert protocol.MAX_TYPE_CHARS == 64
    assert protocol.MAX_HOST_PIDS == 64

    # Sessão sem handlers não anuncia falsas capacidades
    caps_minimal = protocol.default_capabilities()
    assert caps_minimal == {
        "integrations": False,
        "auto": False,
        "converse": False,
        "execute": False,
        "memory": False,
        "brain": False,
        "file_read": False,
        "web_search": False,
        "browser_use": False,
    }

    # Sessão com todos os comandos de protocolo habilita converse/execute/memory/brain/file_read
    caps_full = protocol.capabilities_for_commands(protocol.COMMAND_TYPES)
    assert caps_full == {
        "integrations": True,
        "auto": True,
        "converse": True,
        "execute": True,
        "memory": True,
        "brain": True,
        "file_read": True,
        "web_search": False,
        "browser_use": False,
    }


def test_file_read_capability_is_additive_and_an_old_v1_client_still_works() -> None:
    """DoD global8: a extensão é aditiva — o cliente v1 antigo lê o que conhece e ignora o resto.

    O "cliente antigo" aqui é o contrato v1 anterior a esta entrega: seis capabilities, sem
    `fs.read`. Ele decide o que fazer a partir dessas seis chaves; a sétima não muda nenhuma
    decisão dele, e o comando novo só chega se for enviado.
    """
    v1_client_knowledge = ("converse", "execute", "memory", "brain", "web_search", "browser_use")
    v1_expected = {"converse": True, "execute": True, "memory": True, "brain": True,
                   "web_search": False, "browser_use": False}

    caps = protocol.capabilities_for_commands(protocol.COMMAND_TYPES)
    assert set(v1_client_knowledge) <= set(caps)
    assert {key: caps[key] for key in v1_client_knowledge} == v1_expected
    assert caps["file_read"] is True
    # O comando é opcional no contrato: quem não o conhece nunca o envia e segue funcionando.
    assert "fs.read" in protocol.COMMAND_TYPES
    assert protocol.capabilities_for_commands(("chat.send", "task.cancel", "task.confirm",
                                               "task.answer"))["file_read"] is False


def test_python_protocol_matches_shared_golden_v1() -> None:
    golden_path = Path(__file__).resolve().parents[1] / "desktop" / "src" / "shared" / "ipc_golden_v1.json"
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    assert protocol.PROTOCOL_VERSION == golden["version"]
    assert protocol.MAX_LINE_BYTES == golden["limits"]["max_line_bytes"]
    assert protocol.MAX_IMPORT_BYTES == golden["limits"]["max_import_bytes"]
    assert protocol.MAX_CHAT_TEXT_CHARS == golden["limits"]["max_chat_text_chars"]
    assert protocol.MAX_MEMORY_TEXT_CHARS == golden["limits"]["max_memory_text_chars"]
    assert protocol.MAX_PROFILE_CHARS == golden["limits"]["max_profile_chars"]
    assert protocol.MAX_ID_CHARS == golden["limits"]["max_id_chars"]
    assert protocol.MAX_TYPE_CHARS == golden["limits"]["max_type_chars"]
    assert protocol.MAX_HOST_PIDS == golden["limits"]["max_host_pids"]
    assert protocol.ID_PATTERN.pattern == golden["patterns"]["id"]
    assert protocol.TYPE_PATTERN.pattern == golden["patterns"]["type"]
    assert sorted(protocol.COMMAND_TYPES) == sorted(golden["commands"])
    assert sorted(protocol.RESPONSE_TYPES) == sorted(golden["responses"])
    assert sorted(protocol.EVENT_TYPES) == sorted(golden["events"])
    assert protocol.default_capabilities() == golden["default_capabilities"]


def test_parse_envelope_valid_and_invalid_and_strict_json_utf8() -> None:
    env = protocol.parse_envelope('{"v":1,"id":"cmd-1","type":"session.hello","payload":{"client":"electron"}}')
    assert env.v == 1
    assert env.id == "cmd-1"
    assert env.type == "session.hello"
    assert env.payload == {"client": "electron"}

    with pytest.raises(protocol.ProtocolError) as exc_info:
        protocol.parse_envelope("isto nao e json")
    assert exc_info.value.code == "invalid_json"

    # Rejeição estrita de NaN / Infinity / -Infinity na entrada
    for bad_const in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(protocol.ProtocolError) as exc_info:
            protocol.parse_envelope(
                f'{{"v":1,"id":"nan-1","type":"session.hello","payload":{{"x":{bad_const}}}}}'
            )
        assert exc_info.value.code == "invalid_json"

    # Rejeição estrita de UTF-8 inválido
    with pytest.raises(protocol.ProtocolError) as exc_info:
        protocol.parse_envelope(b'{"v":1,"id":"u-1","type":"session.hello","payload":{"x":"\xff"}}')
    assert exc_info.value.code == "invalid_encoding"

    # Rejeição de surrogates
    with pytest.raises(protocol.ProtocolError) as exc_info:
        protocol.parse_envelope('{"v":1,"id":"s-1","type":"session.hello","payload":{"x":"\\ud800"}}')
    assert exc_info.value.code == "invalid_encoding"

    with pytest.raises(protocol.ProtocolError) as exc_info:
        protocol.parse_envelope('["lista"]')
    assert exc_info.value.code == "invalid_envelope"

    with pytest.raises(protocol.ProtocolError) as exc_info:
        protocol.parse_envelope('{"v":true,"id":"1","type":"session.hello","payload":{}}')
    assert exc_info.value.code == "unsupported_version"

    with pytest.raises(protocol.ProtocolError) as exc_info:
        protocol.parse_envelope('{"v":2,"id":"1","type":"session.hello","payload":{}}')
    assert exc_info.value.code == "unsupported_version"
    assert exc_info.value.request_id == "1"

    # Padrão ASCII de id e type
    with pytest.raises(protocol.ProtocolError) as exc_info:
        protocol.parse_envelope('{"v":1,"id":"id com espaco","type":"session.hello","payload":{}}')
    assert exc_info.value.code == "invalid_envelope"

    with pytest.raises(protocol.ProtocolError) as exc_info:
        protocol.parse_envelope('{"v":1,"id":"1","type":" UpperCase ","payload":{}}')
    assert exc_info.value.code == "invalid_envelope"

    with pytest.raises(protocol.ProtocolError) as exc_info:
        protocol.parse_envelope('{"v":1,"id":"1","type":"session.hello","payload":"nao-objeto"}')
    assert exc_info.value.code == "invalid_payload"


def test_validate_host_pids_enforces_max_64_and_positive_ints() -> None:
    assert protocol.validate_host_pids(None) == []
    assert protocol.validate_host_pids([10, 20]) == [10, 20]

    with pytest.raises(protocol.ProtocolError) as exc_info:
        protocol.validate_host_pids(list(range(1, 66)))  # 65 itens > 64
    assert exc_info.value.code == "invalid_payload"

    with pytest.raises(protocol.ProtocolError) as exc_info:
        protocol.validate_host_pids([True])
    assert exc_info.value.code == "invalid_payload"

    with pytest.raises(protocol.ProtocolError) as exc_info:
        protocol.validate_host_pids([0])
    assert exc_info.value.code == "invalid_payload"


def test_framer_partial_and_multiple_frames_and_unicode() -> None:
    framer = protocol.JsonlFramer(max_line_bytes=1024)
    msg1 = {"v": 1, "id": "u1", "type": "session.hello", "payload": {"note": "Olá Lunmi 🌙 日本語"}}
    msg2 = {"v": 1, "id": "u2", "type": "app.shutdown", "payload": {}}
    raw = (protocol.encode_message(msg1) + protocol.encode_message(msg2)).encode("utf-8")

    # Alimenta em pedaços pequenos quebrando no meio de caracteres multibyte UTF-8
    out: list[bytes] = []
    for i in range(0, len(raw), 7):
        out.extend(framer.feed(raw[i : i + 7]))

    assert len(out) == 2
    parsed1 = protocol.parse_envelope(out[0])
    parsed2 = protocol.parse_envelope(out[1])
    assert parsed1.id == "u1"
    assert parsed1.payload["note"] == "Olá Lunmi 🌙 日本語"
    assert parsed2.id == "u2"
    assert parsed2.type == "app.shutdown"


def test_framer_oversized_plus_valid_in_same_chunk_preserves_valid_frame() -> None:
    framer = protocol.JsonlFramer(max_line_bytes=128)
    valid = protocol.encode_message({"v": 1, "id": "ok-after-oversized", "type": "session.hello", "payload": {}}).encode(
        "utf-8"
    )
    combined = (b"x" * 200) + b"\n" + valid

    outcomes = framer.feed_outcomes(combined)
    assert len(outcomes) == 2
    assert outcomes[0].error is not None and outcomes[0].error.code == "message_too_large"
    assert outcomes[1].raw is not None
    assert protocol.parse_envelope(outcomes[1].raw).id == "ok-after-oversized"

    # E também via feed() + drain_pending_lines() sem perder o frame válido
    framer2 = protocol.JsonlFramer(max_line_bytes=128)
    with pytest.raises(protocol.ProtocolError) as exc_info:
        framer2.feed(combined)
    assert exc_info.value.code == "message_too_large"
    recovered = framer2.feed(b"") or framer2.drain_pending_lines()
    assert len(recovered) == 1
    assert protocol.parse_envelope(recovered[0]).id == "ok-after-oversized"


def test_server_oversized_plus_valid_in_same_chunk_answers_both() -> None:
    hello = protocol.encode_message({"v": 1, "id": "h-after", "type": "session.hello", "payload": {}}).encode("utf-8")
    shutdown = protocol.encode_message({"v": 1, "id": "s-after", "type": "app.shutdown", "payload": {}}).encode("utf-8")
    stdin = io.BytesIO((b"x" * 300) + b"\n" + hello + shutdown)
    stdout = io.BytesIO()
    stderr = io.BytesIO()

    server = BridgeServer(stdin=stdin, stdout=stdout, stderr=stderr)
    server._framer = protocol.JsonlFramer(max_line_bytes=256)
    assert server.run() == 0

    messages = [json.loads(line) for line in stdout.getvalue().decode("utf-8").splitlines() if line.strip()]
    assert messages[0]["type"] == "command.error"
    assert messages[0]["payload"]["code"] == "message_too_large"
    assert any(m["type"] == "command.result" and m["id"] == "h-after" for m in messages)
    assert any(m["type"] == "command.result" and m["id"] == "s-after" for m in messages)


def test_server_hello_ready_honest_capabilities_unknown_command_and_shutdown_in_memory() -> None:
    hello = protocol.encode_message(
        {"v": 1, "id": "h1", "type": "session.hello", "payload": {"client": "test", "client_version": "0.1.0"}}
    )
    unknown = protocol.encode_message(
        {"v": 1, "id": "u1", "type": "mystery.command", "payload": {}}
    )
    bad_ver = json.dumps({"v": 99, "id": "b1", "type": "session.hello", "payload": {}}) + "\n"
    shutdown = protocol.encode_message(
        {"v": 1, "id": "s1", "type": "app.shutdown", "payload": {}}
    )

    stdin = io.BytesIO((hello + unknown + bad_ver + shutdown).encode("utf-8"))
    stdout = io.BytesIO()
    stderr = io.BytesIO()

    # Sem dispatcher/service -> capabilities falsas de verdade
    server = BridgeServer(stdin=stdin, stdout=stdout, stderr=stderr)
    rc = server.run()
    assert rc == 0

    lines = [line for line in stdout.getvalue().decode("utf-8").splitlines() if line.strip()]
    messages = [json.loads(line) for line in lines]

    ready_events = [m for m in messages if m["type"] == "session.ready"]
    assert len(ready_events) == 1
    assert ready_events[0]["payload"]["capabilities"] == {
        "integrations": False,
        "auto": False,
        "converse": False,
        "execute": False,
        "memory": False,
        "brain": False,
        "file_read": False,
        "web_search": False,
        "browser_use": False,
    }

    err_u1 = next(m for m in messages if m["id"] == "u1")
    assert err_u1["type"] == "command.error"
    assert err_u1["payload"]["code"] == "unknown_command"

    err_b1 = next(m for m in messages if m["id"] == "b1")
    assert err_b1["type"] == "command.error"
    assert err_b1["payload"]["code"] == "unsupported_version"

    res_s1 = next(m for m in messages if m["id"] == "s1")
    assert res_s1["type"] == "command.result"
    assert res_s1["payload"]["status"] == "shutting_down"


def test_server_clean_eof_exits_zero() -> None:
    stdin = io.BytesIO(b"")
    stdout = io.BytesIO()
    stderr = io.BytesIO()
    server = BridgeServer(stdin=stdin, stdout=stdout, stderr=stderr)
    assert server.run() == 0
    assert stdout.getvalue() == b""


def test_a_slow_provider_command_never_blocks_cancel_or_confirm() -> None:
    """`brain.probe` esperando o provider não segura Parar nem a resposta a uma confirmação.

    Antes, todo comando de domínio era respondido na única thread que lê o stdin: um teste
    de modelo (até 95 s) congelava `task.cancel` e `task.confirm` atrás dele.
    """
    import threading

    provider_released = threading.Event()
    answered: list[str] = []

    def dispatcher(env: protocol.Envelope, _send) -> dict:
        if env.type == "brain.probe":
            assert provider_released.wait(5.0)
        answered.append(env.type)
        return {"ok": True}

    stdout = io.BytesIO()
    server = BridgeServer(stdin=io.BytesIO(), stdout=stdout, stderr=io.BytesIO(),
                          dispatcher=dispatcher,
                          supported_commands={"brain.probe", "task.cancel", "task.confirm"})

    def send(identifier: str, kind: str, payload: dict) -> None:
        server.handle_raw_line(protocol.encode_message(
            {"v": 1, "id": identifier, "type": kind, "payload": payload}).encode("utf-8"))

    send("h", "session.hello", {"client": "test"})
    send("p", "brain.probe", {"consent": True})
    send("c", "task.cancel", {})
    send("k", "task.confirm", {"request_id": "cfm-1", "approved": False})
    # Both answers left while the provider was still holding the probe.
    assert answered == ["task.cancel", "task.confirm"]

    provider_released.set()
    assert server.wait_for_slow_commands(5.0)
    messages = [json.loads(line) for line in stdout.getvalue().decode("utf-8").splitlines() if line.strip()]
    results = [m["id"] for m in messages if m["type"] == "command.result"]
    assert results == ["h", "c", "k", "p"]


def test_slow_commands_enforce_hard_thread_admission_limit_and_preserve_inline_commands() -> None:
    """MAX_SLOW_COMMANDS é aplicado na admissão: nunca cria mais threads que o teto."""
    import threading
    from app.bridge.server import MAX_SLOW_COMMANDS

    entered = threading.Barrier(MAX_SLOW_COMMANDS + 1, timeout=5.0)
    release = threading.Event()
    inline_answered: list[str] = []

    def dispatcher(env: protocol.Envelope, _send) -> dict:
        if env.type == "brain.list":
            entered.wait()
            assert release.wait(5.0)
            if env.id == "list-err":
                raise RuntimeError("boom")
            return {"ok": True}
        inline_answered.append(env.id)
        return {"ok": True}

    stdout = io.BytesIO()
    server = BridgeServer(
        stdin=io.BytesIO(),
        stdout=stdout,
        stderr=io.BytesIO(),
        dispatcher=dispatcher,
        supported_commands={"brain.list", "task.cancel", "task.confirm"},
    )

    def send(identifier: str, kind: str, payload: dict) -> None:
        server.handle_raw_line(
            protocol.encode_message({"v": 1, "id": identifier, "type": kind, "payload": payload}).encode("utf-8")
        )

    send("h", "session.hello", {"client": "test"})
    send("list-err", "brain.list", {})
    for idx in range(1, MAX_SLOW_COMMANDS):
        send(f"list-{idx}", "brain.list", {})

    entered.wait()
    with server._slow_lock:
        assert len(server._slow_threads) == MAX_SLOW_COMMANDS

    # Com os 4 slots ocupados, mais 4 brain.list são recusados imediatamente sem criar threads.
    for extra in range(MAX_SLOW_COMMANDS, MAX_SLOW_COMMANDS * 2):
        send(f"list-{extra}", "brain.list", {})

    with server._slow_lock:
        assert len(server._slow_threads) == MAX_SLOW_COMMANDS

    # Enquanto saturado, task.cancel e task.confirm continuam respondendo inline.
    send("cancel-sat", "task.cancel", {})
    send("confirm-sat", "task.confirm", {"request_id": "cfm-sat", "approved": True})
    assert inline_answered == ["cancel-sat", "confirm-sat"]

    release.set()
    assert server.wait_for_slow_commands(5.0)
    with server._slow_lock:
        assert len(server._slow_threads) == 0

    # Após liberar os slots (inclusive o que lançou exceção), um novo brain.list volta a ser aceito.
    entered_single = threading.Event()

    def dispatcher_after(env: protocol.Envelope, _send) -> dict:
        entered_single.set()
        return {"recovered": True}

    server._dispatcher = dispatcher_after
    send("list-after", "brain.list", {})
    assert server.wait_for_slow_commands(5.0)
    assert entered_single.is_set()

    messages = [json.loads(line) for line in stdout.getvalue().decode("utf-8").splitlines() if line.strip()]
    by_id = {m["id"]: m for m in messages if "id" in m and m["type"] in {"command.result", "command.error"}}

    for extra in range(MAX_SLOW_COMMANDS, MAX_SLOW_COMMANDS * 2):
        err = by_id[f"list-{extra}"]
        assert err["type"] == "command.error"
        assert err["payload"]["code"] == "slow_commands_busy"

    assert by_id["list-err"]["type"] == "command.error"
    assert by_id["list-err"]["payload"]["code"] == "internal_error"
    for idx in range(1, MAX_SLOW_COMMANDS):
        assert by_id[f"list-{idx}"]["type"] == "command.result"
    assert by_id["cancel-sat"]["type"] == "command.result"
    assert by_id["confirm-sat"]["type"] == "command.result"
    assert by_id["list-after"]["type"] == "command.result"
    assert by_id["list-after"]["payload"]["recovered"] is True


def test_subprocess_stdio_only_jsonl_and_no_personal_db_touched(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "app.bridge.server", "--sandbox-dir", str(tmp_path)],
        cwd=str(repo_root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    req = (
        json.dumps({"v": 1, "id": "sub-1", "type": "session.hello", "payload": {"client": "pytest"}})
        + "\n"
        + json.dumps({"v": 1, "id": "sub-2", "type": "app.shutdown", "payload": {}})
        + "\n"
    ).encode("utf-8")
    out, err = proc.communicate(input=req, timeout=10)
    assert proc.returncode == 0, f"stderr={err.decode('utf-8', errors='replace')}"

    out_lines = [line for line in out.decode("utf-8").splitlines() if line.strip()]
    assert len(out_lines) >= 3
    for line in out_lines:
        parsed = json.loads(line)
        assert parsed["v"] == 1
        assert isinstance(parsed["id"], str) and parsed["id"]
        assert isinstance(parsed["type"], str)
        assert isinstance(parsed["payload"], dict)

    ready_evt = next(json.loads(line) for line in out_lines if json.loads(line)["type"] == "session.ready")
    assert ready_evt["payload"]["capabilities"]["converse"] is True
    assert ready_evt["payload"]["capabilities"]["execute"] is True
    assert ready_evt["payload"]["capabilities"]["memory"] is True
    assert ready_evt["payload"]["capabilities"]["brain"] is True
    assert ready_evt["payload"]["capabilities"]["file_read"] is True
    assert ready_evt["payload"]["capabilities"]["web_search"] is False

    # Nenhum banco SQLite deve ser criado apenas para um hello/shutdown (lazy service init)
    assert list(tmp_path.rglob("*.sqlite3")) == []
