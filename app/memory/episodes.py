from __future__ import annotations

from typing import Any

from app.memory.contracts import MemoryItem
from app.memory.policy import MemoryPolicyError, identifier, safe_text
from app.memory.commands_helpers import history_allowed

OUTCOMES = {'SUCCESS', 'FAILED', 'NOT_VERIFIED', 'CANCELLED'}


def episode_item(result: Any) -> MemoryItem | None:
    """Uses only controller-approved working instruction and a small scalar allowlist."""
    scope = getattr(result, 'scope', '')
    if scope not in {'end_to_end', 'engine_only'}:
        return None
    task_id = identifier(getattr(result, 'task_id', None))
    instruction = safe_text(getattr(result, 'instruction', ''), maximum=400)
    if not history_allowed(instruction):
        return None
    outcome = getattr(result, 'status', '')
    if outcome not in OUTCOMES:
        raise MemoryPolicyError()
    # No evidence is persisted. Its integrity signal may only downgrade the label.
    if outcome == 'SUCCESS' and (getattr(result, 'false_success', False) or not getattr(result, 'evidence', [])):
        outcome = 'NOT_VERIFIED'
    llm_calls = getattr(result, 'llm_calls', 0)
    plan_source = getattr(result, 'plan_source', '') or 'unspecified'
    if plan_source not in {'unspecified', 'llm', 'deterministic', 'deterministic+llm'}:
        raise MemoryPolicyError()
    failure = getattr(result, 'failure_kind', '') or 'none'
    if failure not in {'none', 'PLANNING_FAILURE', 'ACTION_FAILURE', 'VERIFICATION_FAILURE',
                       'PROVIDER_FAILURE', 'CONFIGURATION', 'AUTHORIZATION_REFUSED', 'TIMEOUT',
                       'CANCELLED', 'INTERNAL_ERROR'}:
        raise MemoryPolicyError()
    apps = getattr(result, 'apps', [])
    # Engine evidence titles/paths/content are intentionally never copied to apps.
    if not isinstance(apps, list):
        raise MemoryPolicyError()
    summary = f'Tarefa: {instruction}\nResultado registrado: {outcome}. Planejamento: {plan_source}.'
    return MemoryItem(kind='episode', text=summary, source='episode', metadata={
        'task_id': task_id, 'instruction': instruction, 'outcome': outcome, 'apps': apps,
        'plan_source': plan_source, 'llm_calls': llm_calls, 'failure_kind': failure,
        'paths_are_hypotheses': True,
    })
