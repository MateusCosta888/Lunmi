from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Support both python app/main.py and python -m app.main.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.controller import TaskController
from app.settings import ConfigurationError, Settings


def configure_logging(settings: Settings) -> None:
    settings.logs_path.mkdir(exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[RotatingFileHandler(settings.logs_path / "app.log", maxBytes=2_000_000,
                                      backupCount=2, encoding="utf-8")],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def command(settings: Settings, instruction: str, engine_only: bool) -> int:
    controller = TaskController(settings)
    try:
        result = await controller.run(instruction, engine_only=engine_only)
        print(json.dumps(asdict(result), ensure_ascii=True, indent=2))
        return 0 if result.status == "SUCCESS" else 1
    finally:
        await controller.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows Agent — HostAgent/AppAgent UFO²")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--instruction", help="Executa uma tarefa por API + UFO real, sem abrir o chat")
    group.add_argument("--engine-check", action="store_true", help="Abre Notepad via UFO real, SEM LLM; não valida o milestone completo")
    arguments = parser.parse_args()
    try:
        settings = Settings.load()
        settings.validate(credentials=False)
        configure_logging(settings)
        if arguments.instruction or arguments.engine_check:
            return asyncio.run(command(settings, arguments.instruction or "open_notepad", arguments.engine_check))
        import tkinter as tk
        from app.ui.chat_window import ChatWindow
        root = tk.Tk()
        ChatWindow(root, settings)
        root.mainloop()
        return 0
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
