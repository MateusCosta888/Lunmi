"""Small constraints for the observed query failures, not a general web ranker."""
from __future__ import annotations

import re
import unicodedata
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

_FINANCIAL_FOOTER = re.compile(r'divulgacao de riscos\s*:\s*negociar instrumentos financeiros')
_FX_QUOTE_CURRENCY = re.compile(r'\b(?:dolar|dolares|usd|euro|euros|eur)\b')
_FX_QUOTE_MARK = re.compile(
    r'\b(?:quanto|valor|preco|cotacao|taxa|cambio|esta|estao|vale|hoje|agora)\b')
_FX_EXPLANATION = re.compile(r'^(?:explica|explique|o que e|como funciona)\b')


def normalized(text: str) -> str:
    return ''.join(char for char in unicodedata.normalize('NFKD', text.casefold())
                   if not unicodedata.combining(char))


def currency_quote_question(text: str) -> bool:
    """The router and evidence policy must agree on requests such as `Dólar hoje`."""
    text = normalized(text)
    if _FX_EXPLANATION.match(text):
        return False
    return bool(_FX_QUOTE_CURRENCY.search(text) and _FX_QUOTE_MARK.search(text))


# Fatos mutáveis (contagem, cargo, fixture, preço, valuation) exigem uma observação
# recente e não apenas texto útil. 183 dias é o limite fechado para esta política;
# não é um timeout nem um prazo de rede.
CURRENT_EVIDENCE_MAX_AGE_DAYS = 183

_DEFINITION = re.compile(
    r'^(?:o que (?:e|sao|significa)|what (?:is|are)|'
    r'explica(?:r|me)?\s+(?:o que|como|a definicao)|explique\s+(?:o que|como)|'
    r'como funciona\b|defin(?:e|icao|ition)\b|qual (?:e )?o significado\b)')
_OFFICE = re.compile(
    r'\b(?:presidente|presidenta|primeiro[- ]ministro|primeira[- ]ministra|prefeito|prefeita|'
    r'governador|governadora|president|prime minister|mayor|governor)\b')
_OFFICE_CURRENT = re.compile(
    r'\b(?:atual|atuais|current|hoje|agora|em exercicio|no cargo|who is|quem e)\b')
_OFFICE_PAST = re.compile(r'\b(?:foi|foram|was|were|historico|history)\b')
_FIXTURE = re.compile(
    r'\b(?:proximo|proxima|next)\s+(?:jogo|partida|encontro|match|fixture)\b|'
    r'\b(?:jogo|partida|match|fixture)\s+(?:de hoje|hoje|amanha|de amanha)\b')
_VALUATION = re.compile(
    r'\b(?:valuation|valor de mercado|market cap|capitalizacao(?: bolsista)?|'
    r'avaliad[ao] (?:em|por)|quanto valem?)\b')
_PRICE = re.compile(
    r'\b(?:preco|precos|price|prices|custa|custam|cost|costs|cotacao|quote|quotation)\b')
_COMPARISON = re.compile(
    r'\b(?:melhor(?:es)? preco|preco mais baixo|mais barat[ao]|onde comprar|'
    r'best price|cheapest|compare prices|comparar precos)\b')
_COUNT = re.compile(r'\b(?:quantos?|quantas?|how many|numero de|numero total de|total de|count of)\b')

_COUNT_UNITS: dict[str, tuple[str, ...]] = {
    'chapter': ('capitulo', 'capitulos', 'chapter', 'chapters'),
    'volume': ('volume', 'volumes'),
    'season': ('temporada', 'temporadas', 'season', 'seasons'),
    'episode': ('episodio', 'episodios', 'episode', 'episodes'),
    'issue': ('edicao', 'edicoes', 'issue', 'issues'),
    'film': ('filme', 'filmes', 'movie', 'movies', 'film', 'films'),
    'book': ('livro', 'livros', 'book', 'books'),
    'page': ('pagina', 'paginas', 'page', 'pages'),
    'year': ('ano', 'anos', 'year', 'years'),
    'month': ('mes', 'meses', 'month', 'months'),
    'day': ('dia', 'dias', 'day', 'days'),
}
_UNIT_WORDS = {alias for aliases in _COUNT_UNITS.values() for alias in aliases}
_ATTRIBUTE_STOP = {
    'quantos', 'quantas', 'how', 'many', 'numero', 'total', 'count', 'tem', 'hoje', 'atual',
    'atuais', 'agora', 'current', 'latest', 'melhor', 'best', 'preco', 'precos', 'price',
    'prices', 'custa', 'custam', 'cost', 'cotacao', 'quote', 'quem', 'where', 'onde', 'site',
    'vale', 'valor', 'quanto', 'presidente', 'presidenta', 'president', 'primeiro', 'primeira',
    'ministro', 'ministra', 'prefeito', 'prefeita', 'governador', 'governadora', 'mayor',
    'governor', 'proximo', 'proxima', 'next', 'jogo', 'partida', 'fixture', 'match', 'qual',
    'quais', 'como',
}
_NUMBER_WORDS = {
    'zero': 0, 'um': 1, 'uma': 1, 'dois': 2, 'duas': 2, 'tres': 3, 'quatro': 4, 'cinco': 5,
    'seis': 6, 'sete': 7, 'oito': 8, 'nove': 9, 'dez': 10, 'onze': 11, 'doze': 12, 'treze': 13,
    'catorze': 14, 'quatorze': 14, 'quinze': 15, 'dezasseis': 16, 'dezesseis': 16,
    'dezassete': 17, 'dezessete': 17, 'dezoito': 18, 'dezanove': 19, 'dezenove': 19,
    'vinte': 20, 'trinta': 30, 'quarenta': 40, 'cinquenta': 50, 'sessenta': 60, 'setenta': 70,
    'oitenta': 80, 'noventa': 90, 'cem': 100, 'cento': 100, 'mil': 1000, 'one': 1, 'two': 2,
    'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14, 'fifteen': 15, 'sixteen': 16,
    'seventeen': 17, 'eighteen': 18, 'nineteen': 19, 'twenty': 20, 'thirty': 30, 'forty': 40,
    'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90, 'hundred': 100,
    'thousand': 1000,
}
_PT_TENS = ('vinte', 'trinta', 'quarenta', 'cinquenta', 'sessenta', 'setenta', 'oitenta', 'noventa')
_PT_UNITS = ('um', 'dois', 'tres', 'quatro', 'cinco', 'seis', 'sete', 'oito', 'nove')
_EN_TENS = ('twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety')
_EN_UNITS = ('one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine')
_WORD_ALT = '|'.join(sorted(map(re.escape, _NUMBER_WORDS), key=len, reverse=True))
_CARDINAL = (
    r'(?<!\w)(?:\d{1,4}|' + _WORD_ALT
    + r'|(?:' + '|'.join(_PT_TENS) + r')\s+e\s+(?:' + '|'.join(_PT_UNITS) + r')'
    + r'|(?:' + '|'.join(_EN_TENS) + r')[- ](?:' + '|'.join(_EN_UNITS) + r'))(?!\w)')
_RELEASED = re.compile(
    r'\b(?:estreou|estrearam|premiered|premiere|aired|released|launched|debut(?:ed)?|'
    r'lancad[ao]s?|publicad[ao]s?|disponiveis?|available)\b')
_ANNOUNCED = re.compile(
    r'\b(?:renovad[ao]s?|renewed|anunciad[ao]s?|announced|confirmad[ao]s?|confirmed|'
    r'coming|proxim[ao]|futur[ao])\b')
_ELECTION = re.compile(
    r'\b(?:eleit[oa]s?|elected|candidat[oa]s?|candidate|eleic(?:ao|oes)|'
    r'toma posse|tomou posse|tomara posse|empossad[oa]s?|sworn in|take office|'
    r'presidente eleito|president-elect)\b')
_INCUMBENT = re.compile(
    r'\b(?:atual|atuais|em exercicio|no cargo|currently|is the current|'
    r'serves as|desde)\b')
_NAME = re.compile(r'\b[A-ZÁÉÍÓÚÂÊÔÃÕÇ][a-záéíóúâêôãõç]+')
_MONTHS = (
    'janeiro', 'fevereiro', 'marco', 'abril', 'maio', 'junho', 'julho', 'agosto', 'setembro',
    'outubro', 'novembro', 'dezembro', 'january', 'february', 'march', 'april', 'may', 'june',
    'july', 'august', 'september', 'october', 'november', 'december')
_DATE = re.compile(
    r'\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b|'
    r'\b\d{1,2}\s+de\s+(?:' + '|'.join(_MONTHS) + r')\b|'
    r'\b(?:' + '|'.join(_MONTHS) + r')\s+\d{1,2}\b|'
    r'\b20\d{2}-\d{2}-\d{2}\b')
_MONTH_NUMBERS = {name: index + 1 for index, name in enumerate(_MONTHS[:12])}
_MONTH_NUMBERS.update({name: index + 1 for index, name in enumerate(_MONTHS[12:])})
_PT_FIXTURE_DATE = re.compile(
    r'\b(\d{1,2})\s+de\s+(' + '|'.join(_MONTHS[:12]) + r')\s+de\s+(\d{4})\b')
_EN_FIXTURE_DATE = re.compile(
    r'\b(' + '|'.join(_MONTHS[12:]) + r')\s+(\d{1,2})(?:,)?\s+(\d{4})\b')
_ISO_FIXTURE_DATE = re.compile(r'\b(20\d{2})-(\d{2})-(\d{2})\b')
_SLASH_FIXTURE_DATE = re.compile(r'\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b')
_FIXTURE_MATCH = re.compile(r'\b(?:vs|contra|x|enfrenta|defronta|recebe|visita)\b')
_VARIANTS = ('pro max', 'pro', 'max', 'plus', 'ultra', 'air', 'mini', 'lite', 'se', 'fe')
_REGION_CURRENCY = (
    (re.compile(r'\b(?:portugal|portugues[ae]?|lisboa|pt-pt)\b'), 'EUR'),
    (re.compile(r'\b(?:brasil|brazil|brasileir[ao]|brl)\b'), 'BRL'),
    (re.compile(r'\b(?:eua|usa|united states|estados unidos|usd)\b'), 'USD'),
    (re.compile(r'\b(?:reino unido|uk|gbp|libra)\b'), 'GBP'),
)
_CURRENCY_CODES = (
    (re.compile(r'(?:us\$|\busd\b|dolar(?:es)?\b|dollar(?:s)?\b)'), 'USD'),
    (re.compile(r'(?:€|\beur\b|euro(?:s)?\b)'), 'EUR'),
    (re.compile(r'(?:r\$|\bbrl\b|\breais?\b)'), 'BRL'),
    (re.compile(r'(?:£|\bgbp\b|libra(?:s)?\b|pound(?:s)?\b)'), 'GBP'),
)
_CURRENCY = (
    r'(?:us\$|r\$|€|\$|£|\busd\b|\beur\b|\bbrl\b|\bgbp\b|dolar(?:es)?\b|'
    r'dollar(?:s)?\b|euros?\b|reais?\b|libra(?:s)?\b|pounds?\b)')
_SCALE = (
    r'(?:mil|milhao|milhoes|bilhao|bilhoes|trilhao|trilhoes|thousand|million|billion|trillion)')
_CURRENCY_AMOUNT = re.compile(
    r'(?:\d[\d.,]*\s*(?:' + _SCALE + r'\s*){0,2}(?:de\s+)?' + _CURRENCY
    + r'|(?:us\$|r\$|€|\$|£|\busd\b|\beur\b|\bbrl\b|\bgbp\b)\s*\d[\d.,]*)')


def requested_attribute(query: str) -> str:
    """Atributo finito pedido: '', count, office, fixture, price ou valuation."""
    text = normalized(query).strip()
    if not text or _DEFINITION.match(text):
        return ''
    if _OFFICE.search(text) and _OFFICE_CURRENT.search(text) and not _OFFICE_PAST.search(text):
        return 'office'
    if _FIXTURE.search(text):
        return 'fixture'
    if _VALUATION.search(text):
        return 'valuation'
    if _PRICE.search(text):
        return 'price'
    if _COUNT.search(text) and _count_unit(text):
        return 'count'
    return ''


def _count_unit(text: str) -> str:
    earliest: tuple[int, str] | None = None
    for unit, aliases in _COUNT_UNITS.items():
        for alias in aliases:
            match = re.search(r'\b' + re.escape(alias) + r'\b', text)
            if match and (earliest is None or match.start() < earliest[0]):
                earliest = (match.start(), unit)
    return earliest[1] if earliest else ''


def _subject_terms(query: str) -> set[str]:
    return {term for term in _terms(query)
            if term not in _ATTRIBUTE_STOP and term not in _UNIT_WORDS}


def _number_value(raw: str) -> int | None:
    value = normalized(raw).strip()
    if value.isdigit():
        return int(value)
    if value in _NUMBER_WORDS:
        return _NUMBER_WORDS[value]
    parts = value.replace('-', ' ').split()
    if len(parts) == 3 and parts[1] == 'e':
        return _NUMBER_WORDS[parts[0]] + _NUMBER_WORDS[parts[2]]
    if len(parts) == 2:
        return _NUMBER_WORDS[parts[0]] + _NUMBER_WORDS[parts[1]]
    return None


def _stated_count(unit: str, text: str) -> int | None:
    aliases = '|'.join(re.escape(alias) for alias in _COUNT_UNITS[unit])
    patterns = (
        r'(?P<count>' + _CARDINAL + r')\s+(?:[a-z]+\s+)?(?:' + aliases + r')\b',
        r'\b(?:' + aliases + r')\s*[:=]\s*(?P<count>' + _CARDINAL + r')',
    )
    for sentence in _sentences(text):
        normalized_sentence = normalized(sentence)
        for pattern in patterns:
            for match in re.finditer(pattern, normalized_sentence):
                before = normalized_sentence[max(0, match.start() - 50):match.start()]
                after = normalized_sentence[match.end():match.end() + 50]
                announced = bool(_ANNOUNCED.search(before) or _ANNOUNCED.search(after))
                released = bool(_RELEASED.search(before) or _RELEASED.search(after))
                # `renovada para 12 temporadas` é cardinal anunciado, não lançado;
                # `9 temporadas lançadas` mantém a contagem mesmo com renovação ao lado.
                if announced and not released:
                    continue
                value = _number_value(match.group('count'))
                if value is not None:
                    return value
    return None


def _latest_released(text: str, unit: str) -> int | None:
    aliases = '|'.join(re.escape(alias) for alias in _COUNT_UNITS[unit])
    latest: int | None = None
    patterns = (
        r'\b(?:' + aliases + r')\s+(\d{1,4})\b',
        r'\b(\d{1,4})[aº]?\s*(?:' + aliases + r')\b',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            number = int(match[1])
            window = text[max(0, match.start() - 90):match.end() + 90]
            if _RELEASED.search(window) and not _ANNOUNCED.search(window):
                latest = number if latest is None else max(latest, number)
    return latest


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r'(?<=[.!?])\s+|\n+', text) if part.strip()]


def _currencies(text: str) -> set[str]:
    text = normalized(text)
    return {code for pattern, code in _CURRENCY_CODES if pattern.search(text)}


def _supports_count(query: str, text: str) -> bool:
    query_text = normalized(query)
    text = normalized(text)
    unit = _count_unit(query_text)
    if not unit:
        return False
    subject = _subject_terms(query_text)
    if subject and not subject & _terms(text):
        return False
    count = _stated_count(unit, text)
    if count is None:
        return False
    latest = _latest_released(text, unit)
    return latest is None or count >= latest


def _supports_office(query: str, text: str) -> bool:
    office = _OFFICE.search(normalized(query))
    if office is None:
        return False
    subject = _subject_terms(query)
    for sentence in _sentences(text):
        normalized_sentence = normalized(sentence)
        if office.group(0) not in normalized_sentence:
            continue
        if subject and not subject & _terms(normalized_sentence):
            continue
        if _ELECTION.search(normalized_sentence) and not _INCUMBENT.search(
                normalized_sentence):
            continue
        capitals = _NAME.findall(sentence)
        first = sentence.split()[0] if sentence.split() else ''
        if capitals and capitals[0] == first:
            capitals = capitals[1:]
        capitals = [word for word in capitals
                    if normalized(word) not in subject | _ATTRIBUTE_STOP]
        if capitals or re.search(r'\b(?:em exercicio|no cargo|is the current)\b',
                                 normalized_sentence):
            return True
    return False


def _fixture_dates(text: str) -> list[date]:
    """Datas completas de evento com ano; não infere ano em datas parciais."""
    text = normalized(text)
    found: list[date] = []
    for match in _PT_FIXTURE_DATE.finditer(text):
        try:
            found.append(date(int(match[3]), _MONTH_NUMBERS[match[2]], int(match[1])))
        except (KeyError, ValueError):
            continue
    for match in _EN_FIXTURE_DATE.finditer(text):
        try:
            found.append(date(int(match[3]), _MONTH_NUMBERS[match[1]], int(match[2])))
        except (KeyError, ValueError):
            continue
    for match in _ISO_FIXTURE_DATE.finditer(text):
        try:
            found.append(date.fromisoformat(match[0]))
        except ValueError:
            continue
    for match in _SLASH_FIXTURE_DATE.finditer(text):
        try:
            found.append(date(int(match[3]), int(match[2]), int(match[1])))
        except ValueError:
            continue
    return found


def _supports_fixture(query: str, text: str, *, today: date | None = None) -> bool:
    text = normalized(text)
    subject = _subject_terms(query)
    if subject and not subject & _terms(text):
        return False
    if not _DATE.search(text) or not _FIXTURE_MATCH.search(text):
        return False
    if today is not None:
        event_dates: list[date] = []
        for sentence in _sentences(text):
            if _FIXTURE_MATCH.search(normalized(sentence)):
                event_dates.extend(_fixture_dates(sentence))
        # Datas explícitas na própria frase do confronto; uma data de atualização
        # noutra frase não transforma um evento passado no próximo jogo.
        dates = event_dates or _fixture_dates(text)
        if dates and not any(event >= today for event in dates):
            return False
    return True


def _supports_price(query: str, text: str) -> bool:
    query_text = normalized(query)
    product = _product_model(query_text)
    subject = _subject_terms(query)
    wanted = next((code for pattern, code in _REGION_CURRENCY
                   if pattern.search(query_text)), '')
    for sentence in _sentences(text):
        if not has_quote_value(sentence):
            continue
        normalized_sentence = normalized(sentence)
        if product:
            family, model = product
            if not re.search(r'\b' + re.escape(family) + r'\b', normalized_sentence):
                continue
            if not re.search(r'\b' + re.escape(model) + r'\b', normalized_sentence):
                continue
            unwanted = any(
                re.search(r'\b' + re.escape(family) + r'\s+' + re.escape(model)
                          + r'\s+' + re.escape(variant) + r'\b', normalized_sentence)
                for variant in _VARIANTS
                if not re.search(r'\b' + re.escape(variant) + r'\b', query_text))
            if unwanted:
                continue
        elif subject and not subject & _terms(normalized_sentence):
            continue
        codes = _currencies(sentence)
        if wanted and codes and wanted not in codes:
            continue
        return True
    return False


def _product_model(query: str) -> tuple[str, str] | None:
    match = re.search(r'\b([a-z]{3,})\s+(\d{2,4})\b', query)
    return (match[1], match[2]) if match else None


def _supports_valuation(query: str, text: str) -> bool:
    subject = _subject_terms(query)
    for sentence in _sentences(text):
        normalized_sentence = normalized(sentence)
        if not _VALUATION.search(normalized_sentence):
            continue
        if subject and not subject & _terms(normalized_sentence):
            continue
        if _CURRENCY_AMOUNT.search(normalized_sentence):
            return True
    return False


def supports_requested_attribute(query: str, text: str, *, today: date | None = None) -> bool:
    """Texto contém o sujeito e o atributo finito pedido.

    `today` só afeta fixture: uma data completa passada não sustenta o próximo jogo.
    """
    attribute = requested_attribute(query)
    if not attribute:
        return True
    if attribute == 'count':
        return _supports_count(query, text)
    if attribute == 'office':
        return _supports_office(query, text)
    if attribute == 'fixture':
        return _supports_fixture(query, text, today=today)
    if attribute == 'price':
        return _supports_price(query, text)
    return _supports_valuation(query, text)


def valuation_conflict(documents: list[dict[str, Any]]) -> bool:
    """Valores de valuation em moedas ou datas de observação diferentes."""
    currencies: set[str] = set()
    dates: set[str] = set()
    for document in documents:
        content = str(document.get('content') or '')
        found: set[str] = set()
        for sentence in _sentences(content):
            if _VALUATION.search(normalized(sentence)) and _CURRENCY_AMOUNT.search(
                    normalized(sentence)):
                found |= _currencies(sentence)
        currencies |= found
        published = document.get('published_at')
        if found and published:
            dates.add(str(published)[:10])
    return len(currencies) > 1 or len(dates) > 1


def _document_text(document: dict[str, Any]) -> str:
    return str(document.get('title') or '') + '\n' + str(document.get('content') or '')


def _is_current(document: dict[str, Any], today: date) -> bool:
    if document.get('cached'):
        return False
    value = document.get('published_at')
    if not value:
        return False
    try:
        published = date.fromisoformat(str(value)[:10])
    except ValueError:
        return False
    return 0 <= (today - published).days <= CURRENT_EVIDENCE_MAX_AGE_DAYS


def _offer_hosts(documents: list[dict[str, Any]]) -> set[str]:
    hosts: set[str] = set()
    for document in documents:
        host = (urlsplit(str(document.get('url') or '')).hostname or '').casefold()
        if host:
            hosts.add(host.removeprefix('www.'))
    return hosts


def attribute_evidence_problem(
    query: str,
    documents: list[dict[str, Any]],
    *,
    today: date | None = None,
) -> str:
    """'' quando há suporte atual; senão um código fechado de limitação.

    Códigos: missing, stale, cached_only, single_offer, mixed_valuation. A data de
    publicação e o estado de cache fazem parte do suporte: texto útil não é prova de
    atualidade. Prices de comparação precisam de dois vendedores independentes.
    """
    attribute = requested_attribute(query)
    if not attribute:
        return ''
    current = today or datetime.now().date()
    supported = [document for document in documents
                 if supports_requested_attribute(query, _document_text(document),
                                                 today=current)]
    if not supported:
        if attribute == 'fixture' and any(
                supports_requested_attribute(query, _document_text(document))
                for document in documents):
            return 'stale'
        return 'missing'
    fresh = [document for document in supported if _is_current(document, current)]
    if fresh:
        if attribute == 'valuation' and valuation_conflict(fresh):
            return 'mixed_valuation'
        if attribute == 'price' and _COMPARISON.search(normalized(query)):
            if len(_offer_hosts(fresh)) < 2:
                return 'single_offer'
        return ''
    if any(document.get('cached') for document in supported):
        return 'cached_only'
    return 'stale'


@dataclass(frozen=True)
class WebConstraints:
    topic: str = ''
    official: bool = False
    domains: tuple[str, ...] = ()
    freshness: str = ''
    quote: bool = False
    read_required: bool = False
    start_date: date | None = None
    end_date: date | None = None
    depth: str = 'quick'

    def parameters(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.domains:
            result['include_domains'] = list(self.domains)
        if self.freshness:
            result.update(freshness=self.freshness, category='news')
        return result

    def accepts(self, source: dict[str, Any]) -> bool:
        host = (urlsplit(source['url']).hostname or '').casefold()
        if self.domains and not any(
                host == domain or host.endswith('.' + domain) for domain in self.domains):
            return False
        text = normalized(str(source.get('title', '')) + ' ' + str(source.get('snippet', '')))
        if self.topic == 'react':
            if re.search(r'\b(?:resume|curriculo|ats|jobs?|hiring|careers?|empregos?|vagas?)\b', text):
                return False
            return bool(host == 'react.dev' or host.endswith('.react.dev') or (
                re.search(r'\breact\b', text) and re.search(
                    r'\b(?:javascript|reactjs|hooks?|components?|library|biblioteca|release|framework)\b',
                    text) and not re.search(r'\b(?:reacts|reaction|reacoes)\b', text)))
        if self.topic == 'ai':
            return bool(re.search(r'\b(?:artificial intelligence|inteligencia artificial|ai|ia)\b', text)
                        and not re.search(r'\b(?:jobs?|hiring|careers?|empregos?|vagas?)\b', text))
        return True

    def dated(self, source: dict[str, Any]) -> bool:
        if not self.freshness:
            return True
        value = source.get('published_at')
        if not value:
            return False
        try:
            published = date.fromisoformat(str(value)[:10])
        except ValueError:
            return False
        end = self.end_date or datetime.now().date()
        start = self.start_date or end - timedelta(
            days={'day': 0, 'week': 7, 'month': 31, 'year': 366}[self.freshness])
        return start <= published <= end


# `aí`/`ai` is Portuguese filler before an interrogative; `AI`/`IA` stays a subject.
_AI_TOPIC = re.compile(r'\b(?:inteligencia artificial|artificial intelligence|ia|ai)\b')
_PT_AI_FILLER = re.compile(
    r'\bai\b(?=\s*(?:[,;:—-]\s*)?(?:quando|quanto|quantos|quantas|onde|como|qual|quais|quem|'
    r'porque|por que|se|que)\b)')


def _topic(text: str, raw: str) -> str:
    if re.search(r'\breact\b', text):
        return 'react'
    if re.search(r'\b(?:AI|IA)\b', raw):
        return 'ai'
    return 'ai' if _AI_TOPIC.search(_PT_AI_FILLER.sub(' ', text)) else ''


_SOURCE_DETAIL = re.compile(
    r'\b(?:estudo original|artigo original|original (?:study|paper)|paper original|'
    r'estudo cientifico|investigacao premiada|numeros cientificos|dados cientificos|'
    r'(?:metodologia|methodology|protocolo experimental)|'
    r'(?:numeros|valores|dados) (?:cientificos )?exatos|'
    r'capitulo mais recente|latest chapter|'
    r'(?:nota|score|posicao|ranking) (?:e a posicao )?atual)\b')
_RESEARCH_DEEP = re.compile(
    r'\b(?:aprofundad[ao]|investiga(?:cao|r)|detalhad[ao]|vantagens e desvantagens|'
    r'deep research)\b')


def requires_original_study(query: str) -> bool:
    """Keep the requested source kind when the Brain shortens a scientific query."""
    return bool(re.search(
        r'\b(?:estudo|artigo|cientific\w*|metodologia|protocolo experimental|'
        r'investigacao premiada|original (?:study|paper)|methodology)\b', normalized(query)))


def requires_page_evidence(query: str) -> bool:
    """Explicit source verification cannot be answered by a long search excerpt."""
    text = normalized(query)
    if _DEFINITION.match(text):
        return bool(re.search(r'\b(?:segundo|estudo original|paper|fonte|link)\b', text))
    return bool(requested_attribute(query) or _SOURCE_DETAIL.search(text)
                or ('myanimelist' in text and re.search(r'avaliad|ranking|nota|posicao', text)))


def research_depth(query: str) -> str:
    """Reuse the existing depth vocabulary; no extra Brain call to choose reading."""
    text = normalized(query)
    parts = sum(bool(re.search(pattern, text)) for pattern in (
        r'\b(?:metodologia|methodology|methods)\b', r'\b(?:resultados|results)\b',
        r'\b(?:limitacoes|limitations)\b'))
    quantitative_comparison = bool(re.search(r'cientific\w*', text)
                                   and re.search(r'exat\w*', text)
                                   and re.search(r'compar\w*|mais .* do que', text))
    if _RESEARCH_DEEP.search(text) or parts >= 2 or quantitative_comparison:
        return 'deep'
    return 'verified' if requires_page_evidence(query) else 'quick'


def constraints(query: str, *, today: date | None = None) -> WebConstraints:
    text = normalized(query)
    topic = _topic(text, query)
    official = bool(re.search(r'\b(?:oficial|oficiais|official)\b', text))
    domains: tuple[str, ...] = ()
    if official:
        if re.search(r'\bpython\b', text):
            domains = ('python.org', 'docs.python.org')
        elif topic == 'react':
            domains = ('react.dev',)
        elif re.search(r'\bwindows\b', text):
            domains = ('learn.microsoft.com', 'support.microsoft.com', 'blogs.windows.com')
        else:
            domains = _project_domains(text)
    news = bool(re.search(r'\b(?:noticias|news)\b', text)
                or (re.search(r'\baconteceu\b', text)
                    and re.search(r'\b(?:hoje|ontem|agora|mundo|tecnologia)\b', text)))
    freshness = ('day' if re.search(r'\b(?:hoje|today)\b', text) and news else
                 'week' if news and re.search(r'\b(?:recentes?|ultimas?|atuais)\b', text) else '')
    if not freshness and topic == 'react' and re.search(r'\b(?:novidades|atualizacoes)\b', text):
        freshness = 'month'
    end = today or datetime.now().date()
    start: date | None = None
    window = re.search(r'\b(?:ultimos?|last)\s+(\d{1,3}|sete|seis|seven|six)\s+'
                       r'(dias?|days?|mes(?:es)?|months?)\b', text)
    if window:
        words = {'sete': 7, 'seis': 6, 'seven': 7, 'six': 6}
        count = words[window[1]] if window[1] in words else int(window[1])
        if 0 < count <= 366:
            if window[2].startswith(('mes', 'month')):
                month_index = end.year * 12 + end.month - 1 - count
                year, month = divmod(month_index, 12)
                start = date(year, month + 1, min(end.day, monthrange(year, month + 1)[1]))
            else:
                start = end - timedelta(days=count)
            days = (end - start).days
            freshness = 'week' if days <= 7 else 'month' if days <= 31 else 'year'
    if freshness and start is None:
        start = end - timedelta(days={'day': 0, 'week': 7, 'month': 31, 'year': 366}[freshness])
    quote = bool(re.search(r'\b(?:bitcoin|btc|cotacao|dolar|euro)\b', text)
                 and re.search(r'\b(?:quanto|preco|valor|vale|agora|atual|tempo real|cotacao)\b', text))
    quote = quote or currency_quote_question(text)
    version = re.search(r'\b(?:versao|version)\b', text)
    current = bool(re.search(r'\b(?:novidades|atualizacoes|releases?)\b', text)
                   or (version and re.search(r'\b(?:recente|latest|ultima|atual)\b', text)))
    read_required = (official or current or bool(freshness) or quote
                     or requires_page_evidence(query))
    depth = research_depth(query)
    if 'myanimelist' in text and re.search(r'avaliad|ranking|nota|posicao', text):
        domains = ('myanimelist.net',)
        read_required = True
    return WebConstraints(topic, official, domains, freshness, quote, read_required,
                          start, end if freshness else None,
                          'deep' if depth == 'deep' else 'verified' if read_required else 'quick')


def _project_domains(text: str) -> tuple[str, ...]:
    # Only projects in the reproduced failures; unknown authority remains unverified.
    projects = {'typescript': ('typescriptlang.org', 'devblogs.microsoft.com'),
                'fastapi': ('fastapi.tiangolo.com',), 'bun': ('bun.sh', 'bun.com'),
                'django': ('djangoproject.com', 'docs.djangoproject.com'),
                'firefox': ('mozilla.org', 'support.mozilla.org', 'developer.mozilla.org')}
    return next((domains for name, domains in projects.items()
                 if re.search(r'\b' + name + r'\b', text)), ())


def rank_sources(query: str, sources: list[dict[str, Any]],
                 policy: WebConstraints) -> list[dict[str, Any]]:
    """Stable, local ranking before HTTP; metadata is a hint, not proof of truth."""
    terms = _terms(query)
    attribute = requested_attribute(query)
    today = datetime.now().date()
    preferred = policy.domains or _project_domains(normalized(query))
    release = bool(re.search(r'\b(?:release|lancamento|versao)\b', normalized(query)))
    source_required = requires_page_evidence(query)
    academic = ('iucr.org', 'academic.oup.com', 'onlinelibrary.wiley.com',
                'sciencedirect.com', 'news.uzh.ch')

    def score(source: dict[str, Any]) -> int:
        parsed = urlsplit(source['url'])
        host = (parsed.hostname or '').casefold()
        text = normalized(str(source.get('title', '')) + ' ' + str(source.get('snippet', '')))
        authority = any(host == domain or host.endswith('.' + domain) for domain in preferred)
        article = bool(parsed.path.strip('/'))
        scholarly = (host.endswith(('.gov', '.edu')) or '.ac.' in host
                     or any(host == domain or host.endswith('.' + domain) for domain in academic))
        attribute_bonus = 0
        if attribute:
            attribute_bonus += 8 * supports_requested_attribute(query, text, today=today)
            published = source.get('published_at')
            if published:
                try:
                    age = (today - date.fromisoformat(str(published)[:10])).days
                except ValueError:
                    age = None
                if age is not None:
                    attribute_bonus += (
                        6 if 0 <= age <= CURRENT_EVIDENCE_MAX_AGE_DAYS else -6)
        return (len(terms & _terms(text)) + 8 * authority + 2 * article
                + 5 * bool(release and re.search(r'releases?|blog/|notes', parsed.path))
                + 6 * bool(policy.quote and has_quote_value(str(source.get('snippet') or '')))
                + attribute_bonus
                + 16 * bool(source_required and scholarly)
                - 8 * bool(policy.freshness and source.get('published_at') and not policy.dated(source)))

    candidates = [source for source in sources if policy.accepts(source)
                  and (urlsplit(source['url']).hostname or '').casefold().removeprefix('www.')
                  not in ({'youtube.com', 'youtu.be', 'tiktok.com', 'facebook.com',
                           'instagram.com', 'reddit.com'} if source_required
                          else {'youtube.com', 'youtu.be', 'tiktok.com'})
                  and not (policy.domains == ('myanimelist.net',)
                           and urlsplit(source['url']).path.startswith('/stacks'))]
    return sorted(candidates, key=score, reverse=True)


def useful_content(content: str) -> bool:
    """A successful fetch of a blocked shell or footer is not a read article."""
    text = normalized(content.strip())
    if len(text) < 100 or len(text.split()) < 12:
        return False
    if _FINANCIAL_FOOTER.match(text.lstrip('# \n*')):
        return False
    if len(text) < 2500 and re.match(
            r"(?:a )?required part.*(?:couldn.t|could not) load|enable (?:javascript|cookies).*continue|"
            r'access denied|verify (?:that )?you are human|checking your browser|'
            r'about press copyright contact us creators advertise|'
            r'just a moment.*cloudflare', text.lstrip('# \n'), re.DOTALL):
        return False
    return True


def has_quote_value(content: str) -> bool:
    """A value with a unit makes a quote candidate, never proof of a live price."""
    currency = r'(?:us\$|r\$|\$|€|usd\b|eur\b|brl\b|dolares?\b|euros?\b|reais\b)'
    number = r'\d[\d.,]*(?!\w)'
    return bool(re.search(currency + r'\s*' + number + r'|(?<!\w)' + number + r'\s*' + currency,
                          normalized(content)))


def _terms(text: str) -> set[str]:
    stop = {'com', 'uma', 'para', 'que', 'sobre', 'the', 'and', 'this', 'with',
            'pesquisa', 'explica', 'documentacao', 'oficial', 'official', 'quais', 'qual'}
    return {term for term in re.findall(r'[a-z0-9_]{3,}', normalized(text)) if term not in stop}


def select_evidence(query: str, documents: list[dict[str, Any]], budget: int
                    ) -> list[dict[str, Any]]:
    """Select relevant paragraphs in original order, with a fair per-source budget."""
    if not documents or budget <= 0:
        return []
    terms = _terms(query)
    source_required = requires_page_evidence(query)
    if source_required:
        # PT requests often read EN papers; keep requested sections, not navigation.
        for pattern, aliases in (
            (r'metodologia|metodo|experiencia', {'methods', 'procedure', 'experiment', 'volunteers'}),
            (r'resultados|numeros|exatos', {'results', 'measured', 'data', 'energy', 'density'}),
            (r'limitacoes', {'limitations', 'discussion', 'temperature', 'moisture'}),
            (r'leite|nutritiv', {'milk', 'protein', 'nutrient', 'energy', 'crystal'}),
            (r'angulo|salpicos|fisica', {'angle', 'critical', 'splash', 'degrees', 'weber'}),
        ):
            if re.search(pattern, normalized(query)):
                terms |= aliases
    attribute = requested_attribute(query)
    quantitative = bool(source_required and re.search(
        r'\b(?:numeros|valores|dados|exatos|compar\w*|angulo|quantitative|exact|compare)\b',
        normalized(query)))
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    remaining = budget
    quote = constraints(query).quote
    for index, document in enumerate(documents):
        quota = remaining // (len(documents) - index)
        blocks = re.findall(r'```[^\n]*\n[\s\S]*?```|[^\n]+(?:\n(?!\n|```)[^\n]+)*',
                            str(document.get('content') or ''))
        title = str(document.get('title') or '')

        def block_score(position: int, title: str = title) -> int:
            block = blocks[position]
            support = bool(attribute and supports_requested_attribute(
                query, title + '\n' + block, today=datetime.now().date()))
            section = bool(source_required and re.match(
                r'\s*#*\s*(?:methods?|results?|limitations?|discussion|abstract)\b',
                block, re.IGNORECASE))
            numeric_comparison = bool(quantitative and re.search(r'\d', block)
                and re.search(r'\b(?:J|kJ|cal|kcal|joules?|grams?|graus|degrees)\b|[%°]', block)
                and re.search(r'\b(?:equivalent|same)\s+(?:mass|masses|volume|weight)|'
                              r'\b(?:per|por)\s+(?:\d+\s*)?(?:g\b|grams?\b|kg\b|'
                              r'massa\b|mass\b|litres?\b|litros?\b)',
                              block, re.IGNORECASE))
            return (len(terms & _terms(block)) + (1000 if support else 0)
                    + 5 * section + 12 * numeric_comparison)

        ranked = sorted(range(len(blocks)), key=block_score, reverse=True)
        chosen: dict[int, str] = {}
        used = 0
        for position in ranked:
            block = blocks[position].strip()
            if quote and _FINANCIAL_FOOTER.match(normalized(block).lstrip('# \n*')):
                continue
            key = ' '.join(block.casefold().split())
            if key in seen or not block or used >= quota:
                continue
            available = quota - used - (2 if chosen else 0)
            if available < 1:
                break
            if len(block) > available:
                # Keep a sentence boundary when possible; do not create broken code fences.
                if block.startswith('```'):
                    continue
                prefix = block[:available]
                split = max(prefix.rfind('. '), prefix.rfind('\n'))
                block = prefix[:split + 1] if split >= available // 2 else prefix
            chosen[position] = block
            used += len(block) + (2 if len(chosen) > 1 else 0)
            seen.add(key)
        content = '\n\n'.join(chosen[pos] for pos in sorted(chosen))
        remaining -= len(content)
        selected.append({**document, 'content': content,
                         'truncated': bool(document.get('truncated') or
                                           len(content) < len(str(document.get('content') or '')))})
    return selected


def search_query(query: str, policy: WebConstraints) -> str:
    return query + ' JavaScript biblioteca' if policy.topic == 'react' and not re.search(
        r'\bjavascript\b', query, re.IGNORECASE) else query


def publication_hint(item: dict[str, Any], retrieved_at: str) -> str | None:
    """Rounded ages yield a date only if the whole possible interval is that day."""
    if item.get('date_source') != 'structured':
        return None
    value = str(item.get('published_age') or '')
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        pass
    match = re.fullmatch(r'(\d{1,3}) (minutes?|hours?) ago', value)
    if not match:
        return None
    try:
        retrieved = datetime.fromisoformat(retrieved_at.replace('Z', '+00:00'))
    except ValueError:
        return None
    unit = timedelta(minutes=1) if match[2].startswith('minute') else timedelta(hours=1)
    newest = retrieved - int(match[1]) * unit
    oldest = newest - unit
    return newest.date().isoformat() if newest.date() == oldest.date() else None
