"""Voice models downloaded after installation: pinned sources, resumable, verified before use.

The setup ships the voice runtime but not its ~2 GB of models (user request, 01/11: the setup
was 18 MB under the 2 GiB limit). They are fetched on request into
``<state_root>/.local/voice-models`` from the URLs, sizes and SHA-256 pinned in
``VOICE-SOURCE.json``, in two groups the user picks separately: the Lunmi voice (speech) and
dictation.

Rules, each guarding a failure seen or expected on users' machines:
- only pinned URLs, over HTTPS, to the model hosts (redirects included);
- a file is written to an isolated ``.part``, resumed with ``Range`` + ``If-Range`` so changed
  content restarts instead of being stitched, and its SHA-256 is checked before use;
- a marker is written only after every file of the group was hash-verified in that run (a
  same-size corrupt file is never accepted), and last, so a crash never looks installed;
- every path written, moved or read from an older install is checked component by component:
  links/junctions anywhere on the way are refused (writes must stay in the user's data);
- an older install's models are adopted file by file by link/copy and hash of the copy; the
  originals are removed only after the new group is active, so a failure mid-way loses nothing;
  until the new group is complete, that older copy (verified against its own pins) stays in use;
- free space is checked first; cancel keeps the ``.part`` for resume; repair re-hashes all.
Nothing here opens the microphone or plays audio.
Review 21a75b0 (orchestrator, 01/11) found P1-1..P1-5 in the first version; each has a test.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import ssl
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import truststore

GROUPS: dict[str, tuple[str, ...]] = {
    "speech": ("models/supertonic-3/",),
    "dictation": ("models/whisper-large-v3-turbo/", "models/silero/"),
}
MODELS_DIRNAME = "voice-models"
MARKER = ".lunmi-verified"
STAGING = ".staging"
HASH_CACHE = ".hash-cache.json"
CHUNK = 1 << 20
# Room for the staging-to-final moves and the filesystem itself, beyond the bytes to fetch.
SPACE_MARGIN = 256 << 20
TIMEOUT_S = 30.0
RETRIES = 2
# A dropped connection resumes the .part by itself after these pauses before the user sees it.
NETWORK_RETRY_WAITS_S = (2.0, 5.0, 15.0, 30.0)
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


def _guard(base: Path, path: Path) -> Path:
    """`path` inside `base` with no link/junction on any component from base down (P1-2)."""
    relative = path.relative_to(base)  # lexical: raises for anything outside
    current = base
    if _is_link(current):
        raise ModelsError(MESSAGE_LINKED)
    for part in relative.parts:
        current = current / part
        if _is_link(current):
            raise ModelsError(MESSAGE_LINKED)
    try:
        path.resolve(strict=False).relative_to(base.resolve(strict=False))
    except ValueError as exc:
        raise ModelsError(MESSAGE_LINKED) from exc
    return path


def _write_json(base: Path, target: Path, data: Any) -> None:
    """Atomic JSON write through a new, exclusive temp file: an existing name (even a hard link
    planted to a file elsewhere) is never opened or truncated, only replaced as a directory entry."""
    folder = _guard(base, target.parent)
    handle, temp = tempfile.mkstemp(dir=folder, prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
        os.replace(temp, _guard(base, target))
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _sha256(path: Path, cancelled: Callable[[], bool] = lambda: False) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(CHUNK * 4):
            if cancelled():
                raise ModelsError("cancelled")
            digest.update(block)
    return digest.hexdigest()


def _stamp(path: Path) -> list[int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return [info.st_size, info.st_mtime_ns]


def _host_allowed(url: str, allow_hosts: frozenset[str], require_https: bool) -> bool:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if require_https and parts.scheme != "https":
        return False
    return host in allow_hosts or any(host == suffix or host.endswith("." + suffix) for suffix in MODEL_HOST_SUFFIXES)


def older_voice_components() -> list[Path]:
    """Voice components of earlier versions next to the current one that still hold models."""
    bundled = os.environ.get("LUNMI_BUNDLED_VOICE_HOME", "").strip()
    if not bundled or not Path(bundled).is_absolute():
        return []
    current = Path(bundled)
    try:
        return sorted(folder for folder in current.parent.glob("voice-*")
                      if folder != current and (folder / "models").is_dir() and not _is_link(folder))
    except OSError:
        return []


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
        self._cache_lock = threading.Lock()  # speech and dictation threads share the hash cache
        self._jobs: dict[str, dict[str, Any]] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._last_emit = 0.0

    # ------------------------------------------------------------------ status --
    def _groups(self) -> dict[str, list[ModelFile]]:
        return load_manifest(self.manifest_path)

    def installed_dir(self, group: str) -> Path | None:
        """The folder holding this version's verified models for `group`, or None."""
        try:
            files = self._groups()[group]
        except (ModelsError, KeyError):
            return None
        return self.root if self._quick_ok(files, self._marker(group)) else None

    def previous_dir(self, group: str) -> Path | None:
        """An older install's copy of `group`, verified against its own pins, still unchanged.

        Used only while this version's group is incomplete (P1-5: Alpha 9 lacked F5.json), so an
        update keeps a working voice without downloading anything until the user agrees to.
        """
        try:
            record = json.loads(self._previous_record(group).read_text(encoding="utf-8"))
            folder = Path(record["folder"])
            if _is_link(folder) or not folder.is_dir():
                return None
            for path, stamp in record["stamps"].items():
                if _stamp(_guard(folder, folder / path)) != stamp:
                    return None
            return folder
        except (OSError, ValueError, KeyError, TypeError, ModelsError):
            return None

    def _marker(self, group: str) -> Path:
        return self.root / f"{MARKER}-{group}.json"

    def _previous_record(self, group: str) -> Path:
        return self.root / f".previous-{group}.json"

    def _quick_ok(self, files: list[ModelFile], marker: Path) -> bool:
        """Installed means: a marker for this exact manifest (written only after every file was
        hash-verified) and every file still present at its size."""
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
                previous = self.previous_dir(name) is not None
                if job is not None and job["state"] in {"downloading", "verifying"}:
                    out[name] = {"state": job["state"], "bytes_total": total, "bytes_done": job["done"],
                                 "message": "", "previous": previous}
                elif self._quick_ok(files, self._marker(name)):
                    out[name] = {"state": "installed", "bytes_total": total, "bytes_done": total,
                                 "message": "", "previous": False}
                else:
                    partial = sum(self._present_size(f) for f in files)
                    state, message = ("error", job["message"]) if job and job["state"] == "error" else (
                        ("paused" if partial else "missing"), "")
                    out[name] = {"state": state, "bytes_total": total, "bytes_done": partial, "message": message,
                                 "previous": previous}
        return {"groups": out, "message": ""}

    def _present_size(self, item: ModelFile) -> int:
        """Bytes already here for a file: final, staged, or partly downloaded."""
        final = self.root / item.path
        staged = self.root / STAGING / item.path
        for candidate in (final, staged, staged.with_name(staged.name + ".part")):
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
            self._jobs[group] = {"state": "verifying" if not network else "downloading", "done": 0, "message": ""}
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
                if not self._complete(group, files, cancelled, network=network, trust_cache=not repair):
                    self._remember_previous(group, cancelled)
                    raise ModelsError("cancelled")  # incomplete without consent to download
            self._forget_previous(group)
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

    # ------------------------------------------------------------ verification --
    def _hash_cache(self) -> dict[str, Any]:
        try:
            data = json.loads((self.root / HASH_CACHE).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _proven(self, path: Path, item: ModelFile, cancelled: Callable[[], bool], *, trust_cache: bool) -> bool:
        """`path` holds exactly `item`, proven by its SHA-256 (cached by size+mtime of our files)."""
        stamp = _stamp(path)
        if stamp is None or stamp[0] != item.bytes:
            return False
        key = str(path)
        cache = self._hash_cache()
        if trust_cache and cache.get(key) == [*stamp, item.sha256]:
            return True
        if _sha256(path, cancelled) != item.sha256:
            return False
        with self._cache_lock:  # re-read inside the lock: another group's entries are kept
            cache = self._hash_cache()
            cache[key] = [*stamp, item.sha256]
            _write_json(self.root, self.root / HASH_CACHE, cache)
        return True

    # ---------------------------------------------------------------- complete --
    def _complete(self, group: str, files: list[ModelFile], cancelled: Callable[[], bool], *,
                  network: bool, trust_cache: bool) -> bool:
        """Makes every file of `group` present and hash-proven in this run, then activates it."""
        self._progress(group, 0, "verifying")
        where: dict[str, str] = {}  # path -> "final" | "staged", each proven by hash in this run
        for item in files:
            final = _guard(self.root, self.root / item.path)
            staged = _guard(self.root, self.root / STAGING / item.path)
            if self._proven(final, item, cancelled, trust_cache=trust_cache):
                where[item.path] = "final"
            elif self._proven(staged, item, cancelled, trust_cache=trust_cache):
                where[item.path] = "staged"
        for item in files:
            if item.path not in where and self._stage_from_previous(item, cancelled, trust_cache=trust_cache):
                where[item.path] = "staged"
        missing = [f for f in files if f.path not in where]
        if missing and not network:
            return False
        need = sum(f.bytes - self._present_size(f) for f in missing)
        if need and shutil.disk_usage(self.root).free < need + SPACE_MARGIN:
            raise ModelsError(MESSAGE_NO_SPACE)
        done = sum(f.bytes for f in files if f.path in where)
        for item in missing:
            self._fetch_file(group, item, cancelled, done)
            where[item.path] = "staged"
            done += item.bytes
        self._progress(group, done, "verifying")
        for item in files:  # activate: proven staged files move into place
            if where[item.path] == "staged":
                staged = _guard(self.root, self.root / STAGING / item.path)
                final = _guard(self.root, self.root / item.path)
                final.parent.mkdir(parents=True, exist_ok=True)
                _guard(self.root, final)
                os.replace(staged, final)
        for item in files:
            if _stamp(self.root / item.path) is None or (self.root / item.path).stat().st_size != item.bytes:
                raise ModelsError(MESSAGE_CORRUPT)
        self._write_marker(group, files)  # last: only now does the group count as installed
        self._remove_previous_copies(files)
        return True

    def _write_marker(self, group: str, files: list[ModelFile]) -> None:
        _write_json(self.root, self._marker(group), {"group": group, "fingerprint": _fingerprint(files)})

    # ---------------------------------------------------------------- download --
    def _fetch_file(self, group: str, item: ModelFile, cancelled: Callable[[], bool], before: int) -> None:
        staged = _guard(self.root, self.root / STAGING / item.path)
        staged.parent.mkdir(parents=True, exist_ok=True)
        _guard(self.root, staged)
        part = _guard(self.root, staged.with_name(staged.name + ".part"))
        meta = _guard(self.root, part.with_name(part.name + ".json"))
        for _attempt in range(RETRIES + 1):
            for wait in (*NETWORK_RETRY_WAITS_S, None):
                try:
                    self._download_part(group, item, part, meta, cancelled, before)
                    break
                except ModelsError as exc:
                    if str(exc) != MESSAGE_NO_NETWORK or wait is None:
                        raise
                    if self._cancel.get(group, threading.Event()).wait(wait):  # True: cancelled
                        raise ModelsError("cancelled") from exc
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
        # Windows' own chain check: a fresh install has no cached roots, and only it fetches them.
        tls = urllib.request.HTTPSHandler(context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
        opener = urllib.request.build_opener(tls, _PinnedRedirects(self._allow_hosts, self._require_https))
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
            _write_json(self.root, meta, {"url": item.url, "sha256": item.sha256, "etag": etag})
            written = offset
            with open(part, "ab" if offset else "wb") as handle:
                while True:
                    if cancelled():
                        raise ModelsError("cancelled")
                    try:
                        block = response.read(CHUNK)
                    except (OSError, http.client.HTTPException) as exc:  # timeout or cut mid-file
                        raise ModelsError(MESSAGE_NO_NETWORK) from exc
                    if not block:
                        break
                    written += len(block)
                    if written > item.bytes:
                        raise ModelsError(MESSAGE_CORRUPT)
                    handle.write(block)
                    self._progress(group, before + written)
        if written != item.bytes:
            raise ModelsError(MESSAGE_NO_NETWORK)  # cut short: the .part resumes next time

    # ------------------------------------------------------------- older install --
    def _older(self) -> list[Path]:
        return [Path(folder) for folder in self._legacy_dirs() if not _is_link(Path(folder))]

    def _stage_from_previous(self, item: ModelFile, cancelled: Callable[[], bool], *, trust_cache: bool) -> bool:
        """An identical file of an older install, linked or copied into staging and proven by
        the hash of the copy (P1-3). The original is never moved or deleted here."""
        for base in self._older():
            try:
                source = _guard(base, base / item.path)
                if _stamp(source) is None or source.stat().st_size != item.bytes:
                    continue
                staged = _guard(self.root, self.root / STAGING / item.path)
                staged.parent.mkdir(parents=True, exist_ok=True)
                _guard(self.root, staged)
                staged.unlink(missing_ok=True)
                try:
                    os.link(source, staged)  # same volume: no extra space, no copy
                except OSError:
                    if shutil.disk_usage(self.root).free < item.bytes + SPACE_MARGIN:
                        raise ModelsError(MESSAGE_NO_SPACE)
                    shutil.copyfile(source, staged)
                if self._proven(staged, item, cancelled, trust_cache=False):
                    return True
                staged.unlink(missing_ok=True)
            except ModelsError as exc:
                if str(exc) in {"cancelled", MESSAGE_NO_SPACE}:
                    raise
                continue  # a linked or odd older copy is simply not used
            except OSError:
                continue
        return False

    def _remember_previous(self, group: str, cancelled: Callable[[], bool]) -> None:
        """Keeps an older install's complete copy of `group` in use, proven against its own pins."""
        for base in self._older():
            try:
                files = load_manifest(_guard(base, base / "VOICE-SOURCE.json"))[group]
                stamps: dict[str, list[int] | None] = {}
                for item in files:
                    path = _guard(base, base / item.path)
                    if not self._proven(path, item, cancelled, trust_cache=True):
                        break
                    stamps[item.path] = _stamp(path)
                else:
                    _write_json(self.root, self._previous_record(group), {"folder": str(base), "stamps": stamps})
                    return
            except ModelsError as exc:
                if str(exc) == "cancelled":
                    raise
            except OSError:
                continue

    def _forget_previous(self, group: str) -> None:
        self._previous_record(group).unlink(missing_ok=True)

    def _remove_previous_copies(self, files: list[ModelFile]) -> None:
        """After this version's group is active, the older copies of those files can go."""
        for base in self._older():
            for item in files:
                try:
                    _guard(base, base / item.path).unlink(missing_ok=True)
                except (ModelsError, OSError):
                    continue
            for folder in sorted((p for p in (base / "models").rglob("*") if p.is_dir()), reverse=True):
                try:
                    folder.rmdir()  # only empty folders
                except OSError:
                    pass


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
    """The models folder to load `group` from: this version's verified copy, else an older
    install's copy still verified against its own pins, else None."""
    manager = VoiceModels(models_root(state_root), manifest_for(voice_home))
    return manager.installed_dir(group) or manager.previous_dir(group)
