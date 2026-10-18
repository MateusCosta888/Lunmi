from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from app.memory.contracts import MemoryItem
from app.memory.manager import MemoryManager
from app.memory.policy import MemoryPolicyError, normalized


# Formas explícitas PT-BR/PT-PT. Só estas (ou o prefixo "prefiro"/"daqui pra frente")
# gravam; uma pergunta comum que mencione memória não é comando.
REMEMBER = re.compile(
    r"^(?:"
    r"lembra(?:-te)?(?:-se)?(?: que)?|"
    r"lembre(?:-se)?(?: de)?(?: que)?|"
    r"memoriza(?: que)?|memorize(?: que)?|"
    r"guarda(?: isto| isso| a[ií])?(?: que)?|"
    r"anota(?: a[ií])?(?: que)?|"
    r"(?:quero|preciso) que (?:voc[eê]|tu) (?:se )?lembre(?:-se)?(?: de)?(?: que)?|"
    r"podes? (?:te )?lembrar(?:-te)?(?: de)?(?: que)?"
    r")\s*[:,]?\s+(.+)$",
    re.I,
)
PREFERENCE = re.compile(r"^(?:eu\s+)?prefiro\s+.+$", re.I)
FORWARD = re.compile(r"^(?:da[ií] pra frente|de agora em diante)\s*[, :]\s*(.+)$", re.I)
QUESTION = re.compile(r"\?\s*$")


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
        # Exige alvo único e exato: uma palavra solta nunca apaga um bloco com outras informações.
        removed = memory.forget_by_text(target)
        if removed == 0:
            return ('Seleção ausente ou ambígua; nada foi apagado. '
                    'Repita o trecho exato que quer esquecer.')
        return 'Memória apagada.'
    invalidate = re.match(r'^(?:n[aã]o quero mais (?:usar )?|n[aã]o uso mais )(.+)$', value, re.I)
    if invalidate:
        target = invalidate[1].strip().rstrip('.')
        removed = memory.forget_by_text(target, allow_suffix=True)
        if removed == 0:
            return ('Seleção ausente ou ambígua; nada foi apagado. '
                    'Repita o trecho exato da preferência.')
        return 'Preferência invalidada.'
    remember = REMEMBER.match(value)
    preference = PREFERENCE.match(value)
    forward = FORWARD.match(value)
    if not remember and not preference and not forward:
        return None
    if not remember and QUESTION.search(value):
        # Uma pergunta comum ("prefiro X?") não é uma declaração para guardar.
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
