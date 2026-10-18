"""Documented reasoning knobs, scoped to the actual endpoint and model family."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from app.settings import ConfigurationError, Settings


def reasoning_parameters(settings: Settings) -> dict[str, Any]:
    """Empty/default and unknown destinations never get guessed parameters."""
    effort = settings.effort.strip()
    if not effort:
        return {}
    provider, model = settings.provider, settings.model.removeprefix('models/')
    if provider == 'openai':
        # https://developers.openai.com/api/reference/resources/chat
        if re.fullmatch(r'(?:o1|o3|o4-mini)(?:-\d{4}-\d{2}-\d{2})?', model):
            allowed = {'low', 'medium', 'high'}
        elif re.fullmatch(r'gpt-5(?:-mini|-nano)?(?:-\d{4}-\d{2}-\d{2})?', model):
            allowed = {'minimal', 'low', 'medium', 'high'}
        else:
            return {}
        _validate(effort, allowed)
        return {'reasoning_effort': effort}
    if provider == 'gemini':
        # https://ai.google.dev/gemini-api/docs/openai#thinking
        endpoint = urlsplit(settings.base_url or 'https://generativelanguage.googleapis.com/v1beta/openai')
        if (endpoint.scheme != 'https' or endpoint.hostname != 'generativelanguage.googleapis.com'
                or endpoint.port not in {None, 443} or endpoint.path.rstrip('/') != '/v1beta/openai'
                or endpoint.query or endpoint.fragment or endpoint.username or endpoint.password):
            return {}
        if re.fullmatch(r'gemini-2\.5-(?:flash|flash-lite|pro)(?:-preview-[\d-]+)?', model):
            allowed = {'minimal', 'low', 'medium', 'high'}
            if '-pro' not in model:
                allowed.add('none')
        elif re.fullmatch(r'gemini-3(?:\.\d+)?-flash(?:-lite)?(?:-preview)?', model):
            allowed = {'minimal', 'low', 'medium', 'high'}
        elif re.fullmatch(r'gemini-3(?:\.\d+)?-pro(?:-preview)?', model):
            allowed = {'low', 'high'}
        else:
            return {}
        _validate(effort, allowed)
        return {'reasoning_effort': effort}
    if provider == 'openrouter':
        # https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
        endpoint = urlsplit(settings.base_url or 'https://openrouter.ai/api/v1')
        if (endpoint.scheme != 'https' or endpoint.hostname != 'openrouter.ai'
                or endpoint.port not in {None, 443} or endpoint.path.rstrip('/') != '/api/v1'
                or endpoint.query or endpoint.fragment or endpoint.username or endpoint.password):
            return {}
        # Only the documented fixed families; auto routers/unknown models stay unknown.
        if re.fullmatch(r'openai/(?:o1|o3|o4-mini)(?:-\d{4}-\d{2}-\d{2})?', model):
            allowed = {'low', 'medium', 'high'}
        elif re.fullmatch(r'google/gemini-3(?:\.\d+)?-(?:flash|pro)(?:-preview)?', model):
            allowed = {'minimal', 'low', 'medium', 'high', 'xhigh'}
        else:
            return {}
        _validate(effort, allowed)
        return {'reasoning': {'effort': effort, 'exclude': True}}
    if provider == 'anthropic' and re.fullmatch(r'claude-(?:opus|sonnet)-4-6(?:-\d{8})?', model):
        # https://platform.claude.com/docs/en/build-with-claude/effort
        _validate(effort, {'low', 'medium', 'high', 'max'})
        return {'output_config': {'effort': effort}}
    return {}


def _validate(effort: str, allowed: set[str]) -> None:
    if effort not in allowed:
        raise ConfigurationError('Esforço de raciocínio não suportado pelo destino selecionado. Use: '
                                 + ', '.join(sorted(allowed)) + '.')
