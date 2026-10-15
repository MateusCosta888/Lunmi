from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any


class SafetyError(ValueError):
    pass


def save_target(root: Path, requested: str) -> Path:
    """Only plain-text documents inside the configured root; no ADS or devices."""
    if not requested or len(requested) > 240 or any(ord(c) < 32 for c in requested):
        raise SafetyError("Informe um nome/caminho .txt válido, com até 240 caracteres.")
    path = Path(requested)
    target = path if path.is_absolute() else root / path
    root = root.resolve()
    if any(c in part for part in target.parts[1:] for c in '<>:"|?*'):
        raise SafetyError("Caminho com caracteres especiais/ADS não permitido.")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    for part in target.parts[1:]:
        if part.endswith((".", " ")) or part.split(".")[0].upper() in reserved:
            raise SafetyError("Nome de dispositivo ou caminho ambíguo não permitido.")
    # Reject reparse points, including junctions, on every existing component.
    for component in [target, *target.parents]:
        if component.exists() and getattr(component.lstat(), "st_file_attributes", 0) & 0x400:
            raise SafetyError("Symlinks/junctions não são permitidos para salvar documentos.")
    target = target.resolve()
    if target == root or not target.is_relative_to(root) or target.suffix.lower() != ".txt":
        raise SafetyError(f"Salvamento permitido somente em arquivos .txt dentro de {root}.")
    if target.parent != root and not target.parent.is_dir():
        raise SafetyError("A subpasta de destino precisa existir.")
    return target


def file_version(path: Path) -> dict[str, Any] | None:
    """Evidence of the exact local object approved, not a model-provided claim."""
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 1_048_576:
        raise SafetyError("Não sobrescrevemos diretórios ou arquivos maiores que 1 MiB.")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_nlink > 1:
            raise SafetyError("Não sobrescrevemos arquivos com hard links compartilhados.")
        digest = hashlib.sha256(stream.read()).hexdigest()
        after = os.fstat(stream.fileno())
    if (before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_ino, after.st_mtime_ns, after.st_size):
        raise SafetyError("O arquivo mudou durante a leitura; tente novamente.")
    return {"inode": after.st_ino, "mtime_ns": after.st_mtime_ns, "size": after.st_size, "sha256": digest}


def check_approved_file(root: Path, path: str, approved: dict[str, Any] | None) -> Path:
    target = save_target(root, path)
    if file_version(target) != approved:
        raise SafetyError("Destino mudou após a confirmação. Operação bloqueada; confirme novamente.")
    return target


def normalized_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def verify_saved_file(path: Path, expected: str) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > 1_048_576:
        return {"file_verified": False}
    if getattr(path.lstat(), "st_file_attributes", 0) & 0x400 or path.stat().st_nlink > 1:
        return {"file_verified": False}
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        data = stream.read()
        after = os.fstat(stream.fileno())
    if (before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_ino, after.st_mtime_ns, after.st_size):
        return {"file_verified": False}
    try:
        actual = data.decode("utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
    except UnicodeError:
        return {"file_verified": False}
    return {"file_verified": normalized_text(actual) == normalized_text(expected),
            "file_path": str(path), "file_size": len(data), "file_sha256": hashlib.sha256(data).hexdigest()}
