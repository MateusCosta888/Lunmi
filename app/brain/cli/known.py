"""Local CLIs this application can *find* but must not drive as a brain.

A ``CliSpec`` is a promise that the generic adapter can run one non-interactive
inference turn (see ``catalog.py``). Some installed CLIs have no such contract:
``free-pi-cli`` (0.6.7) only starts an interactive TUI — its own ``--help`` lists
session flags, ``logout``, ``mcp``, ``--version`` and ``--help``; any other
argument is rejected with exit code 2 (``--print`` included). Declaring it here
keeps that promise separate:

* ``Settings``/the registry can list it as a known runtime, disabled with a reason;
* detection can report whether the binary is installed and its version;
* ``CLI_SPECS`` stays the catalogue of runtimes the adapter may spawn, so no
  accidental inference call, TUI launch or invented flag can come from this entry.

This is deliberately not a generic "any CLI" mechanism: a runtime is added by
name with its verified executable and its verified lack of a headless contract.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KnownRuntime:
    """A real local CLI together with the reason it cannot be a brain."""

    id: str
    label: str
    executable: str
    search_paths: tuple[str, ...] = ()
    version_args: tuple[str, ...] = ("--version",)
    # Shown to the user when this runtime is selected. Never empty: "known but
    # not usable" without a reason is indistinguishable from a broken provider.
    unavailable_reason: str = ""


KNOWN_RUNTIMES: dict[str, KnownRuntime] = {
    "freepi": KnownRuntime(
        id="freepi",
        label="Free Pi CLI",
        executable="free-pi-cli",
        # npm's global bin, where the package installs itself on Windows.
        search_paths=("~/AppData/Roaming/npm",),
        version_args=("--version",),
        unavailable_reason=(
            "Esta versão só conversa pela interface interativa. Não disponibiliza um modo "
            "headless para responder à Lunmi em segundo plano."
        ),
    ),
}


def known_runtimes() -> list[KnownRuntime]:
    """Every local CLI that is recognisable but has no headless contract."""
    return list(KNOWN_RUNTIMES.values())


def known_runtime(cli_id: str) -> KnownRuntime | None:
    """Return the known non-headless runtime for one id, or ``None``."""
    return KNOWN_RUNTIMES.get(cli_id)
