from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from app.computer.fastpath import resolve
from app.computer.search import query_field, result_proof, spotify_search
from app.computer.uia import UIAError
from app.computer.verification import unresolved_evidence
from app.computer.windows_tools import PreconditionMissing


@pytest.mark.parametrize('instruction,query', [
    ('pesquisa por Billie Jean no spotify', 'Billie Jean'),
    ('Busque a música Thriller no Spotify.', 'Thriller'),
    ('Procure "Rock & Roll + {ENTER}" no Spotify.', 'Rock & Roll + {ENTER}'),
    ('Pesquisar Coldplay no Spotify', 'Coldplay'),
])
def test_literal_search_is_fully_consumed(instruction, query):
    plan = resolve(instruction)
    assert plan is not None and plan.kind == 'search'
    assert plan.to_wire()['query'] == query


@pytest.mark.parametrize('text', [
    'Busque Thriller e toque no Spotify', 'Busque Thriller no Spotify e toque a primeira',
    'Busque Thriller se estiver aberto no Spotify', 'Toque Thriller no Spotify',
    'Busque Thriller no navegador', 'Busque no Spotify', 'Busque x\ny no Spotify',
    'Busque '+ 'a' * 201 + ' no Spotify',
])
def test_ambiguous_or_extra_operations_do_not_fast_path(text):
    assert resolve(text) is None


@pytest.mark.parametrize('instruction,query', [
    # FASE 1.1: the same literal search named three ways, one of them compound.
    ('Abra o Spotify e busque Billie Jean.', 'Billie Jean'),
    ('Abra Spotify e busque Billie Jean', 'Billie Jean'),
    ('Abra o Spotify e pesquise por Thriller', 'Thriller'),
    ('No Spotify busque Coldplay.', 'Coldplay'),
    ('no spotify pesquisa por Daft Punk', 'Daft Punk'),
])
def test_composite_open_and_search_is_one_deterministic_plan(instruction, query):
    plan = resolve(instruction)
    assert plan is not None and plan.kind == 'search', instruction
    wire = plan.to_wire()
    assert wire['query'] == query
    assert wire['processes'] == ['spotify.exe']


@pytest.mark.parametrize('text', [
    # A second operation, a conditional, or a name the registry does not carry decline.
    'Abra o Spotify e toque Billie Jean', 'Abra o Spotify e busque X e feche',
    'Abra o Spotify e busque Billie Jean se estiver aberto',
    'Abra o Bloco de Notas e busque Thriller', 'No navegador busque Thriller',
    'Abra o Spotify e busque',
])
def test_composite_search_declines_extra_operations(text):
    assert resolve(text) is None, text


def test_new_searchable_app_is_a_registry_entry_not_a_parser_change(monkeypatch):
    """FASE 1.2 acceptance: naming a new app needs no parser/HostAgent/AppAgent edit."""
    from app.computer import fastpath
    from app.computer.fastpath import KnownApp

    extra = KnownApp('Player', ('player', 'meu player'), ('player.exe',), ('search',))
    monkeypatch.setattr(fastpath, 'KNOWN_APPS', (*fastpath.KNOWN_APPS, extra))
    assert fastpath.known_actions('Player') == ('search',)
    plan = resolve('Abra o Player e busque Led Zeppelin')
    assert plan is not None and plan.kind == 'search'
    assert plan.to_wire()['processes'] == ['player.exe']
    assert plan.to_wire()['query'] == 'Led Zeppelin'
    # A registry entry with no registered action is never a search target.
    assert fastpath.known_app('Player', 'open') is None


def test_maximize_uses_verified_window_path():
    plan = resolve('Maximize a Calculadora.')
    assert plan is not None and plan.operation == 'maximize'


def row(identifier, kind, name, parent=''):
    return {'id': identifier, 'type': kind, 'name': name, 'parent': parent,
            'enabled': True, 'offscreen': False, 'protected': False}


class Engine:
    def __init__(self, proof=True, siblings=1):
        self.field = row('field', 'ComboBox', 'O que você quer ouvir?', 'header')
        self.grid = row('grid', 'DataGrid', 'Search results')
        self.hit = row('hit', 'DataItem', 'Thriller', 'grid')
        self.proof = proof
        self.siblings = siblings
        self.mutations = []
        self.backend = self

    def observe(self):
        return {'elements': [self.field, self.grid, self.hit], 'complete': False}

    def _referenced(self, identifier):
        return identifier, {}, self

    def children(self, parent):
        return [self.field] * self.siblings

    def metadata(self, element):
        return element

    def reference(self, *args, **kwargs):
        return None

    def text(self, identifier, name):
        return {'readable': True, 'truncated': False,
                'text': 'Thriller' if self.mutations else 'old query'}

    def verify(self, identifier, name, **kwargs):
        # Shape copied from UIAEngine.verify: the post-condition contract reads the native
        # operation and the control's own runtime identity, not a workflow label.
        return {'operation': 'uia_verify', 'id': identifier, 'handle': 1,
                'runtime_id': [2, 7], 'property': kwargs.get('property_name', 'text'),
                'observed': {'readable': True, 'length': len(name)},
                'verified': self.proof, 'expected_text': kwargs.get('expected_text', ''),
                'expected_state': -1, 'source': 'rendered_subtree',
                'verified_by': 'independent_live_read'}

    def set_text(self, identifier, name, query, **kwargs):
        self.mutations.append(('set', query))

    def keyboard(self, *args):
        self.mutations.append(('keyboard', args[-1]))


def test_query_field_proves_header_uniqueness_without_whole_tree():
    engine = Engine()
    assert query_field(engine, engine.observe())['id'] == 'field'
    engine.siblings = 2
    with pytest.raises(PreconditionMissing):
        query_field(engine, engine.observe())
    assert not engine.mutations


def test_query_input_alone_cannot_prove_results():
    engine = Engine()
    assert result_proof(engine, {'elements': [engine.field]}, 'Thriller') is None
    assert result_proof(engine, engine.observe(), 'Billie Jean') is None
    engine.proof = False
    assert result_proof(engine, engine.observe(), 'Thriller') is None
    engine.proof = True
    assert result_proof(engine, engine.observe(), 'Thriller')['results_verified']


def test_result_must_belong_to_results_not_library():
    engine = Engine()
    engine.grid['name'] = 'Sua Biblioteca'
    assert result_proof(engine, engine.observe(), 'Thriller') is None


def test_result_proof_resolves_a_pending_enter_without_relabelling_the_operation():
    """A-01 anti-regression: the proof keeps the operation the provider ran.

    B0 (`logs/uia-v2-after-all-20261011-210950.json`, determinística 1) recorded a
    verified result proof relabelled `operation=spotify_search`, so
    `verification.covers_dispatch` refused it and the controller kept the Enter pending:
    the task was downgraded to NOT_VERIFIED while the oracle proved the results existed.
    The workflow identity now travels as its own metadata.
    """
    engine = Engine()
    proof = result_proof(engine, engine.observe(), 'Thriller')
    assert proof is not None
    assert proof['operation'] == 'uia_verify'  # the native read, not a workflow label
    assert proof['property'] == 'text' and proof['verified']
    assert proof['results_verified'] is True  # existing consumers keep working
    assert proof['workflow'] == 'spotify_search'
    assert proof['query_length'] == len('Thriller')
    typed = {'operation': 'set_edit_text', 'handle': 1, 'id': 'field', 'runtime_id': [1, 4],
             'verified': True, 'length': len('Thriller'),
             'text_sha256': hashlib.sha256(b'Thriller').hexdigest()}
    pending_enter = {'operation': 'keyboard_input', 'handle': 1, 'id': 'field',
                     'runtime_id': [1, 4], 'submitted': True, 'verified': False,
                     'needs_postcondition': True}
    assert unresolved_evidence([typed, pending_enter, proof]) == []
    # Same length is not the same submission: the proof is bound to Thriller's digest.
    other = {**typed, 'length': len('NOT-SAME'),
             'text_sha256': hashlib.sha256(b'NOT-SAME').hexdigest()}
    assert unresolved_evidence([other, pending_enter, proof]) == [pending_enter]
    # The contract itself is not loosened: a proof of another operation still cannot
    # resolve the Enter. Only the native verification can.
    assert unresolved_evidence([typed, pending_enter, {**proof, 'operation': 'spotify_search'}]) == [
        pending_enter]
    # A marker supplied by someone else, even in the same window, is not the live
    # workflow's association. The opaque token was issued for the real proof.
    foreign = {**proof, 'id': 'library-row', 'runtime_id': [9, 9],
               'association': 'forged-marker'}
    assert unresolved_evidence([typed, pending_enter, foreign]) == [pending_enter]
    no_token = {key: value for key, value in proof.items() if key != 'association'}
    assert unresolved_evidence([typed, pending_enter, no_token]) == [pending_enter]


def runtime(engine):
    tools = SimpleNamespace(uia=engine, evidence=[], pending_ui=False,
                            target_windows=lambda processes: [{'handle': 1, 'title': 'Spotify'}])
    async def bind(**kwargs):
        return None
    return SimpleNamespace(tools=tools, actions=0, bind_selected_window=bind)


def test_workflow_groups_input_and_verifies_without_llm():
    engine = Engine()
    original_observe = engine.observe
    def observe():
        data = original_observe()
        if not engine.mutations:
            data['elements'] = [engine.field]
        return data
    engine.observe = observe
    rt = runtime(engine)
    data = asyncio.run(spotify_search(rt, {'query': 'Thriller'}))
    assert data['actions'] == 2 and rt.tools.evidence[-1]['verified']
    assert engine.mutations == [('set', 'Thriller'), ('keyboard', '{ENTER}')]
    assert not rt.tools.pending_ui


def launching_runtime(engine):
    launched = {'handle': 1, 'title': 'Spotify', 'process': 'Spotify.exe'}
    tools = SimpleNamespace(uia=engine, evidence=[], pending_ui=False,
                            target_windows=lambda processes: [])
    async def launch(*args, **kwargs):
        assert kwargs['processes'] == ('spotify.exe',)
        return launched
    tools.launch = launch
    async def bind(**kwargs):
        return None
    return SimpleNamespace(tools=tools, actions=0, bind_selected_window=bind)


def test_closed_app_waits_for_the_search_field_before_typing(monkeypatch):
    """FASE 1.1: after a launch the field is still rendering; wait, never type blind."""
    from app.computer import search as search_module
    monkeypatch.setattr(search_module, 'FIELD_READY_SECONDS', 1.0)
    engine = Engine()
    real_observe = engine.observe
    calls = {'n': 0}
    def observe():
        calls['n'] += 1
        if calls['n'] < 3:
            return {'elements': [], 'complete': False}
        data = real_observe()
        if not engine.mutations:
            data['elements'] = [engine.field]
        return data
    engine.observe = observe
    rt = launching_runtime(engine)
    asyncio.run(spotify_search(rt, {'target': 'exe:C:/fake/Spotify.exe', 'query': 'Thriller'}))
    assert calls['n'] >= 3 and rt.tools.evidence[-1]['verified']
    assert engine.mutations == [('set', 'Thriller'), ('keyboard', '{ENTER}')]


def test_closed_app_without_the_field_is_unverified_never_replayed(monkeypatch):
    from app.computer import search as search_module
    monkeypatch.setattr(search_module, 'FIELD_READY_SECONDS', 0.2)
    engine = Engine()
    engine.observe = lambda: {'elements': [], 'complete': False}
    rt = launching_runtime(engine)
    with pytest.raises(UIAError):
        asyncio.run(spotify_search(rt, {'target': 'exe:C:/fake/Spotify.exe',
                                        'query': 'Thriller'}))
    assert not engine.mutations


def test_failed_write_is_not_replayed_or_declined():
    engine = Engine()
    def fail(*args, **kwargs):
        engine.mutations.append('uncertain write')
        raise UIAError('verification_failed', 'not confirmed')
    engine.set_text = fail
    rt = runtime(engine)
    with pytest.raises(UIAError):
        asyncio.run(spotify_search(rt, {'query': 'Thriller'}))
    assert engine.mutations == ['uncertain write']
    assert rt.actions == 1
