"""Local CLI agent runtimes driven through one generic adapter."""

from app.brain.cli.catalog import CLI_SPECS, find_spec, specs
from app.brain.cli.headless import HeadlessCliBrain, extract_json_reply, render_prompt
from app.brain.cli.process import CliProcess, resolve_executable, run_once, spawn, terminate_tree
from app.brain.cli.spec import MODEL_PARSERS, REPLY_PARSERS, CliSpec

__all__ = [
    "CLI_SPECS",
    "CliProcess",
    "CliSpec",
    "HeadlessCliBrain",
    "MODEL_PARSERS",
    "REPLY_PARSERS",
    "extract_json_reply",
    "find_spec",
    "render_prompt",
    "resolve_executable",
    "run_once",
    "spawn",
    "specs",
    "terminate_tree",
]
