"""Verified search workflows: literal queries, existing UFO executor, no inference.

Only the registered application and its observed search region can be touched. A
missing/ambiguous/incomplete target declines BEFORE mutation; after dispatch, a
failure is unverified work, never permission to replay through the model.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import hashlib

from app.computer.controls import fold
from app.computer.uia import UIAError
from app.computer.verification import issue_association
from app.computer.windows_tools import PreconditionMissing

SEARCH_LABELS = frozenset({"o que voce quer ouvir", "what do you want to play",
                          "what do you want to listen to"})
RESULT_LABELS = frozenset({"search results", "resultados da busca", "resultados da pesquisa"})
# Measured (2026-10-10): launching a *closed* Spotify proves the window in under a second
# (readiness_ms ~914), but the search header inside it is still rendering then. Waiting for
# the field, instead of reading it once, is what turns "abra X e busque Y" into one
# deterministic task; the wait is bounded and it never types before the field is proven.
FIELD_READY_SECONDS = 8.0


def query_field(engine: Any, observation: dict[str, Any]) -> dict[str, Any]:
    """Prove uniqueness in the search header, not in a truncated whole-window tree."""
    candidates = [row for row in observation['elements']
                  if row['type'] in {'ComboBox', 'Edit'} and fold(row['name']) in SEARCH_LABELS
                  and row.get('enabled') and not row.get('offscreen')
                  and not row.get('protected')]
    if len(candidates) != 1 or not candidates[0].get('parent'):
        raise PreconditionMissing('Campo de busca do Spotify ausente ou ambíguo; nada foi digitado.')
    field = candidates[0]
    parent, _, backend = engine._referenced(field['parent'])
    # Only direct header children determine which query field this is. Autocomplete
    # descendants can number in the hundreds and do not make that header ambiguous.
    children = backend.children(parent)
    if len(children) >= 1600:
        raise PreconditionMissing('Cabeçalho de busca incompleto; nada foi digitado.')
    siblings = [backend.metadata(child) for child in children]
    siblings = [item for item in siblings if item['type'] in {'ComboBox', 'Edit'}
                and fold(item['name']) in SEARCH_LABELS]
    if len(siblings) != 1:
        raise PreconditionMissing('Mais de um campo de busca no cabeçalho; nada foi digitado.')
    engine.reference(field['id'], field['name'], enabled=True)
    content = engine.text(field['id'], field['name'])
    if not content['readable'] or content['truncated']:
        raise PreconditionMissing('Campo de busca não permite leitura completa; nada foi digitado.')
    return field


def result_proof(engine: Any, observation: dict[str, Any], query: str) -> dict[str, Any] | None:
    """A fresh results row with independently rendered query text (not its own Name)."""
    by_id = {row['id']: row for row in observation['elements']}
    grids = {row['id'] for row in observation['elements']
             if row['type'] == 'DataGrid' and fold(row['name']) in RESULT_LABELS
             and not row.get('offscreen')}
    for row in observation['elements']:
        if row['type'] != 'DataItem' or row.get('offscreen') or not row.get('name'):
            continue
        ancestor = row.get('parent', '')
        seen: set[str] = set()
        while ancestor and ancestor not in grids and ancestor not in seen:
            seen.add(ancestor)
            ancestor = by_id.get(ancestor, {}).get('parent', '')
        if ancestor not in grids or fold(query) not in fold(row['name']):
            continue
        # Finding a row is not selecting/playing it; multiple results are fine. Each
        # candidate is checked, not used as proof of its own accessible label.
        proof = engine.verify(row['id'], row['name'], expected_text=row['name'])
        if proof['verified']:
            # Preserve the operation the provider answered: only a native `uia_verify` read
            # resolves the pending dispatch; workflow identity goes as metadata (B0 measured).
            query_sha256 = hashlib.sha256(query.encode('utf-8')).hexdigest()
            result = {**proof, 'workflow': 'spotify_search', 'results_verified': True,
                      'query_length': len(query), 'query_sha256': query_sha256}
            result['association'] = issue_association(result, query_sha256)
            return result
    return None


def results_signature(observation: dict[str, Any]) -> tuple[str, ...]:
    """Semantic rows in results grids, independent of regenerated observation IDs."""
    grids = {row['id'] for row in observation['elements']
             if row['type'] == 'DataGrid' and fold(row['name']) in RESULT_LABELS}
    return tuple(row['name'] for row in observation['elements']
                 if row['type'] == 'DataItem' and row.get('parent') in grids)


async def spotify_search(runtime: Any, plan: dict[str, Any]) -> dict[str, Any]:
    """Search once, then poll fresh field + results; never retry an uncertain write."""
    query = plan.get('query')
    if not isinstance(query, str) or not query.strip() or len(query) > 200 or any(ord(c) < 32 for c in query):
        raise ValueError('Consulta inválida; use 1 a 200 caracteres sem controles.')
    observation_ms = execution_ms = verification_ms = 0.0
    actions = 0
    matches = runtime.tools.target_windows(('spotify.exe',))
    if len(matches) > 1:
        raise PreconditionMissing('Mais de uma janela do Spotify; escolha a janela antes de buscar.')
    if not matches:
        if not plan.get('target'):
            raise PreconditionMissing('Spotify não está aberto nem identificado no índice.')
        started = time.perf_counter()
        launched = await runtime.tools.launch(
            plan['target'], args=plan.get('args') or (), workdir=plan.get('workdir') or '',
            expected=plan.get('expected') or '', aumid=plan.get('aumid') or '',
            trust=plan.get('trust') or '', processes=('spotify.exe',))
        execution_ms += (time.perf_counter() - started) * 1000
        actions += 1
        runtime.actions = actions
        matches = [launched]
    started = time.perf_counter()
    await runtime.bind_selected_window(handle=int(matches[0]['handle']), label='',
                                       window_name=str(matches[0].get('title') or ''), source='search_workflow')
    engine = runtime.tools.uia
    observation = engine.observe()
    deadline = time.perf_counter() + (FIELD_READY_SECONDS if actions else 0.0)
    while True:
        try:
            field = query_field(engine, observation)
            break
        except PreconditionMissing as exc:
            if not actions:
                # Spotify was already open and its search header is not what it must be:
                # nothing has been typed, so the request declines to the agent.
                raise
            if time.perf_counter() >= deadline:
                # A launch already ran: it must not be repeated by a fallback planner, and
                # the field never appeared — unverified work, never a replay.
                raise UIAError('verification_failed', str(exc)) from exc
            # The application was just started: its window is proven, but the search field
            # inside it may still be rendering. Re-read, bounded, before giving up.
            await asyncio.sleep(0.15)
            observation = engine.observe()
    observation_ms += (time.perf_counter() - started) * 1000
    before_results = results_signature(observation)
    before_query = engine.text(field['id'], field['name'])['text']
    # Already fulfilled: verify the existing results, rather than issuing another search.
    if before_query == query:
        proof = result_proof(engine, observation, query)
        if proof:
            runtime.tools.evidence.append(proof)
            return {'actions': actions, 'steps': 1, 'execution_time_ms': execution_ms,
                    'observation_time_ms': observation_ms, 'verification_time_ms': 0.0,
                    'polls': 0, 'message': 'Consulta já aberta no Spotify; resultados verificados por UIA.'}
    started = time.perf_counter()
    # SetValue where available; fallback text is literal-escaped by UIAEngine.
    # Both operations use the existing UFO AtomicCommand executor and live identity.
    runtime.actions = actions + 1
    engine.set_text(field['id'], field['name'], query, replace=True)
    runtime.tools.pending_ui = True
    runtime.actions += 1
    engine.keyboard(field['id'], field['name'], '{ENTER}')
    execution_ms += (time.perf_counter() - started) * 1000
    deadline = time.perf_counter() + 5.0
    polls = 0
    while True:
        started = time.perf_counter()
        fresh = engine.observe()
        observation_ms += (time.perf_counter() - started) * 1000
        polls += 1
        started = time.perf_counter()
        try:
            current = query_field(engine, fresh)
            content = engine.text(current['id'], current['name'])
            changed_results = before_query == query or results_signature(fresh) != before_results
            proof = (result_proof(engine, fresh, query)
                     if content['readable'] and not content['truncated'] and content['text'] == query
                     and changed_results else None)
        except (UIAError, PreconditionMissing):
            # Reading while the page transitions proves nothing. Only the read is
            # retried; the original mutation is never sent again.
            proof = None
        verification_ms += (time.perf_counter() - started) * 1000
        if proof is not None:
            runtime.tools.evidence.append(proof)
            runtime.tools.pending_ui = False
            return {'actions': runtime.actions, 'steps': 1,
                    'execution_time_ms': execution_ms, 'observation_time_ms': observation_ms,
                    'verification_time_ms': verification_ms, 'polls': polls,
                    'message': 'Busca no Spotify concluída: consulta e conteúdo dos resultados verificados por UIA.'}
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise UIAError('verification_failed',
                           'Consulta enviada ao Spotify, mas resultados não confirmados em 5 s; não foi repetida.')
        await asyncio.sleep(min(0.1, remaining))
