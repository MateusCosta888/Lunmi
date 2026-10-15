"""Private JSONL worker. All mutations use official UFO MCP or AtomicCommand.

No arbitrary shell, mouse coordinates, screenshots or alternate computer engine.
UIA TextPattern and file reads are used only as independent evidence.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from contextlib import AsyncExitStack, redirect_stdout
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
setattr(sys, "coinit_flags", 0)


def check_source(path: Path) -> None:
    manifest = json.loads((ROOT / "ufo.lock.json").read_text(encoding="utf-8"))
    for filename, digest in manifest["sha256"].items():
        source = path / filename
        if not source.is_file() or hashlib.sha256(source.read_bytes().replace(b"\r\n", b"\n")).hexdigest() != digest:
            raise RuntimeError("Fonte UFO divergente do pin. Execute python install_ufo.py.")


def literal_keys(text: str) -> str:
    """Literal pywinauto syntax. Never let document text become hotkeys."""
    escaped = {"\n": "{ENTER}", "\t": "{TAB}"}
    return "".join(escaped.get(c, "{" + c + "}" if c in "+^%{}()" else c) for c in text)


async def serve() -> None:
    output = sys.stdout
    with redirect_stdout(sys.stderr):
        ufo_path = Path(os.environ.get("UFO_PATH", str(ROOT / "vendor" / "UFO")))
        output_path = Path(os.environ.get("OUTPUT_DIRECTORY", str(ROOT / "output"))).resolve()
        check_source(ufo_path)
        sys.path.insert(0, str(ufo_path))
        os.chdir(ufo_path)
        from app.safety import check_approved_file, normalized_text, verify_saved_file
        from config.config_loader import get_ufo_config
        config = get_ufo_config()
        config.system.show_visual_outline_on_screen = False
        config.system.maximize_window = False
        config.system.input_text_api = "type_keys"
        config.system.input_text_inter_key_pause = 0
        config.system.sleep_time = 0
        # Two attempts, and the second one restates the JSON protocol (see
        # `ufo_runtime.REPLY_PROTOCOL_NUDGE`): a model that decorates its answer once is
        # asked again instead of the round ending with zero actions. Measured 2026-10-09:
        # with a single attempt, an unreadable reply was the whole task — 26 s of inference
        # and no action, reported as a generic "fluxo encerrado".
        config.system.json_parsing_retry = 2
        config.system.enabled_third_party_agents = []
        config.system.include_last_screenshot = False
        config.system.screenshot_to_memory = False
        config.system.action_sequence = True
        config.host_agent.visual_mode = False
        config.app_agent.visual_mode = False
        config.rag.offline_docs = config.rag.online_search = False
        config.rag.experience = config.rag.demonstration = False
        from fastmcp import Client
        from ufo.client.mcp.local_servers.ui_mcp_server import (
            create_data_mcp_server, create_host_action_mcp_server, UIServerState,
        )
        from ufo.automator.ui_control.controller import AtomicCommand, ControlReceiver
        import psutil

        logging.getLogger().setLevel(logging.WARNING)
        selected: Any = None
        editor: Any = None
        expected_text: str | None = None
        saved_path: Path | None = None
        state = UIServerState()

        def atomic(control: Any, method: str, arguments: dict[str, Any]) -> None:
            # This is UFO's original native/UIA command, not direct pywinauto mutation.
            result = AtomicCommand(ControlReceiver(control, selected), method, arguments).execute()
            if isinstance(result, str) and (result.startswith("An error occurred") or "doesn't have a method named" in result):
                raise RuntimeError("Executor UFO não conseguiu aplicar a ação.")

        def read_document() -> str:
            if editor.element_info.control_type == "Document":
                return normalized_text(editor.iface_text.DocumentRange.GetText(-1))
            return normalized_text(editor.get_value())

        def controls(window: Any, types: list[str]) -> list[Any]:
            return state.control_inspector.find_control_elements_in_descendants(window, control_type_list=types)

        def save_dialog() -> Any:
            dialogs = [c for c in controls(selected, ["Window"]) if c.class_name() == "#32770"]
            return next((d for d in dialogs if any(c.element_info.automation_id == "1001"
                         for c in controls(d, ["Edit"]))), None)

        async with AsyncExitStack() as stack:
            collector = await stack.enter_async_context(Client(create_data_mcp_server()))
            host = await stack.enter_async_context(Client(create_host_action_mcp_server()))

            async def observe() -> list[dict[str, Any]]:
                await collector.call_tool("get_desktop_app_info", {"remove_empty": True, "refresh_app_windows": True})
                windows: list[dict[str, Any]] = []
                for label, window in (state.last_app_windows or {}).items():
                    try:
                        pid = window.process_id()
                        name = psutil.Process(pid).name()
                        if name.casefold() == "notepad.exe" and window.is_visible():
                            windows.append({"handle": int(window.handle), "pid": pid,
                                            "process": name, "visible": True, "id": label})
                    except psutil.NoSuchProcess:
                        continue
                return windows

            from app.computer.ufo_runtime import UFORuntime, set_assistant_host_pids

            async def exchange(event: dict[str, Any]) -> dict[str, Any]:
                output.write(json.dumps(event, ensure_ascii=True) + "\n")
                output.flush()
                reply = json.loads(await asyncio.to_thread(sys.stdin.readline))
                if reply.get("error"):
                    raise RuntimeError(reply["error"])
                return reply

            runtime = UFORuntime(exchange)
            output.write(json.dumps({"ready": True}) + "\n")
            output.flush()
            while True:
                line = await asyncio.to_thread(sys.stdin.readline)
                if not line:
                    break
                start = time.perf_counter()
                try:
                    request = json.loads(line)
                    if "host_pids" in request:
                        set_assistant_host_pids(request.get("host_pids"))
                    operation = request.get("operation")
                    if operation == "run_task":
                        # The approval mode is the user's setting, carried by the request:
                        # the worker never decides it on its own.
                        data = await runtime.run(request["instruction"], request["max_steps"],
                                                 approval_mode=str(request.get("approval_mode") or ""))
                    elif operation == "fast_path":
                        # FASE 8: a plan resolved by a table, executed with no inference.
                        # Its own outcomes (declined/failed) travel inside `data`, so a
                        # plan that did not apply never looks like a broken worker.
                        data = await runtime.fast_path(request.get("plan") or {})
                    elif operation == "open_notepad":
                        selected = editor = None
                        expected_text = None
                        saved_path = None
                        t0 = time.perf_counter()
                        before = await observe()
                        observation_ms = (time.perf_counter() - t0) * 1000
                        t0 = time.perf_counter()
                        await runtime.tools.launch("notepad.exe")
                        data = {"before": before, "observation_time_ms": observation_ms,
                                "execution_time_ms": (time.perf_counter() - t0) * 1000,
                                "actions": 1, "steps": 1}
                        # Bind subsequent actions to exactly one newly observed Notepad.
                        previous = {(w["handle"], w["pid"]) for w in before}
                        fresh = ([w for w in await observe() if (w["handle"], w["pid"]) not in previous]
                                 if request.get("prepare_document") is True else [])
                        if len(fresh) == 1:
                            chosen = fresh[0]
                            selected = state.last_app_windows[chosen["id"]]
                            await host.call_tool("select_application_window", {
                                "id": chosen["id"], "name": selected.element_info.name})
                            editors = [c for c in controls(selected, ["Document", "Edit"])
                                       if c.element_info.class_name in {"RichEditD2DPT", "Edit"}]
                            if len(editors) == 1:
                                editor = editors[0]
                        data["document_bound"] = editor is not None
                    elif operation == "write_notepad":
                        text = request.get("text")
                        if not isinstance(text, str) or not 1 <= len(text) <= 4000:
                            raise ValueError("Texto inválido.")
                        if any(ord(c) < 32 and c not in "\n\t" for c in text):
                            raise ValueError("Caracteres de controle inválidos.")
                        if selected is None or editor is None or expected_text is not None:
                            raise ValueError("Não há documento novo autorizado para escrita.")
                        if read_document() != "":
                            raise ValueError("Documento não está vazio: escrita bloqueada para proteger conteúdo existente.")
                        # Direct native edit API where exposed; modern RichEdit Document
                        # lacks ValuePattern, so UFO AtomicCommand types literal Unicode.
                        if hasattr(editor, "set_edit_text"):
                            atomic(editor, "set_edit_text", {"text": text})
                        else:
                            atomic(editor, "type_keys", {"keys": literal_keys(text), "pause": 0.02,
                                                          "with_spaces": True, "vk_packet": True})
                        expected_text = normalized_text(text)
                        deadline = time.monotonic() + 2
                        while read_document() != expected_text and time.monotonic() < deadline:
                            await asyncio.sleep(0.05)
                        if read_document() != expected_text:
                            raise RuntimeError("Texto real do documento diverge do solicitado; não salvar.")
                        data = {"actions": 1, "steps": 1, "execution_time_ms": (time.perf_counter() - start) * 1000}
                    elif operation == "save_notepad":
                        if selected is None or editor is None or expected_text is None:
                            raise ValueError("Não há documento novo verificado para salvar.")
                        target = check_approved_file(output_path, request["path"], request.get("approved_version"))
                        if read_document() != expected_text:
                            raise ValueError("Documento mudou após escrita; salvamento bloqueado.")
                        atomic(editor, "type_keys", {"keys": "^+s", "pause": 0.02})
                        deadline = time.monotonic() + 4
                        dialog = save_dialog()
                        while dialog is None and time.monotonic() < deadline:
                            await asyncio.sleep(0.05)
                            dialog = save_dialog()
                        if dialog is None:
                            raise RuntimeError("Caixa Save As não encontrada.")
                        fields = controls(dialog, ["Edit", "Button"])
                        filename = next(c for c in fields if c.element_info.automation_id == "1001"
                                        and c.element_info.control_type == "Edit")
                        save = next(c for c in fields if c.element_info.automation_id == "1"
                                    and c.element_info.control_type == "Button")
                        atomic(filename, "set_edit_text", {"text": str(target)})
                        if filename.get_value() != str(target):
                            raise RuntimeError("Destino real no diálogo diverge do autorizado.")
                        check_approved_file(output_path, str(target), request.get("approved_version"))
                        atomic(save, "invoke", {})
                        # Native overwrite prompt is accepted only with exact approval.
                        deadline = time.monotonic() + 4
                        while time.monotonic() < deadline:
                            dialogs = [c for c in controls(selected, ["Window"]) if c.class_name() == "#32770"]
                            yes = next((c for d in dialogs for c in controls(d, ["Button"])
                                        if c.element_info.automation_id in {"6", "CommandButton_6"}), None)
                            if yes is not None:
                                if request.get("approved_version") is None:
                                    raise ValueError("Arquivo apareceu após confirmação: sobrescrita não autorizada.")
                                check_approved_file(output_path, str(target), request["approved_version"])
                                atomic(yes, "invoke", {})
                            if not dialogs and verify_saved_file(target, expected_text)["file_verified"]:
                                break
                            await asyncio.sleep(0.05)
                        if save_dialog() is not None:
                            raise RuntimeError("Salvamento não encerrou o diálogo; tarefa interrompida sem sucesso.")
                        saved_path = target
                        data = {"actions": 3 + int(request.get("approved_version") is not None),
                                "steps": 1, "execution_time_ms": (time.perf_counter() - start) * 1000}
                    elif operation == "verify_notepad":
                        windows = await observe()
                        document_ok = False
                        if editor is not None and expected_text is not None:
                            document_ok = read_document() == expected_text
                        data = {"windows": windows, "text_verified": document_ok,
                                "verification_time_ms": (time.perf_counter() - start) * 1000}
                        if saved_path and expected_text is not None:
                            data.update(verify_saved_file(saved_path, expected_text))
                    else:
                        raise ValueError("Operação não autorizada no worker.")
                    response = {"ok": True, "data": data}
                except Exception:
                    logging.exception("Falha no worker UFO")
                    response = {"ok": False, "error": "UFO falhou; nenhuma próxima ação será iniciada. Consulte logs/worker.log."}
                output.write(json.dumps(response, ensure_ascii=True) + "\n")
                output.flush()
            await runtime.close()


if __name__ == "__main__":
    try:
        asyncio.run(serve())
    except Exception:
        logging.exception("Falha ao inicializar UFO")
        sys.exit(1)
