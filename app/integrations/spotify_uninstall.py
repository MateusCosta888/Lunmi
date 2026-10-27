"""Remoção Spotify no desinstalador: o MESMO job consentido, nunca um segundo algoritmo.

Chamado pelo NSIS antes de apagar `resources/`. Sem Spotify/Lunmi configurado não há
nada a remover e a desinstalação continua. Um job falhado, cancelado ou adiado devolve
código 2: o desinstalador para e conserva estado/backup, em vez de afirmar limpeza.

Sem `--approved` (consentimento separado, depois do aviso NSIS) uma ligação
configurada devolve 2 sem plano nem efeito.

Uso: python app/integrations/spotify_uninstall.py --state-root <LOCALAPPDATA>\\Lunmi --approved
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Executado como script pelo runtime empacotado; a raiz do payload é `resources/app`.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.integrations.spotify import SpotifyBroker  # noqa: E402
from app.integrations.spotify_install import (  # noqa: E402
    CLI_APPLY_TIMEOUT_S,
    SpotifyInstallError,
    SpotifyInstaller,
)


def _report(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--approved", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        _report({"removed": False, "reason": "invalid_arguments"})
        return 2
    root = Path(args.state_root).expanduser().resolve()
    broker = SpotifyBroker(state_root=root, session_id="uninstall")
    installer = SpotifyInstaller(state_root=root, broker=broker, session_id="uninstall",
                                emit=lambda _payload: None)
    try:
        # Sem app configurada não há transação a iniciar: a desinstalação prossegue e
        # o estado/backup do utilizador ficam intactos (`deleteAppDataOnUninstall` false).
        if installer.state().get("setup") in {"absent", "not_configured"}:
            _report({"removed": False, "reason": "not_configured"})
            return 0
        if not args.approved:
            # Consentimento separado, posterior ao aviso do NSIS: sem ele não existe
            # plano de remoção nem efeito, mesmo com a ligação configurada.
            _report({"removed": False, "reason": "consent_required"})
            return 2
        try:
            prepared = installer.plan("remove")
        except SpotifyInstallError as exc:
            _report({"removed": False, "reason": exc.code})
            return 2
        accepted = installer.apply(prepared["consent"]["consent_id"])
        job_id = str(accepted.get("job_id") or "")
        # O limite cobre o timeout do CLI pin; não se abandona um filho vivo.
        deadline = time.monotonic() + float(CLI_APPLY_TIMEOUT_S + 30)
        while time.monotonic() < deadline:
            job = installer.state().get("job") or {}
            if job.get("status") != "running":
                break
            time.sleep(0.2)
        job = installer.state().get("job") or {}
        if job.get("status") == "running" and job_id:
            # Cancelar mata/aguarda o CLI próprio e dá ao rollback uma última janela.
            installer.cancel(job_id)
            grace = time.monotonic() + 15.0
            while time.monotonic() < grace:
                job = installer.state().get("job") or {}
                if job.get("status") != "running":
                    break
                time.sleep(0.2)
        job = installer.state().get("job") or {}
        if job.get("status") == "done":
            _report({"removed": True, "reason": "done"})
            return 0
        _report({"removed": False,
                 "reason": job.get("error_code") or job.get("status") or "not_finished"})
        return 2
    finally:
        try:
            installer.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
