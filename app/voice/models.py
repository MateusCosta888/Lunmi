"""Voice models downloaded after installation: pinned sources, resumable, verified before use.

The setup ships the voice runtime but not its ~2 GB of models (user request, 01/11: the setup
was 18 MB under the 2 GiB limit). They are fetched on request into
``<state_root>/.local/voice-models`` from the URLs, sizes and SHA-256 pinned in
``VOICE-SOURCE.json``, in two groups the user picks separately: the Lunmi voice (speech) and
dictation.

Rules, each guarding a failure seen or expected on users' machines:
- only pinned URLs, over HTTPS, to the model hosts (redirects included);
- a file is written to an isolated ``.part``, resumed with ``Range`` + ``If-Range`` so changed
  content restarts instead of being stitched, and checked for size and SHA-256 before use;
- a group is activated atomically: files move into place, then a marker is written last, so a
  crash never leaves a half group that looks installed;
- free space is checked first; cancel keeps the ``.part`` for resume; repair re-hashes all;
- folders that are links/junctions are refused (writes must stay in the user's data);
- models of an older install (inside a previous voice component) are adopted only after their
  hashes are verified, then that copy is removed.
Nothing here opens the microphone or plays audio.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

GROUPS: dict[str, tuple[str, ...]] = {
    "speech": ("models/supertonic-3/",),
    "dictation": ("models/whisper-large-v3-turbo/", "models/silero/"),
}
MODELS_DIRNAME = "voice-models"
MARKER = ".lunmi-verified"
STAGING = ".staging"
CHUNK = 1 << 20
# Room for the staging-to-final moves and the filesystem itself, beyond the bytes to fetch.
SPACE_MARGIN = 256 << 20
TIMEOUT_S = 30.0
RETRIES = 2
MODEL_HOST_SUFFIXES = ("huggingface.co", "hf.co", "raw.githubusercontent.com")

MESSAGE_NO_NETWORK = "Sem ligação aos servidores dos modelos. A transferência retoma onde parou."
MESSAGE_NO_SPACE = "Espaço em disco insuficiente para os modelos de voz."
MESSAGE_CORRUPT = "Um ficheiro chegou diferente do esperado e foi descartado; tenta de novo."
MESSAGE_LINKED = "A pasta dos modelos é uma ligação para outro sítio; a transferência foi recusada."
MESSAGE_SOURCE = "A origem de um modelo não é a fixada pela Lunmi; a transferência foi recusada."
MESSAGE_MANIFEST = "A lista de modelos desta versão está em falta ou inválida."


class ModelsError(RuntimeError):
    """A download or verification failure with a message safe to show."""


@dataclass(frozen=True)
class ModelFile:
    path: str
    url: str
    sha256: str
    bytes: int


def load_manifest(path: Path) -> dict[str, list[ModelFile]]:
    """The pinned files of each group, from VOICE-SOURCE.json; raises on anything malformed."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        files = [ModelFile(str(item["path"]), str(item["url"]), str(item["sha256"]).lower(), int(item["bytes"]))
                 for item in data["files"]]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ModelsError(MESSAGE_MANIFEST) from exc
    groups: dict[str, list[ModelFile]] = {name: [] for name in GROUPS}
    for item in files:
        if ".." in Path(item.path).parts or Path(item.path).is_absolute():
            raise ModelsError(MESSAGE_MANIFEST)
        if len(item.sha256) != 64 or item.bytes <= 0:
            raise ModelsError(MESSAGE_MANIFEST)
        for name, prefixes in GROUPS.items():
            if item.path.startswith(prefixes):
                groups[name].append(item)
    if not all(groups.values()):
        raise ModelsError(MESSAGE_MANIFEST)
    return groups


def _fingerprint(files: Iterable[ModelFile]) -> str:
    text = "\n".join(f"{f.path} {f.sha256} {f.bytes}" for f in sorted(files, key=lambda f: f.path))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_link(path: Path) -> bool:
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _sha256(path: Path, cancelled: Callable[[], bool] = lambda: False) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(CHUNK * 4):
            if cancelled():
                raise ModelsError("cancelled")
            digest.update(block)
    return digest.hexdigest()


def _host_allowed(url: str, allow_hosts: frozenset[str], require_https: bool) -> bool:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if require_https and parts.scheme != "https":
        return False
    return host in allow_hosts or any(host == suffix or host.endswith("." + suffix) for suffix in MODEL_HOST_SUFFIXES)


class VoiceModels:
    """Status, download, cancel and repair of the two model groups. Thread-safe."""

    def __init__(self, root: Path, manifest_path: Path, *, emit: Callable[[dict[str, Any]], None] | None = None,
                 legacy_dirs: Callable[[], list[Path]] | None = None,
                 allow_hosts: Iterable[str] = (), require_https: bool = True) -> None:
        self.root = Path(root)
        self.manifest_path = Path(manifest_path)
        self._emit = emit or (lambda _event: None)
        self._legacy_dirs = legacy_dirs or (lambda: [])
        self._allow_hosts = frozenset(host.lower() for host in allow_hosts)
        self._require_https = require_https
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._last_emit = 0.0

    # ------------------------------------------------------------------ status --
    def _groups(self) -> dict[str, list[ModelFile]]:
        return load_manifest(self.manifest_path)

    def installed_dir(self, group: str) -> Path | None:
        """The models folder to load ``group`` from, or None when it is not installed and verified."""
        try:
            files = self._groups()[group]
        except (ModelsError, KeyError):
            return None
        return self.root if self._quick_ok(files, self._marker(group)) else None

    def _marker(self, group: str) -> Path:
        return self.root / f"{MARKER}-{group}.json"

    def _quick_ok(self, files: list[ModelFile], marker: Path) -> bool:
        """Installed means: marker for this exact manifest, and every file present at its size."""
        try:
            saved = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(saved, dict) or saved.get("fingerprint") != _fingerprint(files):
            return False
        return all((self.root / f.path).is_file() and (self.root / f.path).stat().st_size == f.bytes for f in files)

    def snapshot(self) -> dict[str, Any]:
        try:
            groups = self._groups()
        except ModelsError as exc:
            return {"groups": {}, "message": str(exc)}
        out: dict[str, Any] = {}
        with self._lock:
            for name, files in groups.items():
                total = sum(f.bytes for f in files)
                job = self._jobs.get(name)
                if job is not None and job["state"] in {"downloading", "verifying"}:
                    out[name] = {"state": job["state"], "bytes_total": total, "bytes_done": job["done"], "message": ""}
                elif self._quick_ok(files, self._marker(name)):
                    out[name] = {"state": "installed", "bytes_total": total, "bytes_done": total, "message": ""}
                else:
                    partial = sum(self._staged_size(f) for f in files)
                    state, message = ("error", job["message"]) if job and job["state"] == "error" else (
                        ("paused" if partial else "missing"), "")
                    out[name] = {"state": state, "bytes_total": total, "bytes_done": partial, "message": message}
        return {"groups": out, "message": ""}

    def _staged_size(self, item: ModelFile) -> int:
        done = self.root / STAGING / item.path
        part = done.with_name(done.name + ".part")
        for candidate in (done, part):
            try:
                return min(candidate.stat().st_size, item.bytes)
            except OSError:
                continue
        return 0

    def _publish(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_emit < 0.5:
            return
        self._last_emit = now
        try:
            self._emit(self.snapshot())
        except Exception:  # noqa: BLE001 - a UI listener never breaks a download
            pass

    # ----------------------------------------------------------------- actions --
    def download(self, group: str, *, repair: bool = False, network: bool = True) -> dict[str, Any]:
        """Starts (or resumes) one group in the background; idempotent while it runs.

        `network=False` only adopts an older install's verified models: used at start-up, so an
        update keeps the voice without downloading anything or asking.
        """
        if group not in GROUPS:
            raise ModelsError("Grupo de modelos desconhecido.")
        with self._lock:
            thread = self._threads.get(group)
            if thread is not None and thread.is_alive():
                return self.snapshot()
            self._cancel[group] = threading.Event()
            self._jobs[group] = {"state": "downloading", "done": 0, "message": ""}
            thread = threading.Thread(target=self._run, args=(group, repair, network), name=f"voice-models-{group}",
                                      daemon=True)
            self._threads[group] = thread
            thread.start()
        self._publish(force=True)
        return self.snapshot()

    def cancel(self, group: str) -> dict[str, Any]:
        with self._lock:
            event = self._cancel.get(group)
            if event is not None:
                event.set()
        return self.snapshot()

    def wait(self, group: str, timeout: float | None = None) -> None:
        thread = self._threads.get(group)
        if thread is not None:
            thread.join(timeout)

    def _run(self, group: str, repair: bool, network: bool = True) -> None:
        cancelled = self._cancel[group].is_set
        try:
            files = self._groups()[group]
            self._prepare_root()
            if repair:
                self._marker(group).unlink(missing_ok=True)
            if not self._quick_ok(files, self._marker(group)):
                if not self._adopt_legacy(group, files, cancelled):
                    if not network:
                        raise ModelsError("cancelled")  # nothing to adopt; downloading needs consent
                    self._fetch_group(group, files, cancelled, verify_existing=repair)
            state, message = "installed", ""
        except ModelsError as exc:
            state, message = ("missing", "") if str(exc) == "cancelled" else ("error", str(exc))
        except Exception:  # noqa: BLE001 - never a raw exception text in the UI
            state, message = "error", MESSAGE_NO_NETWORK
        with self._lock:
            self._jobs[group] = {"state": state, "done": 0, "message": message}
        self._publish(force=True)

    def _prepare_root(self) -> None:
        for folder in (self.root, self.root / STAGING):
            if _is_link(folder):
                raise ModelsError(MESSAGE_LINKED)
            folder.mkdir(parents=True, exist_ok=True)
            if _is_link(folder):
                raise ModelsError(MESSAGE_LINKED)

    def _progress(self, group: str, done: int, state: str = "downloading") -> None:
        with self._lock:
            self._jobs[group] = {"state": state, "done": done, "message": ""}
        self._publish()

    # ---------------------------------------------------------------- download --
    def _fetch_group(self, group: str, files: list[ModelFile], cancelled: Callable[[], bool], *,
                     verify_existing: bool) -> None:
        pending = [f for f in files if not self._final_ok(f, full=verify_existing, cancelled=cancelled)]
        need = sum(f.bytes - self._staged_size(f) for f in pending)
        if need and shutil.disk_usage(self.root).free < need + SPACE_MARGIN:
            raise ModelsError(MESSAGE_NO_SPACE)
        done = sum(f.bytes for f in files if f not in pending)
        for item in pending:
            self._fetch_file(group, item, cancelled, done)
            done += item.bytes
        self._progress(group, done, "verifying")
        # Activate: verified staged files move into place, then the marker is written last.
        for item in pending:
            staged = self.root / STAGING / item.path
            final = self.root / item.path
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, final)
        for item in files:
            if not self._final_ok(item, full=False):
                raise ModelsError(MESSAGE_CORRUPT)
        self._write_marker(group, files)

    def _final_ok(self, item: ModelFile, *, full: bool, cancelled: Callable[[], bool] = lambda: False) -> bool:
        final = self.root / item.path
        try:
            if final.stat().st_size != item.bytes:
                return False
        except OSError:
            return False
        return not full or _sha256(final, cancelled) == item.sha256

    def _write_marker(self, group: str, files: list[ModelFile]) -> None:
        marker = self._marker(group)
        temp = marker.with_name(marker.name + ".tmp")
        temp.write_text(json.dumps({"group": group, "fingerprint": _fingerprint(files)}), encoding="utf-8")
        os.replace(temp, marker)

    def _fetch_file(self, group: str, item: ModelFile, cancelled: Callable[[], bool], before: int) -> None:
        staged = self.root / STAGING / item.path
        if staged.is_file() and staged.stat().st_size == item.bytes and _sha256(staged, cancelled) == item.sha256:
            return  # verified in an earlier run, not yet activated
        staged.parent.mkdir(parents=True, exist_ok=True)
        part = staged.with_name(staged.name + ".part")
        meta = part.with_name(part.name + ".json")
        for attempt in range(RETRIES + 1):
            self._download_part(group, item, part, meta, cancelled, before)
            if _sha256(part, cancelled) == item.sha256:
                os.replace(part, staged)
                meta.unlink(missing_ok=True)
                return
            # Wrong content: never resumed from, never used.
            part.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
        raise ModelsError(MESSAGE_CORRUPT)

    def _download_part(self, group: str, item: ModelFile, part: Path, meta: Path,
                       cancelled: Callable[[], bool], before: int) -> None:
        saved: dict[str, Any] = {}
        try:
            saved = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        offset = part.stat().st_size if part.is_file() else 0
        if (saved.get("url") != item.url or saved.get("sha256") != item.sha256 or offset > item.bytes
                or (offset and not saved.get("etag"))):
            # A different pin, an unknown origin of the bytes or an oversized file: start over.
            part.unlink(missing_ok=True)
            offset, saved = 0, {}
        if offset == item.bytes:
            return
        if not _host_allowed(item.url, self._allow_hosts, self._require_https):
            raise ModelsError(MESSAGE_SOURCE)
        headers = {"User-Agent": "Lunmi-voice-models"}
        if offset:
            headers.update({"Range": f"bytes={offset}-", "If-Range": str(saved["etag"])})
        opener = urllib.request.build_opener(_PinnedRedirects(self._allow_hosts, self._require_https))
        try:
            response = opener.open(urllib.request.Request(item.url, headers=headers), timeout=TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and offset == item.bytes:
                return
            raise ModelsError(MESSAGE_NO_NETWORK) from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, ModelsError):
                raise reason from exc  # a redirect to an unpinned host
            raise ModelsError(MESSAGE_NO_NETWORK) from exc
        with response:
            if offset and response.status != 206:
                # The server sent the whole file (content changed or no range support): restart.
                offset = 0
            etag = response.headers.get("ETag") or saved.get("etag") or ""
            meta.write_text(json.dumps({"url": item.url, "sha256": item.sha256, "etag": etag}), encoding="utf-8")
            written = offset
            with open(part, "ab" if offset else "wb") as handle:
                while True:
                    if cancelled():
                        raise ModelsError("cancelled")
                    block = response.read(CHUNK)
                    if not block:
                        break
                    written += len(block)
                    if written > item.bytes:
                        raise ModelsError(MESSAGE_CORRUPT)
                    handle.write(block)
                    self._progress(group, before + written)
        if written != item.bytes:
            raise ModelsError(MESSAGE_NO_NETWORK)  # cut short: the .part resumes next time

    # ------------------------------------------------------------------ legacy --
    def _adopt_legacy(self, group: str, files: list[ModelFile], cancelled: Callable[[], bool]) -> bool:
        """Models inside an older install's voice component, verified by hash, become ours."""
        for folder in self._legacy_dirs():
            base = Path(folder)
            if _is_link(base) or not all((base / f.path).is_file() and (base / f.path).stat().st_size == f.bytes
                                         for f in files):
                continue
            self._progress(group, 0, "verifying")
            if not all(_sha256(base / f.path, cancelled) == f.sha256 for f in files):
                continue
            for item in files:
                staged = self.root / STAGING / item.path
                staged.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(base / item.path, staged)
                except OSError:
                    shutil.copyfile(base / item.path, staged)  # another drive: copy, then re-check below
            for item in files:
                staged = self.root / STAGING / item.path
                if staged.stat().st_size != item.bytes:
                    raise ModelsError(MESSAGE_CORRUPT)
                final = self.root / item.path
                final.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, final)
            self._write_marker(group, files)
            return True
        return False


class _PinnedRedirects(urllib.request.HTTPRedirectHandler):
    """Follows the model hosts' CDN redirects, and nothing else."""

    def __init__(self, allow_hosts: frozenset[str], require_https: bool) -> None:
        self.allow_hosts = allow_hosts
        self.require_https = require_https

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        if not _host_allowed(newurl, self.allow_hosts, self.require_https):
            raise urllib.error.URLError(ModelsError(MESSAGE_SOURCE))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


REPO_MANIFEST = Path(__file__).resolve().parents[2] / "desktop" / "resources" / "VOICE-SOURCE.json"


def models_root(state_root: str | Path) -> Path:
    """Where downloaded models live: the user's data, so updates and reinstalls keep them."""
    return Path(state_root) / ".local" / MODELS_DIRNAME


def manifest_for(voice_home: Path) -> Path:
    """The pins shipped with the voice runtime, or the source tree's copy when run from source."""
    shipped = Path(voice_home) / "VOICE-SOURCE.json"
    return shipped if shipped.is_file() else REPO_MANIFEST


def verified_models(state_root: str | Path, voice_home: Path, group: str) -> Path | None:
    """The downloaded models folder for `group` when installed and verified, else None."""
    return VoiceModels(models_root(state_root), manifest_for(voice_home)).installed_dir(group)
