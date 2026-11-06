"""Raciocínio e modo rápido do Codex, escolhidos nas configurações do cérebro.

Ficheiro próprio: `brain-selection.json` tem um esquema fechado. Cada turno lê-o, por isso a
escolha vale logo na mensagem seguinte, sem reiniciar o cérebro. Vazio é o padrão do modelo.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

EFFORTS = ("", "low", "medium", "high", "xhigh", "max", "ultra")
FILENAME = "codex-options.json"


@dataclass(frozen=True)
class CodexOptions:
    effort: str = ""
    fast: bool = False


def load_codex_options(state_root: str | Path) -> CodexOptions:
    """Um ficheiro em falta ou estranho nunca impede uma resposta: fica o padrão do modelo."""
    try:
        raw = json.loads((Path(state_root) / ".local" / FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return CodexOptions()
    if not isinstance(raw, dict):
        return CodexOptions()
    effort = raw.get("effort")
    return CodexOptions(effort if effort in EFFORTS else "", raw.get("fast") is True)


def save_codex_options(state_root: str | Path, options: CodexOptions) -> None:
    path = Path(state_root) / ".local" / FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(options)), encoding="utf-8")
    temporary.replace(path)
