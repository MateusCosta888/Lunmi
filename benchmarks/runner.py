from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.controller import TaskController
from app.main import configure_logging
from app.llm import Plan
from app.settings import ROOT, Settings


async def run(repeat: int, engine_only: bool, task_id: str | None = None) -> int:
    settings = Settings.load()
    settings.validate(credentials=not engine_only)
    configure_logging(settings)
    tasks = json.loads((ROOT / "benchmarks" / "tasks.json").read_text(encoding="utf-8"))
    if task_id:
        tasks = [task for task in tasks if task["id"] == task_id]
        if not tasks:
            raise ValueError("Benchmark ID desconhecido.")
    controller = TaskController(settings)
    run_id = uuid.uuid4().hex
    output = settings.logs_path / f"benchmark-{run_id}.jsonl"
    failed = False
    try:
        for iteration in range(repeat):
            for task in tasks:
                instruction = task["instruction"].format(run_id=run_id, iteration=iteration + 1)
                payload = dict(task["plan"])
                if "save_path" in payload:
                    payload["save_path"] = payload["save_path"].format(run_id=run_id, iteration=iteration + 1)
                result = await controller.run(instruction, engine_only=engine_only,
                                              engine_plan=Plan.model_validate(payload) if engine_only else None)
                record = asdict(result)
                record.pop("message")
                record.update(benchmark_id=task["id"], iteration=iteration + 1, run_id=run_id)
                with output.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record) + "\n")
                print(f"{task['id']} #{iteration + 1}: {result.status} {result.duration_ms:.0f} ms")
                failed |= result.status != "SUCCESS"
                if result.status == "CANCELLED":
                    return 1
    finally:
        await controller.close()
    print(f"Results: {output}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Real Notepad open/write/save benchmarks; leaves test documents open.")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--engine-only", action="store_true", help="No LLM: NEVER counted as end-to-end success")
    parser.add_argument("--task", choices=("T01", "T02_WRITE", "T03_SAVE"), help="Limit to one milestone")
    args = parser.parse_args()
    if not 1 <= args.repeat <= 20:
        parser.error("--repeat must be between 1 and 20")
    return asyncio.run(run(args.repeat, args.engine_only, args.task))


if __name__ == "__main__":
    raise SystemExit(main())
