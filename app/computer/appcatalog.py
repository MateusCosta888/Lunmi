"""The parent app's view of the machine's applications (F-AL2, F-AL4, F-AL6).

`appindex.py` is the data and the ranking; `appdiscovery.py` is where the data comes from.
This is the third piece, and the only one that has a *lifecycle*: it loads the index when
the chat opens (milliseconds), answers a name from memory, remembers what the user chose or
authorised, and — in a daemon thread that the user never waits for — keeps discovering.

Three properties this module exists to guarantee:

* **Nothing heavy is on the request path.** A name that is already indexed resolves from
  memory. A name that is not gets one *bounded* search (1.5 s ceiling, shallow) and, from
  then on, is indexed. The machine-wide sweep only ever runs in the background, a small
  slice at a time, between pauses.
* **The index is the memory.** A choice the user made (`picks`), an authorisation they gave
  (a swept executable becoming `trusted`) and a name they typed for an application are all
  written to `.local/apps.json`, so the next session starts already knowing them.
* **A stale entry leaves.** An application that was uninstalled or moved is dropped when
  the launch proves its target is gone, so the next request rediscovers it instead of
  failing forever on a ghost path.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from app.computer import appdiscovery, appindex
from app.computer.appindex import AppEntry, AppIndex, Match, AppResolution

# The background loop's own limits, chosen so a machine-wide sweep costs a little CPU and
# I/O spread over minutes instead of the 16 436 ms a full recursive scan was measured at.
# A slice is 2 s of directory reading, then a 2 s pause; when every root has been visited
# the loop goes quiet for three minutes and only re-reads sources whose signature changed.
SLICE_MS = 2000.0
SLICE_PAUSE_SECONDS = 2.0
IDLE_PAUSE_SECONDS = 180.0
# The index is rewritten at most this often when nothing changed, so a slow sweep does not
# turn persistence into disk churn.
SAVE_INTERVAL_SECONDS = 30.0
FOLDERS_NAME = "app-folders.json"


class AppCatalog:
    """The index, its persistence, its memory and its progressive discovery."""

    def __init__(self, path: Path, *, configured: Iterable[str] = (),
                 folders_path: Path | None = None) -> None:
        self.path = Path(path)
        self.folders_path = (Path(folders_path) if folders_path is not None
                             else self.path.parent / FOLDERS_NAME)
        self.cold = True
        self._index = AppIndex()
        self._configured = [text for text in (str(item).strip() for item in configured) if text]
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._saved_at = 0.0

    # -- lifecycle ---------------------------------------------------------------------
    def load(self) -> AppIndex:
        """Read the index and the folders the user added, from disk, in milliseconds."""
        for folder in self._read_folders():
            if folder not in self._configured:
                self._configured.append(folder)
        self._index = AppIndex.load(self.path)
        # "Cold" is the question the metrics ask: was this index built in this session, or
        # did it survive from the last one?
        self.cold = len(self._index) == 0
        return self._index

    def save(self) -> None:
        try:
            self._index.save(self.path)
            self._saved_at = time.perf_counter()
        except OSError:
            # A machine where the index cannot be written still works from memory; the
            # next session pays a rebuild, and the failure is recorded, not swallowed.
            logging.exception("Não foi possível gravar o índice de aplicativos")

    @property
    def index(self) -> AppIndex:
        return self._index

    @property
    def configured(self) -> tuple[str, ...]:
        return tuple(self._configured)

    def start(self) -> None:
        """Begin progressive discovery in the background. Safe to call twice."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="app-discovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)

    # -- resolution and memory ---------------------------------------------------------
    def resolve(self, name: str) -> AppResolution:
        return self._index.resolve(name)

    def find(self, query: str, limit: int = 5) -> list[Match]:
        return self._index.find(query, limit)

    def entry(self, entry_id: str) -> AppEntry | None:
        return self._index.by_id(entry_id)

    def learn(self, query: str, entry_id: str) -> None:
        """Remember that the user answered `query` with this entry (F-AL4)."""
        if not query or not entry_id:
            return
        with self._lock:
            self._index = self._index.learned(query, entry_id)
        self.save()

    def trust(self, entry_id: str) -> None:
        """Remember that the user authorised launching this entry (F-AL6)."""
        if not entry_id:
            return
        with self._lock:
            self._index = self._index.trusted(entry_id)
        self.save()

    def forget(self, entry_id: str) -> None:
        """Drop an entry whose target is gone, so discovery can find it again."""
        if not entry_id:
            return
        with self._lock:
            self._index = self._index.forget(entry_id)
        self.save()

    # -- discovery (Windows, bounded) ---------------------------------------------------
    def search(self, name: str, *, budget_ms: float = appdiscovery.SEARCH_BUDGET_MS) -> float:
        """One bounded search for a name the index does not carry; returns the ms it cost.

        Shallow and time-limited on purpose: it is allowed to miss, because the background
        sweep keeps making progress and the user can always point at the folder.
        """
        started = time.perf_counter()
        found = appdiscovery.search_for(name, budget_ms=budget_ms, configured=self._configured)
        if found:
            with self._lock:
                self._index = self._index.with_entries([*self._index.entries, *found])
            # Stored, not just used: the next request is a lookup, which is the whole
            # point of searching once (F-AL6).
            self.save()
        return (time.perf_counter() - started) * 1000

    def refresh(self, *, force: bool = False, include_sweep: bool = False,
                budget_ms: float = appdiscovery.SWEEP_BUDGET_MS) -> float:
        """Update the index incrementally and persist it; returns the ms it cost."""
        started = time.perf_counter()
        with self._lock:
            updated = appdiscovery.refresh_index(self._index, configured=self._configured,
                                                 force=force, include_sweep=include_sweep,
                                                 budget_ms=budget_ms)
            self._index = updated
        self.cold = False
        self.save()
        return (time.perf_counter() - started) * 1000

    def sweep_slice(self, *, budget_ms: float = SLICE_MS) -> bool:
        """One bounded slice of the machine-wide sweep; True while a root is unfinished."""
        found, sweeps = appdiscovery.sweep_step(self._index, budget_ms=budget_ms,
                                                configured=self._configured)
        with self._lock:
            if found:
                self._index = self._index.with_entries([*self._index.entries, *found])
            self._index = _with_sweeps(self._index, sweeps)
        self._save_if_due(bool(found))
        roots = (sweeps.get("roots") or {})
        return any(not item.get("finished_ms") for item in roots.values())

    def adopt(self, value: str) -> list[AppEntry]:
        """Take a folder or an executable the user pointed at into the index (F-AL6).

        A folder is remembered as one of the user's own places (swept again later) and
        scanned once, bounded; an executable becomes one entry. Both are marked
        `user_folder`, so the first launch still asks — the path is theirs, the program is
        unknown.
        """
        text = str(value or "").strip().strip('"')
        if not text:
            return []
        path = Path(text).expanduser()
        collected: list[AppEntry] = []
        if path.is_dir():
            collected = appdiscovery.discover_configured([str(path)])
            if str(path) not in self._configured:
                self._configured.append(str(path))
                self._write_folders()
        elif path.is_file():
            entry = appdiscovery.entry_for_path(str(path))
            if entry is not None:
                collected = [entry]
        if collected:
            with self._lock:
                self._index = self._index.with_entries([*self._index.entries, *collected])
            self.save()
        return collected

    # -- internals ---------------------------------------------------------------------
    def _save_if_due(self, changed: bool) -> None:
        if changed or time.perf_counter() - self._saved_at >= SAVE_INTERVAL_SECONDS:
            self.save()

    def _read_folders(self) -> list[str]:
        try:
            value = json.loads(self.folders_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if isinstance(item, str) and str(item).strip()]

    def _write_folders(self) -> None:
        try:
            self.folders_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.folders_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._configured, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.folders_path)
        except OSError:
            logging.exception("Não foi possível gravar as pastas de aplicativos do usuário")

    def _run(self) -> None:
        """The background loop: structured sources first, then the sweep, gently."""
        try:
            self.refresh(force=True, include_sweep=False)
        except Exception:  # noqa: BLE001 - a discovery failure must not kill the thread
            logging.exception("Falha na descoberta inicial de aplicativos")
        while not self._stop.is_set():
            pending = False
            try:
                pending = self.sweep_slice()
            except Exception:  # noqa: BLE001 - one bad slice does not end discovery
                logging.exception("Falha em uma fatia da varredura de aplicativos")
            if not pending and not self._stop.is_set():
                # Everything found: only a source whose own signature changed is read
                # again, so an application installed later appears on the next pass
                # instead of on a rebuild.
                try:
                    self.refresh(force=False, include_sweep=True)
                except Exception:  # noqa: BLE001 - same reasoning as above
                    logging.exception("Falha ao atualizar o índice de aplicativos")
            self._stop.wait(SLICE_PAUSE_SECONDS if pending else IDLE_PAUSE_SECONDS)


def _with_sweeps(index: AppIndex, sweeps: dict) -> AppIndex:
    """Fold sweep progress back into the index, where it survives closing the app."""
    return replace(index, sweeps=dict(sweeps), updated_ms=appindex.now_ms())
