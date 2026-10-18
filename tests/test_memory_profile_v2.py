"""Regressões da Memória Simples v2: documento livre, proveniência, CAS, undo e migração."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

import pytest

from app.brain.contracts import BrainError
from app.core.controller import _learning_origin_ok
from app.memory import MemoryItem, MemoryManager, MemoryPolicyError
from app.memory.conversation import parse_response
from app.memory.profile import ProfileTooLarge, StaleRevision
from app.memory.store import _SCHEMA_V1
from app.memory.turns import TurnWindow


def _v1_database(path: Path, rows: list[tuple], options: tuple[tuple[str, int], ...] = (
        ("enabled", 1), ("episodes", 1), ("proposals", 0), ("history", 0))) -> None:
    db = sqlite3.connect(path)
    try:
        for statement in _SCHEMA_V1:
            db.execute(statement)
        db.executemany("INSERT INTO options VALUES(?,?)", options)
        for row in rows:
            db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?)", row)
        db.execute("PRAGMA user_version=1")
        db.commit()
    finally:
        db.close()


def _legacy_row(item_id: str, text: str, kind: str = "preference",
                expires_at: str | None = None) -> tuple:
    return (item_id, kind, text, "active", "user_explicit", 1.0,
            "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", None, "{}", expires_at)


def test_migration_v1_folds_profile_keeps_backup_and_temporal_semantics(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    _v1_database(path, [
        _legacy_row("old:1", "Prefiro respostas curtas."),
        _legacy_row("old:2", "Hoje estou no projeto temporário.", kind="fact",
                    expires_at="2000-01-01T00:00:00+00:00"),
    ])
    memory = MemoryManager(path)
    try:
        backup = path.with_name(path.name + ".pre-v2.bak")
        assert backup.exists() and backup.stat().st_size > 0
        snapshot = memory.profile_snapshot()
        assert snapshot.text == "Prefiro respostas curtas.\n\nHoje estou no projeto temporário."
        assert snapshot.revision == 1
        assert {chunk.origin for chunk in snapshot.chunks} == {"manual"}
        assert memory.options()["auto_learn"] is False  # enabled AND proposals(0)
        assert memory._store.db.execute("PRAGMA user_version").fetchone()[0] == 2
        # Registo antigo vira histórico de migração (sem recall duplicado).
        assert memory._store.db.execute(
            "SELECT status FROM memories WHERE id='old:1'").fetchone()[0] == "superseded"
        assert [item.text for item in memory.recall("respostas")] == ["Prefiro respostas curtas."]
        # Expiração atravessa a migração: o facto temporário não vira permanente.
        assert memory.recall("temporário") == []
        assert "temporário" not in memory.context("projeto", "s")[0]
    finally:
        memory.close()


def test_save_is_cas_and_undo_restores_old_provenance(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / "db")
    try:
        first = memory.save_profile("Prefiro respostas curtas.", 0)
        assert first.revision == 1 and first.can_undo is True
        learned_expected = memory.apply_learning(
            [{"kind": "fact", "text": "Uso Python no trabalho."}], expected_revision=first.revision)
        assert learned_expected == 1
        snapshot = memory.profile_snapshot()
        learned = next(chunk for chunk in snapshot.chunks if chunk.text == "Uso Python no trabalho.")
        assert learned.origin == "learned" and learned.learned_key
        with pytest.raises(StaleRevision):
            memory.save_profile("Texto que não deve entrar.", 1)
        assert memory.profile_snapshot().text == snapshot.text
        edited = memory.save_profile(snapshot.text + "\n\nGosto de listas.", snapshot.revision)
        undone = memory.undo_profile(edited.revision)
        # O snapshot anterior guarda a proveniência ANTIGA: o facto continua aprendido.
        restored = next(chunk for chunk in undone.chunks if chunk.text == "Uso Python no trabalho.")
        assert restored.origin == "learned" and restored.learned_key
        assert "Gosto de listas." not in undone.text
    finally:
        memory.close()


def test_undo_is_single_level_and_forget_clears_it(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / "db")
    try:
        from app.memory.profile import NothingToUndo
        first = memory.save_profile("Uso Python.", 0)
        second = memory.save_profile("Uso Rust.", first.revision)
        third = memory.undo_profile(second.revision)
        with pytest.raises(NothingToUndo):
            memory.undo_profile(third.revision)
        item = memory.list()[0]
        memory.delete(item.id)
        with pytest.raises(NothingToUndo):
            memory.undo_profile(memory.profile_snapshot().revision)
        assert memory.recall("Rust") == []
    finally:
        memory.close()


def test_wipe_advances_revision_and_prevents_aba(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / "db")
    try:
        first = memory.save_profile("Rascunho antigo.", 0)
        memory.wipe()
        after = memory.profile_snapshot()
        assert after.text == "" and after.revision > first.revision
        with pytest.raises(StaleRevision):
            memory.save_profile("Rascunho antigo.", first.revision)
        memory.save_profile("novo", after.revision)
    finally:
        memory.close()


def test_free_text_preserves_whitespace_and_appended_block_is_byte_prefix(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / "db")
    try:
        original = "Linha um  \n\n- item\n- item  \n\n\nFim.  "
        memory.save_profile(original, 0)
        assert memory.profile_snapshot().text == original
        memory.apply_learning([{"kind": "fact", "text": "Uso Python."}],
                              expected_revision=memory.profile_revision())
        assert memory.profile_snapshot().text.startswith(original)
        assert memory.profile_snapshot().text == original + "\n\nUso Python."
    finally:
        memory.close()


def test_learning_never_touches_manual_and_can_reconcile_learned(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / "db")
    try:
        memory.save_profile("Gosto de chá.", 0)
        memory.apply_learning([{"kind": "preference", "text": "Gosto de café."}],
                              expected_revision=memory.profile_revision())
        memory.apply_learning([{"kind": "preference", "text": "Prefiro café sem açúcar.",
                                "replaces": "Gosto de café."}],
                              expected_revision=memory.profile_revision())
        text = memory.profile_snapshot().text
        assert "Prefiro café sem açúcar." in text and "Gosto de café." not in text
        assert "Gosto de chá." in text
        # Alvo manual nunca é substituído pela aprendizagem.
        memory.apply_learning([{"kind": "preference", "text": "Prefiro chá.",
                                "replaces": "Gosto de chá."}],
                              expected_revision=memory.profile_revision())
        assert "Gosto de chá." in memory.profile_snapshot().text
    finally:
        memory.close()


def test_auto_learn_off_creates_no_change_and_no_proposal(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / "db")
    try:
        memory.set_options(auto_learn=False)
        before = memory.profile_snapshot()
        assert memory.apply_learning([{"kind": "fact", "text": "Uso Python."}],
                                     expected_revision=before.revision) == 0
        assert memory.profile_snapshot().text == before.text
        assert memory.proposals() == []
    finally:
        memory.close()


def test_forget_by_chat_requires_unique_exact_target(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / "db")
    try:
        memory.save_profile("Uso Python e Rust.\n\nUso Python.\n\nTrabalho aos sábados.", 0)
        assert memory.forget_by_text("Python") == 0          # palavra solta não apaga bloco
        assert memory.forget_by_text("Uso Python.") == 1     # trecho exato e único
        text = memory.profile_snapshot().text
        assert "Uso Python e Rust." in text and "\n\nUso Python.\n\n" not in text
        # Não reaprende o parágrafo esquecido.
        memory.apply_learning([{"kind": "fact", "text": "Uso Python."}],
                              expected_revision=memory.profile_revision())
        assert all(item.text != "Uso Python." for item in memory.list())
        # delete(id) continua exclusão explícita do bloco selecionado.
        item = next(item for item in memory.list() if "Rust" in item.text)
        memory.delete(item.id)
        assert memory.recall("Rust") == []
    finally:
        memory.close()


def test_long_paragraph_is_segmented_and_remains_retrievable(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / "db")
    try:
        filler = "texto corrido " * 220  # >2400 caracteres num único parágrafo
        token = "agulha-única-xyz"
        long_paragraph = filler + token + " fim."
        memory.save_profile(long_paragraph, 0)
        chunks = memory.profile_snapshot().chunks
        assert len(chunks) > 1 and all(len(chunk.text) <= 800 for chunk in chunks)
        hits = memory.recall(token)
        assert hits and token in hits[0].text
        context, _ = memory.context(token, "s")
        assert token in context
        assert memory.profile_snapshot().text == long_paragraph  # caixa nunca truncada
    finally:
        memory.close()


def test_import_v1_merges_profile_and_export_omits_undo_slot(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / "db")
    try:
        first = memory.save_profile("Uso Python.", 0)
        exported = memory.export()
        assert exported["version"] == 2 and exported["profile"]["text"] == "Uso Python."
        assert "previous_text" not in json.dumps(exported)
        payload = {
            "version": 1,
            "memories": [asdict(MemoryItem(id="v1:1", kind="fact", text="Uso Go."))],
            "proposals": [],
            "options": {"enabled": True, "episodes": True, "proposals": True, "history": False},
        }
        assert memory.import_data(payload) == 1
        text = memory.profile_snapshot().text
        assert "Uso Python." in text and "Uso Go." in text
        assert memory.profile_snapshot().can_undo is False
        assert memory.profile_snapshot().revision > first.revision
    finally:
        memory.close()


def test_import_v1_cannot_resurrect_forgotten_profile(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / "db")
    try:
        item = MemoryItem(id="legacy:forgotten", text="Uso Blender no ensaio.")
        stored = memory.remember(item)
        memory.delete(stored)
        memory.import_data({"version": 1, "memories": [asdict(item)],
                            "proposals": [], "options": {}})
        assert memory.profile_snapshot().text.strip() == ""
    finally:
        memory.close()


def test_import_v1_over_limit_rolls_back_instead_of_skipping_tail(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / "db")
    try:
        memory.save_profile("Documento anterior.", 0)
        before = memory.export()
        records = [asdict(MemoryItem(id=f"legacy:{i}", text=f"Nota {i}: " + "astronomia " * 65))
                   for i in range(13)]
        with pytest.raises(ProfileTooLarge):
            memory.import_data({"version": 1, "memories": records, "proposals": [],
                                "options": {"auto_learn": False}})
        assert memory.export() == before
    finally:
        memory.close()


def test_learning_origin_rejects_negation_temporal_fragment_and_question() -> None:
    assert not _learning_origin_ok("Eu não gosto de música alta.", "gosto de música alta.")
    assert not _learning_origin_ok("Hoje estou no projeto Alpha.", "estou no projeto Alpha.")
    assert not _learning_origin_ok("Prefiro respostas curtas?", "Prefiro respostas curtas.")
    assert _learning_origin_ok("No meu trabalho uso editor Alpha.", "uso editor Alpha.")
    assert _learning_origin_ok("Prefiro respostas curtas.", "Prefiro respostas curtas.")


def test_turn_window_is_bounded_and_clears_per_session() -> None:
    window = TurnWindow(max_pairs=3, max_chars=1000)
    for n in range(5):
        window.add("s1", f"pergunta {n}", f"resposta {n}")
    pairs = window.pairs("s1")
    assert len(pairs) == 3 and pairs[-1] == ("pergunta 4", "resposta 4")
    window.add("s2", "outra", "sessão")
    window.clear("s1")
    assert window.pairs("s1") == [] and window.pairs("s2")


def test_metadata_invalid_does_not_destroy_valid_message() -> None:
    text, parsed = parse_response(json.dumps({"message": "resposta", "memory_changes": "não-lista"}))
    assert text == "resposta" and parsed == []
    with pytest.raises(BrainError):
        parse_response(json.dumps({"message": "resposta", "status": "CONTINUE"}))


def test_import_is_atomic_when_profile_invalid(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / "db")
    try:
        memory.remember(MemoryItem(text="Base segura."))
        options_before = memory.options()
        export_before = memory.export()
        payload = {
            "version": 2,
            "memories": [asdict(MemoryItem(id="new:1", text="Novo seguro."))],
            "proposals": [],
            "options": {**options_before, "auto_learn": False},
            "profile": {"text": "senha: fictícia", "revision": 9, "chunks": []},
        }
        with pytest.raises(MemoryPolicyError):
            memory.import_data(payload)
        # Nada mudou: nem opções, nem memórias, nem perfil, nem undo.
        assert memory.options() == options_before
        assert memory.export() == export_before
        assert memory.profile_snapshot().text == export_before["profile"]["text"]
    finally:
        memory.close()


def test_import_derives_index_from_text_and_blocks_forgotten(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / "db")
    try:
        memory.save_profile("Uso Python.", 0)
        memory.delete(memory.list()[0].id)
        forgotten_payload = {
            "version": 2, "memories": [], "proposals": [], "options": memory.options(),
            "profile": {"text": "Uso Python.", "revision": 1, "chunks": []},
        }
        assert memory.import_data(forgotten_payload) == 0
        assert memory.profile_snapshot().text == ""

        inconsistent = {
            "version": 2, "memories": [], "proposals": [], "options": memory.options(),
            "profile": {"text": "Uso Go.", "revision": 1,
                        "chunks": [{"text": "Uso Rust.", "kind": "fact", "origin": "learned"}]},
        }
        assert memory.import_data(inconsistent) == 1
        text = memory.profile_snapshot().text
        assert "Uso Go." in text and "Uso Rust." not in text
    finally:
        memory.close()


def test_incomplete_existing_backup_is_not_trusted(tmp_path: Path) -> None:
    from app.memory.store import Store
    path = tmp_path / "memory.sqlite3"
    _v1_database(path, [_legacy_row("old:1", "Prefiro respostas curtas.")])
    marker = path.with_name(path.name + ".pre-v2.bak")
    marker.write_bytes(b"")  # tentativa interrompida deixou um marcador vazio
    memory = MemoryManager(path)
    try:
        assert marker.stat().st_size > 0
        assert Store._valid_v1_copy(marker) is True
        assert memory.profile_snapshot().text == "Prefiro respostas curtas."
    finally:
        memory.close()


def test_valid_previous_backup_is_preserved(tmp_path: Path) -> None:
    import hashlib
    path = tmp_path / "memory.sqlite3"
    _v1_database(path, [_legacy_row("old:1", "Prefiro respostas curtas.")])
    marker = path.with_name(path.name + ".pre-v2.bak")
    source = sqlite3.connect(path)
    try:
        dest = sqlite3.connect(marker)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    digest_before = hashlib.sha256(marker.read_bytes()).hexdigest()
    memory = MemoryManager(path)
    try:
        assert hashlib.sha256(marker.read_bytes()).hexdigest() == digest_before
        assert memory.profile_snapshot().text == "Prefiro respostas curtas."
    finally:
        memory.close()
