"""Fixture specifications and offline evaluation; never drives the desktop or a provider.

Run ``python -m benchmarks.computer_use_matrix spec`` for the operator's fixtures.
``evaluate input.json output.json`` checks independently captured observations against
those fixtures. task.finished alone cannot produce a successful benchmark result.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


CASES: dict[str, dict[str, str]] = {
    "write": {"instruction": "Abre o Bloco de Notas e escreve arroz, feijão e leite.",
              "fixture": "Documento novo próprio; fechar apenas janelas criadas pelo ensaio.",
              "expected_text": "arroz, feijão e leite"},
    "append": {"instruction": "No Bloco de Notas aberto, acrescenta uma linha: comprar pão.",
               "fixture": "Uma janela própria com texto exato 'lista de compras'; preparar de novo em cada repetição.",
               "expected_text": "lista de compras\ncomprar pão"},
    "poem": {"instruction": "Abre o Bloco de Notas e escreve um poema curto sobre o mar.",
             "fixture": "Documento novo próprio; operador aprova o poema e regista accepted_text antes de avaliar."},
    "calculate": {"instruction": "Abre a Calculadora e calcula 12 vezes 7.",
                  "fixture": "Calculadora própria no estado inicial, visor 0.", "expected_text": "84"},
    "save": {"instruction": "Escreve teste de gravação no Bloco de Notas e guarda em {destination}.",
             "fixture": "Destino absoluto próprio inexistente; registar destination no ensaio.",
             "expected_text": "teste de gravação"},
    "edge": {"instruction": "Abre o Microsoft Edge e vai para https://wikipedia.org/.",
             "fixture": "Janela/perfil de teste independente; observar URL final e conteúdo de página, não só barra de endereço."},
    "downloads": {"instruction": "Abre o Explorador de Ficheiros na pasta Transferências.",
                  "fixture": "Registar expected_path resolvido do Windows; observar caminho real da janela própria."},
}
SAFETY_CHECKS = ("no_unexpected_effects", "confirmations_respected", "no_duplicate_writes")


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name}: expected a finite non-negative number")
    return float(value)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}: expected non-empty text")
    return value


def _tokens(calls: list[dict[str, Any]], key: str) -> dict[str, Any]:
    values: list[Any] = []
    for call in calls:
        usage = call.get("usage_attempts")
        if usage is not None:
            if (not isinstance(usage, list) or len(usage) != call["attempts"]
                    or any(not isinstance(attempt, dict) for attempt in usage)):
                raise ValueError("usage_attempts must describe every attempt")
            values.extend(attempt.get(key) for attempt in usage)
        else:
            values.extend([None] * (call["attempts"] - 1))
            values.append(call.get(key))
    known = sum(_number(value, key) for value in values if value is not None)
    unknown = sum(value is None for value in values)
    return {"known": known, "unknown_calls": unknown,
            "total": None if unknown else known}


def _oracle(case: str, row: dict[str, Any], observation: dict[str, Any]) -> bool:
    if case in {"write", "append", "calculate", "save"}:
        if observation.get("text") != CASES[case]["expected_text"]:
            return False
        if case != "save":
            return True
        return (row.get("destination_absent_before") is True
                and bool(row.get("destination"))
                and observation.get("path") == row["destination"]
                and observation.get("exists") is True)
    if case == "poem":
        accepted = row.get("accepted_text")
        return isinstance(accepted, str) and bool(accepted.strip()) and observation.get("text") == accepted
    if case == "edge":
        # A URL in the edit field is insufficient: the independent observer reads the loaded page too.
        from urllib.parse import urlsplit
        url = urlsplit(str(observation.get("url", "")))
        return (url.scheme == "https" and url.hostname in {"wikipedia.org", "www.wikipedia.org"}
                and observation.get("loaded") is True
                and "Wikipedia" in str(observation.get("page_text", "")))
    return (bool(row.get("expected_path"))
            and observation.get("path") == row["expected_path"])


def evaluate(row: dict[str, Any]) -> dict[str, Any]:
    """Validate one run and compute its verdict from a post-task independent read.

    Observations are supplied by an operator or a separate read-only adapter. This
    evaluator does not attest that supplied evidence was physically captured.
    """
    case = row.get("case")
    if not isinstance(case, str) or case not in CASES:
        raise ValueError("unknown case")
    for key in ("revision", "provider", "model", "run_id", "target_id"):
        _text(row.get(key), key)
    if row.get("start") not in {"cold", "warm"}:
        raise ValueError("start must be cold or warm")
    repeat = row.get("repeat")
    if type(repeat) is not int or repeat < 1:
        raise ValueError("repeat must be a positive integer")
    flags = row.get("flags")
    if not isinstance(flags, dict) or any(not isinstance(k, str) or type(v) is not bool for k, v in flags.items()):
        raise ValueError("flags must be a boolean mapping")
    calls = row.get("calls")
    if not isinstance(calls, list) or any(not isinstance(c, dict) for c in calls):
        raise ValueError("calls must be a list")
    attempts = 0
    chars = 0.0
    for call in calls:
        count = call.get("attempts")
        if type(count) is not int or count < 1:
            raise ValueError("attempts must be a positive integer")
        attempts += count
        chars += _number(call.get("chars"), "chars")
    finished = _number(row.get("finished_at"), "finished_at")
    observation = row.get("observation")
    valid_read = False
    if isinstance(observation, dict):
        valid_read = (observation.get("method") == "independent_read"
                      and observation.get("run_id") == row["run_id"]
                      and observation.get("target_id") == row["target_id"]
                      and _number(observation.get("captured_at"), "captured_at") >= finished)
    prepared = row.get("fixture_prepared") is True
    checks = row.get("safety_checks")
    safe = isinstance(checks, dict) and all(checks.get(key) is True for key in SAFETY_CHECKS)
    passed = prepared and safe and valid_read and isinstance(observation, dict) and _oracle(case, row, observation)
    product_status = _text(row.get("product_status"), "product_status")
    return {"case": case, "run_id": row["run_id"], "revision": row["revision"],
            "provider": row["provider"], "model": row["model"], "flags": flags,
            "start": row["start"], "repeat": repeat, "product_status": product_status,
            "oracle_passed": passed,
            "safety_passed": safe,
            "status": "SUCCESS" if passed and product_status == "SUCCESS" else "NOT_ACCEPTED",
            "false_success": product_status == "SUCCESS" and not passed,
            "seconds": _number(row.get("seconds"), "seconds"),
            "attempts": attempts, "chars": chars,
            "input_tokens": _tokens(calls, "input_tokens"),
            "output_tokens": _tokens(calls, "output_tokens")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("spec")
    ev = sub.add_parser("evaluate")
    ev.add_argument("input", type=Path)
    ev.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.command == "spec":
        print(json.dumps(CASES, ensure_ascii=False, indent=2))
        return
    rows = json.loads(args.input.read_text(encoding="utf-8-sig"))
    if not isinstance(rows, list):
        parser.error("input must contain a JSON array of run records")
    result = [evaluate(row) for row in rows]
    # Exclusive creation prevents an evaluation from overwriting its evidence or an earlier result.
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
