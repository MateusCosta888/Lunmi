from __future__ import annotations

import re

from app.memory.policy import normalized


def history_allowed(text: str) -> bool:
    """Do not turn profile/control statements into a second recallable copy."""
    plain = normalized(text)
    return not bool(re.search(
        r'\b(lembra\w*|lembre\w*|memoriz\w*|esquec\w*|esqueca|memorias?|prefiro|preferencia\w*|'
        r'nao quero mais|nao uso mais|daqui pra frente|dai pra frente|de agora em diante|'
        r'remember|forget|prefer\w*)\b', plain))
