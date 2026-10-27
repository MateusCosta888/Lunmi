"""O auditor do payload recusa artefactos privados do Spotify."""
from __future__ import annotations

import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "audit_payload_i3",
    Path(__file__).resolve().parents[1] / "desktop" / "scripts" / "audit_payload.py",
)
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)

ASSEMBLE_SPEC = importlib.util.spec_from_file_location(
    "assemble_resources_i3",
    Path(__file__).resolve().parents[1] / "desktop" / "scripts" / "assemble_resources.py",
)
assert ASSEMBLE_SPEC and ASSEMBLE_SPEC.loader
assemble = importlib.util.module_from_spec(ASSEMBLE_SPEC)
ASSEMBLE_SPEC.loader.exec_module(assemble)


def test_secret_and_install_state_are_findings(tmp_path: Path) -> None:
    root = tmp_path / "win-unpacked"
    (root / "resources" / "app").mkdir(parents=True)
    (root / "resources" / "app" / "secret.bin").write_bytes(b"32-byte-secret")
    (root / "resources" / "app" / "install.json").write_text("{}", encoding="utf-8")
    findings = audit.audit_tree(root)
    assert any("secret.bin" in item for item in findings)
    assert any("install.json" in item for item in findings)


def test_private_bundle_with_pairing_secret_is_a_finding(tmp_path: Path) -> None:
    root = tmp_path / "win-unpacked"
    target = root / "resources" / "app" / "extension"
    target.mkdir(parents=True)
    (target / "bundle.js").write_text(
        'globalThis.LUNMI_SPOTIFY_KIT_CONFIG = {"secretHex":"' + "a" * 64 + '"};',
        encoding="utf-8")
    findings = audit.audit_tree(root)
    assert any("credencial" in item for item in findings)


def test_assemble_rejects_a_private_pairing_bundle_by_content(tmp_path: Path) -> None:
    private = tmp_path / "private.js"
    private.write_text('globalThis.CONFIG = {"secretHex":"' + "b" * 64 + '"};',
                       encoding="utf-8")
    public = tmp_path / "public.js"
    public.write_text('if (typeof secretHex !== "string") { throw new Error(); }',
                      encoding="utf-8")
    assert assemble._contains_pairing_secret(private) is True
    assert assemble._contains_pairing_secret(public) is False


def test_public_extension_and_notices_are_not_findings(tmp_path: Path) -> None:
    root = tmp_path / "win-unpacked"
    target = root / "resources" / "app" / "app" / "integrations" / "spotify_assets"
    target.mkdir(parents=True)
    (target / "lunmi-spotify.js").write_text(
        "globalThis.LUNMI_SPOTIFY_KIT_CONFIG = globalThis.LUNMI_SPOTIFY_KIT_CONFIG || {};",
        encoding="utf-8")
    (root / "resources" / "THIRD_PARTY_NOTICES.md").write_text(
        "websockets BSD-3-Clause", encoding="utf-8")
    assert audit.audit_tree(root) == []
