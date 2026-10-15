"""Declarative catalog of Windows Settings pages (FASE 4 fast path).

Reaching a Settings page used to cost an inference round, a UIA dump of the whole
Settings window and the seconds an AppAgent spends clicking through the UI: the
navigation URI is public and the set of pages is finite, so it belongs in a table
instead of in a decision.

This module is vendor-free and side-effect free: it maps what a user says
("Sistema > Som", "som", "Configurações > Sistema > Som") to the URI that lands
directly on that page, plus the literal texts that prove the page really opened.
The tool layer launches and verifies; this file only knows which page is which.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Iterable

SCHEME = "ms-settings:"

# Words that group pages in a request ("Sistema > Som", "abrir Configurações de Som")
# name a section or an intention, not a page, so they are never aliases of any page.
SECTION_WORDS = frozenset({
    "a", "as", "aba", "abrir", "abre", "configuracao", "configuracoes", "de", "do", "da",
    "em", "e", "entrar", "entre", "ir", "la", "na", "no", "o", "opcao", "opcoes", "pagina",
    "para", "settings", "sistema", "system", "windows",
})


@dataclass(frozen=True)
class SettingsPage:
    """One Settings page: how it is called, where it lives, and how to prove it."""

    name: str
    uri: str
    aliases: tuple[str, ...] = ()
    verify: tuple[str, ...] = ()

    def describe(self) -> str:
        return f"{self.name} ({self.uri})"


class SettingsPageError(ValueError):
    """The requested page is unknown, ambiguous or not a Settings page at all."""


PAGES: tuple[SettingsPage, ...] = (
    SettingsPage("Som", "ms-settings:sound",
                 ("som", "sound", "audio", "audio de saida", "alto falantes", "alto-falantes",
                  "volume", "saida de audio", "dispositivos de saida"),
                 ("Som", "Sound")),
    SettingsPage("Tela", "ms-settings:display",
                 ("tela", "display", "video", "exibicao", "monitor", "resolucao", "escala"),
                 ("Tela", "Display")),
    SettingsPage("Notificações", "ms-settings:notifications",
                 ("notificacoes", "notifications", "alertas", "notificacao"),
                 ("Notificações", "Notifications")),
    SettingsPage("Bluetooth", "ms-settings:bluetooth",
                 ("bluetooth",),
                 ("Bluetooth",)),
    SettingsPage("Dispositivos", "ms-settings:connecteddevices",
                 ("dispositivos", "devices", "bluetooth e dispositivos", "impressora e dispositivos"),
                 ("Dispositivos", "Devices")),
    SettingsPage("Wi-Fi", "ms-settings:network-wifi",
                 ("wi-fi", "wifi", "rede sem fio", "wireless", "redes sem fio"),
                 ("Wi-Fi", "Sem fio")),
    SettingsPage("Rede e Internet", "ms-settings:network",
                 ("rede", "internet", "rede e internet", "network", "conexoes", "status da rede"),
                 ("Rede e Internet", "Network & internet", "Rede")),
    SettingsPage("Aplicativos instalados", "ms-settings:appsfeatures",
                 ("aplicativos", "apps", "apps e recursos", "aplicativos instalados",
                  "programas instalados", "desinstalar", "aplicativos e recursos"),
                 ("Aplicativos", "Apps", "Instalados")),
    SettingsPage("Aplicativos padrão", "ms-settings:defaultapps",
                 ("aplicativos padrao", "app padrao", "default apps", "padrao de aplicativos"),
                 ("Aplicativos padrão", "Default apps", "Padrão")),
    SettingsPage("Armazenamento", "ms-settings:storagesense",
                 ("armazenamento", "storage", "espaco em disco", "sensor de armazenamento", "storage sense"),
                 ("Armazenamento", "Storage")),
    SettingsPage("Energia e bateria", "ms-settings:powersleep",
                 ("energia", "bateria", "power", "sleep", "suspender", "economia de bateria"),
                 ("Energia", "Bateria", "Power", "Suspender")),
    SettingsPage("Data e hora", "ms-settings:dateandtime",
                 ("data e hora", "hora", "relogio", "data", "fuso horario", "date and time"),
                 ("Data e hora", "Date & time", "Fuso horário")),
    SettingsPage("Idioma e região", "ms-settings:language",
                 ("idioma", "regiao", "language", "idioma e regiao", "pais ou regiao"),
                 ("Idioma", "Language", "Região")),
    SettingsPage("Personalização", "ms-settings:personalization",
                 ("personalizacao", "plano de fundo", "papel de parede", "tema", "cores", "wallpaper"),
                 ("Personalização", "Personalization", "Plano de fundo")),
    SettingsPage("Barra de tarefas", "ms-settings:taskbar",
                 ("barra de tarefas", "taskbar", "barra de tarefa"),
                 ("Barra de tarefas", "Taskbar")),
    SettingsPage("Multitarefa", "ms-settings:multitasking",
                 ("multitarefa", "multitasking", "areas de trabalho virtuais"),
                 ("Multitarefa", "Multitasking")),
    SettingsPage("Privacidade e segurança", "ms-settings:privacy",
                 ("privacidade", "privacidade e seguranca", "privacy", "permissoes"),
                 ("Privacidade", "Privacy")),
    SettingsPage("Windows Update", "ms-settings:windowsupdate",
                 ("windows update", "atualizacoes", "atualizacao", "update", "atualizacoes do windows"),
                 ("Windows Update", "Atualizações")),
    SettingsPage("Sobre", "ms-settings:about",
                 ("sobre", "about", "informacoes do sistema", "especificacoes", "sobre este pc"),
                 ("Sobre", "About", "Especificações")),
    SettingsPage("Acessibilidade", "ms-settings:easeofaccess",
                 ("acessibilidade", "accessibility", "facilidade de acesso"),
                 ("Acessibilidade", "Accessibility")),
    SettingsPage("Jogos", "ms-settings:gaming-gamebar",
                 ("jogos", "gaming", "game bar", "barra de jogos", "xbox game bar"),
                 ("Jogos", "Gaming")),
    SettingsPage("Ativação", "ms-settings:activation",
                 ("ativacao", "activation", "ativar windows"),
                 ("Ativação", "Activation")),
    SettingsPage("Recuperação", "ms-settings:recovery",
                 ("recuperacao", "recovery", "redefinir este pc", "redefinir o pc"),
                 ("Recuperação", "Recovery")),
    SettingsPage("Impressoras", "ms-settings:printers",
                 ("impressoras", "impressora", "scanner", "printers", "digitalizar"),
                 ("Impressoras", "Printers", "Scanners")),
    SettingsPage("Mouse", "ms-settings:mousetouchpad",
                 ("mouse", "touchpad", "ponteiro", "cursor"),
                 ("Mouse", "Touchpad")),
    SettingsPage("Digitação", "ms-settings:keyboard",
                 ("digitacao", "teclado", "typing", "keyboard", "autocorrecao"),
                 ("Digitação", "Typing", "Teclado")),
    SettingsPage("Formato regional", "ms-settings:regionformatting",
                 ("formato regional", "regiao e idioma", "region formatting", "formato de data e hora"),
                 ("Região", "Region", "Formato")),
)

_BY_URI = {page.uri.casefold(): page for page in PAGES}


def fold(text: str) -> str:
    """Case- and accent-insensitive form, with punctuation turned into spaces."""
    decomposed = unicodedata.normalize("NFKD", str(text))
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    replaced = "".join(char if char.isalnum() or char in "-_" else " " for char in stripped)
    return " ".join(replaced.casefold().split())


def _contains(tokens: list[str], wanted: list[str]) -> bool:
    n = len(wanted)
    return any(tokens[index:index + n] == wanted for index in range(len(tokens) - n + 1))


def _match(page: str) -> SettingsPage | None:
    """Best page for a text, by longest alias that appears as a whole token run."""
    tokens = [token for token in fold(page).split() if token not in SECTION_WORDS]
    best_length = 0
    best: list[SettingsPage] = []
    for candidate in PAGES:
        for alias in (candidate.name, *candidate.aliases):
            wanted = [token for token in fold(alias).split() if token not in SECTION_WORDS]
            # An alias made only of section words would match everything.
            if not wanted or not _contains(tokens, wanted):
                continue
            if len(wanted) > best_length:
                best_length, best = len(wanted), [candidate]
            elif len(wanted) == best_length and candidate not in best:
                best.append(candidate)
    if len(best) == 1:
        return best[0]
    if len(best) > 1:
        options = ", ".join(page.describe() for page in best)
        raise SettingsPageError(
            f"Página de Configurações ambígua: '{page}'. Escolha uma: {options}.")
    return None


def _by_uri(page: str) -> SettingsPage:
    uri = page.strip()
    if any(char in uri for char in (" ", "&", "|", "?", "\n", "\t")):
        raise SettingsPageError(f"URI de Configurações inválida: '{uri}'.")
    known = _BY_URI.get(uri.casefold())
    if known is not None:
        return known
    # A URI the catalog does not carry is still a page the agent asked for; it is
    # accepted honestly, but only the window itself can be verified for it.
    return SettingsPage(uri, uri)


def resolve_page(page: str) -> SettingsPage:
    """Map a page name (any spelling of it) to the page, or explain why not.

    Unknown and ambiguous inputs raise :class:`SettingsPageError` with the available
    names, because the caller has to be able to fall back to GUI navigation from the
    message alone instead of guessing again.
    """
    text = str(page or "").strip()
    if not text:
        raise SettingsPageError("Nenhuma página de Configurações foi informada.")
    if text.casefold().startswith(SCHEME):
        return _by_uri(text)
    found = _match(text)
    if found is not None:
        return found
    available = ", ".join(sorted(entry.name for entry in PAGES))
    raise SettingsPageError(
        f"Página de Configurações desconhecida: '{text}'. Disponíveis: {available}. "
        f"Para navegar manualmente use launch_application com uma URI {SCHEME}.")


def page_names() -> Iterable[str]:
    return (entry.name for entry in PAGES)
