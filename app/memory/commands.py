from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from app.memory.contracts import MemoryItem
from app.memory.manager import MemoryManager
from app.memory.policy import MemoryPolicyError, normalized


def command(instruction: str, memory: MemoryManager) -> str | None:
    """None means ordinary task/chat; an ambiguous deletion never chooses for the user."""
    value = instruction.strip()
    plain = normalized(value).rstrip(' .?!')
    if plain in {'o que voce lembra', 'o que lembra', 'listar memorias', 'minhas memorias'}:
        items = memory.list(limit=20)
        return '\n'.join(f'{item.id}: {item.text}' for item in items) or 'Nenhuma memória ativa.'
    search = re.match(r'^(?:buscar|busca|procure)\s+mem[oó]rias?\s*[: ]\s*(.+)$', value, re.I)
    if search:
        items = memory.list(query=search[1], limit=20)
        return '\n'.join(f'{item.id}: {item.text}' for item in items) or 'Nenhuma memória encontrada.'
    forget = re.match(r'^(?:esque[cç]a|esquece|apague a mem[oó]ria)\s+(.+)$', value, re.I)
    if forget:
        target = forget[1].strip().rstrip('.')
        items = memory.list(limit=100)
        exact = [item for item in items if item.id == target or normalized(item.text).rstrip(' .') == normalized(target)]
        matches = exact or memory.list(query=target, limit=100)
        if len(matches) != 1:
            return 'Seleção ausente ou ambígua; use o ID da memória. Nada foi apagado.'
        memory.delete(matches[0].id)
        return 'Memória apagada.'
    invalidate = re.match(r'^(?:n[aã]o quero mais (?:usar )?|n[aã]o uso mais )(.+)$', value, re.I)
    if invalidate:
        target = invalidate[1].strip().rstrip('.')
        matches = [item for item in memory.list(query=target, limit=100)
                   if item.kind in {'preference', 'habit', 'decision'} and
                   normalized(target) in normalized(item.text)]
        if len(matches) != 1:
            return 'Seleção ausente ou ambígua; use esquece com o ID. Nada foi apagado.'
        memory.delete(matches[0].id)
        return 'Preferência invalidada.'
    remember = re.match(r'^(?:lembra(?: que)?|lembre(?:-se)?(?: de)?(?: que)?|memorize(?: que)?)\s*[:,]?\s+(.+)$', value, re.I)
    preference = re.match(r'^(?:eu\s+)?prefiro\s+.+$', value, re.I)
    forward = re.match(r'^(?:da[ií] pra frente|de agora em diante)\s*[, :]\s*(.+)$', value, re.I)
    if not remember and not preference and not forward:
        return None
    if not memory.options()['enabled']:
        return 'Memória desabilitada; nada foi gravado.'
    text = remember[1] if remember else (forward[1] if forward else value)
    kind = 'preference' if re.search(r'\b(prefiro|prefer[eê]ncia|respostas?)\b', text, re.I) else 'fact'
    meta = {}
    if re.search(r'\b(hoje|agora|esta semana)\b', text, re.I):
        kind = 'fact'
        meta['expires_at'] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    # Only narrowly defined explicit antonyms share a conflict key. Do not guess
    # arbitrary semantic conflicts or use a model's assertion as authority.
    if kind == 'preference' and re.search(r'\brespostas?\s+(curtas?|longas?|breves?|detalhadas?)\b', text, re.I):
        meta['conflict_key'] = 'response_length'
    try:
        memory.remember(MemoryItem(kind=kind, text=text, metadata=meta))
    except MemoryPolicyError as exc:
        return str(exc)
    return 'Memória guardada.'
