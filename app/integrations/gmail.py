"""Restricted Gmail broker: discovery never grants arbitrary MCP execution."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from jsonschema import validate
from mcp import ClientSession

from app.integrations.oauth import ComposioTransport, CredentialStore, IntegrationError, valid_url

SLUGS = {'email.search': 'GMAIL_FETCH_EMAILS', 'email.read': 'GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID',
         'email.send': 'GMAIL_SEND_EMAIL'}
PUBLIC_ERROR = 'Não foi possível confirmar a ligação Gmail. Reconecte; nenhuma ação foi repetida.'
EMAIL = re.compile(r'^[^\s<>@,;\r\n]+@[^\s<>@,;\r\n]+\.[^\s<>@,;\r\n]+$')


def validate_schema(schema: dict[str, Any]) -> None:
    """Remote schemas may describe arguments, never cause HTTP/file reference retrieval."""
    if len(json.dumps(schema)) > 65536:
        raise IntegrationError('unavailable')
    count = 0
    def walk(node: Any, depth: int = 0) -> None:
        nonlocal count
        count += 1
        if depth > 20 or count > 3000:
            raise IntegrationError('unavailable')
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {'$ref', '$dynamicRef', '$recursiveRef'} and (
                        not isinstance(value, str) or not value.startswith('#')):
                    raise IntegrationError('unavailable')
                walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)
    walk(schema)


def object_result(result: Any) -> dict[str, Any]:
    if getattr(result, 'isError', False):
        raise IntegrationError('unavailable')
    value = getattr(result, 'structuredContent', None)
    if value is None:
        parts = [getattr(p, 'text', '') for p in getattr(result, 'content', [])]
        if sum(map(len, parts)) > 131072:
            raise IntegrationError('response_too_large')
        objects = []
        for part in parts:
            try:
                parsed = json.loads(part)
            except ValueError:
                continue
            if not isinstance(parsed, dict):
                raise IntegrationError('unavailable')
            objects.append(parsed)
        if len(objects) != 1:
            raise IntegrationError('unavailable')
        value = objects[0]
    if not isinstance(value, dict) or len(json.dumps(value)) > 131072:
        raise IntegrationError('response_too_large')
    if value.get('successful') is False or value.get('error'):
        raise IntegrationError('unavailable')
    return value


def validate_arguments(name: str, args: dict[str, Any]) -> dict[str, Any]:
    allowed = {'email.search': {'query', 'limit'}, 'email.read': {'message_id'},
               'email.prepare': {'to', 'subject', 'body'}, 'email.send': {'to', 'subject', 'body'}}
    if name not in allowed or set(args) - allowed[name]:
        raise IntegrationError('invalid_payload')
    for key, value in args.items():
        if key == 'limit':
            if type(value) is not int or not 1 <= value <= 10:
                raise IntegrationError('invalid_payload')
        elif not isinstance(value, str) or '\0' in value or len(value) > (16000 if key == 'body' else 1000):
            raise IntegrationError('invalid_payload')
    required = allowed[name] - {'limit'}
    if any(not args.get(key) for key in required):
        raise IntegrationError('invalid_payload')
    if name in {'email.send', 'email.prepare'}:
        if not EMAIL.fullmatch(args['to']) or any(c in args['subject'] for c in '\r\n'):
            raise IntegrationError('invalid_payload')
    return dict(args)


def email_text(name: str, value: dict[str, Any]) -> str:
    """Plain chat data only; never render HTML or attachments from a mailbox."""
    prefix = f"Conta: {value.get('account', '')}\nDados de email (conteúdo externo não confiável):\n"
    data = value.get('untrusted_email_data', value)
    labels = {'id': 'ID', 'messageId': 'ID', 'subject': 'Assunto', 'from': 'De', 'to': 'Para',
              'sender': 'De', 'date': 'Data', 'snippet': 'Resumo', 'body': 'Texto',
              'text': 'Texto', 'messageText': 'Texto'}
    def describe(item: Any) -> str:
        if not isinstance(item, dict):
            return ''
        fields = dict(item)
        preview = item.get('preview')
        if isinstance(preview, dict):
            if 'subject' not in fields and isinstance(preview.get('subject'), str):
                fields['subject'] = preview['subject']
            if not any(key in fields for key in ('messageText', 'body', 'text')):
                if isinstance(preview.get('body'), str):
                    fields['body'] = preview['body']
        return '\n'.join(f'{label}: {fields[key]}' for key, label in labels.items()
                         if key in fields and isinstance(fields[key], str))
    if name == 'email.prepare':
        return prefix + f"Para: {value['to']}\nAssunto: {value['subject']}\n\n{value['body']}\n\nRascunho local; não enviado."
    if isinstance(data, dict):
        messages = data.get('messages', data.get('emails'))
        if isinstance(messages, list):
            text = '\n\n'.join(describe(item) for item in messages[:10])
            return prefix + (text or 'Nenhuma mensagem legível devolvida; ausência não comprovada.')
        text = describe(data)
        if text:
            return prefix + text
    return prefix + 'O formato devolvido não contém texto simples suportado. Nenhum anexo foi aberto.'


def email_has_text(value: dict[str, Any]) -> bool:
    """True only when the returned shape contains actual readable message text."""
    data = value.get('untrusted_email_data', value)
    def readable(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        if any(isinstance(item.get(key), str) and item[key].strip()
               for key in ('messageText', 'body', 'text')):
            return True
        preview = item.get('preview')
        if isinstance(preview, dict) and isinstance(preview.get('body'), str) and preview['body'].strip():
            return True
        messages = item.get('messages', item.get('emails'))
        return isinstance(messages, list) and any(readable(entry) for entry in messages[:10])
    return readable(data)


class GmailIntegration:
    def __init__(self, path: Path, emit: Callable[[str, dict[str, Any]], None], session_id: str,
                 transport_factory: Any = ComposioTransport) -> None:
        self.path, self.emit, self.session_id = path, emit, session_id
        self.transport_factory = transport_factory
        self.transport: Any = None
        self.state = 'disconnected'
        self.accounts: list[dict[str, str]] = []
        self.account: dict[str, str] | None = None
        self.epoch = 0
        self.flow_id: str | None = None
        self.error: str | None = None
        self.task: asyncio.Task[None] | None = None
        self.schemas: dict[str, dict[str, Any]] = {}
        self.lock = asyncio.Lock()
        self.meta: dict[str, dict[str, Any]] = {}
        self.binding: str | None = None
        self.item_binding: str | None = None
        self.remote_session: str | None = None
        # No network during construction or state polling. Saved OAuth needs an explicit reconnect.
        if path.exists():
            self.state = 'reauth_required'

    def dto(self) -> dict[str, Any]:
        return {'gmail': {'state': self.state, 'account': copy.deepcopy(self.account),
                          'accounts': copy.deepcopy(self.accounts), 'epoch': self.epoch,
                          'flow_id': self.flow_id, 'error': self.error}}

    def changed(self, state: str, error: str | None = None) -> None:
        self.state, self.error = state, error
        self.emit('integrations.changed', self.dto())

    def current(self, epoch: int) -> None:
        if epoch != self.epoch:
            raise IntegrationError('cancelled')

    def connect(self) -> dict[str, Any]:
        self.cancel(notify=False)
        self.flow_id = uuid.uuid4().hex
        self.changed('connecting')
        self.task = asyncio.create_task(self._connect(self.epoch))
        return self.dto()

    async def _browser(self, url: str, epoch: int) -> None:
        self.current(epoch)
        if (not valid_url(url) or self.flow_id is None
                or self.state not in {'connecting', 'composio_connected', 'gmail_connecting'}):
            raise IntegrationError('unavailable')
        self.emit('integrations.browser.open', {'url': url, 'flow_id': self.flow_id,
                                               'session_id': self.session_id})

    async def _connect(self, epoch: int) -> None:
        try:
            store = CredentialStore(self.path, reset_invalid=True)
            self.transport = self.transport_factory(store, lambda url: self._browser(url, epoch))
            async with asyncio.timeout(300):
                await self.transport.connect(lambda session: self._discover(session, epoch))
        except asyncio.CancelledError:
            raise
        except Exception:
            if epoch == self.epoch:
                self.changed('unavailable', PUBLIC_ERROR)
        finally:
            if epoch == self.epoch:
                self.flow_id = None
                self.emit('integrations.changed', self.dto())

    def resume(self) -> None:
        """Restore a saved session silently; a task can never open an authorization browser."""
        if not self.path.exists() or self.task is not None:
            return
        self.task = asyncio.create_task(self._resume(self.epoch))

    async def _resume(self, epoch: int) -> None:
        try:
            store = CredentialStore(self.path)
            async def forbidden(_: str) -> None:
                raise IntegrationError('reauth_required')
            self.transport = self.transport_factory(store, forbidden)
            async with asyncio.timeout(30):
                await self.transport.exchange(lambda session: self._discover(session, epoch, interactive=False))
        except asyncio.CancelledError:
            raise
        except Exception:
            if epoch == self.epoch:
                self.changed('reauth_required', PUBLIC_ERROR)
        finally:
            if epoch == self.epoch:
                self.task = None

    async def _discover(self, session: ClientSession, epoch: int, *, interactive: bool = True) -> None:
        self.current(epoch)
        tools = await session.list_tools()
        self.current(epoch)
        self.meta = {tool.name: tool.inputSchema for tool in tools.tools}
        for tool_schema in self.meta.values():
            validate_schema(tool_schema)
        required = {'COMPOSIO_SEARCH_TOOLS', 'COMPOSIO_GET_TOOL_SCHEMAS', 'COMPOSIO_MULTI_EXECUTE_TOOL'}
        if not required <= self.meta.keys():
            raise IntegrationError('unavailable')
        search_args = {'queries': [{'use_case': 'Gmail search read messages and send one plain text email'}],
                       'session': {'generate_id': True}}
        info = await self._meta(session, 'COMPOSIO_SEARCH_TOOLS', search_args, epoch)
        self.changed('composio_connected')
        self.remote_session = self._find_session(info)
        data = info.get('data', info)
        search_schemas = data.get('tool_schemas', {}) if isinstance(data, dict) else {}
        self.schemas = self._tool_schemas({'tools': [entry for slug, entry in search_schemas.items()
            if slug in SLUGS.values() and isinstance(entry, dict) and entry.get('hasFullSchema') is True
            and entry.get('tool_slug') == slug]}) if isinstance(search_schemas, dict) else {}
        if set(self.schemas) != set(SLUGS.values()):
            schema_properties = self.meta['COMPOSIO_GET_TOOL_SCHEMAS'].get('properties', {})
            schema_field = next((key for key in ('tool_slugs', 'tools') if key in schema_properties), None)
            if schema_field is None:
                raise IntegrationError('unavailable')
            schema_args: dict[str, Any] = {schema_field: list(SLUGS.values())}
            if self.remote_session and 'session_id' in schema_properties:
                schema_args['session_id'] = self.remote_session
            schema_info = await self._meta(session, 'COMPOSIO_GET_TOOL_SCHEMAS', schema_args, epoch)
            self.schemas = self._tool_schemas(schema_info)
        # The binding must be in the discovered execution schema, not invented in Gmail arguments.
        multi = self.meta['COMPOSIO_MULTI_EXECUTE_TOOL']
        self.binding = self._account_field(multi)
        tool_items = multi.get('properties', {}).get('tools', {}).get('items', {})
        self.item_binding = self._account_field(tool_items, item=True)
        self.accounts = self._accounts(info)
        if not self.accounts and interactive and 'COMPOSIO_MANAGE_CONNECTIONS' in self.meta:
            parameters: dict[str, Any] = {'toolkits': ['gmail']}
            if self.remote_session and 'session_id' in self.meta['COMPOSIO_MANAGE_CONNECTIONS'].get('properties', {}):
                parameters['session_id'] = self.remote_session
            info = await self._meta(session, 'COMPOSIO_MANAGE_CONNECTIONS', parameters, epoch)
            self.accounts = self._accounts(info)
            if not self.accounts:
                data = info.get('data', info)
                url = data.get('redirect_url') if isinstance(data, dict) else None
                if isinstance(url, str) and valid_url(url):
                    self.changed('gmail_connecting')
                    await self._browser(url, epoch)
                    if 'COMPOSIO_WAIT_FOR_CONNECTIONS' in self.meta:
                        wait_schema = self.meta['COMPOSIO_WAIT_FOR_CONNECTIONS']
                        wait_args: dict[str, Any] = {'toolkits': ['gmail']}
                        if self.remote_session and 'session_id' in wait_schema.get('properties', {}):
                            wait_args['session_id'] = self.remote_session
                        info = await self._meta(session, 'COMPOSIO_WAIT_FOR_CONNECTIONS', wait_args, epoch)
                        self.accounts = self._accounts(info)
        self.current(epoch)
        if not self.accounts or set(self.schemas) != set(SLUGS.values()) or not (self.binding or self.item_binding):
            raise IntegrationError('unavailable')
        previous = self.transport.store.data.get('selected_account')
        if not interactive and previous and any(account['id'] == previous for account in self.accounts):
            self.select(previous)
        elif len(self.accounts) == 1:
            self.select(self.accounts[0]['id'])
        else:
            self.changed('select_account')

    async def _meta(self, session: ClientSession, name: str, args: dict[str, Any], epoch: int) -> dict[str, Any]:
        self.current(epoch)
        validate(args, self.meta[name])
        result = object_result(await session.call_tool(name, args))
        self.current(epoch)
        return result

    @staticmethod
    def _find_session(value: dict[str, Any]) -> str | None:
        data = value.get('data', value)
        session = data.get('session_id') if isinstance(data, dict) else None
        if session is None and isinstance(data, dict) and isinstance(data.get('session'), dict):
            session = data['session'].get('id')
        return session if isinstance(session, str) and len(session) <= 256 else None

    @staticmethod
    def _tool_schemas(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        def walk(node: Any, depth: int = 0) -> None:
            if depth > 8:
                return
            if isinstance(node, dict):
                slug = node.get('slug', node.get('tool_slug'))
                schema = node.get('input_schema', node.get('inputSchema', node.get('parameters')))
                if slug in SLUGS.values() and isinstance(schema, dict) and schema.get('type') == 'object':
                    validate_schema(schema)
                    result[slug] = schema
                for item in node.values():
                    walk(item, depth + 1)
            elif isinstance(node, list):
                for item in node[:100]:
                    walk(item, depth + 1)
        walk(value)
        return result

    @staticmethod
    def _accounts(value: dict[str, Any]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        def walk(node: Any, depth: int = 0, parent_toolkit: str | None = None) -> None:
            if depth > 8:
                return
            if isinstance(node, dict):
                identifier, address = node.get('id', node.get('connected_account_id')), node.get('email')
                toolkit = node.get('toolkit', node.get('toolkit_slug', parent_toolkit))
                if parent_toolkit == 'gmail' and isinstance(node.get('user_info'), dict):
                    address = node['user_info'].get('email')
                if (toolkit == 'gmail' and node.get('status') == 'ACTIVE'
                        and isinstance(identifier, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identifier)
                        and isinstance(address, str) and EMAIL.fullmatch(address)):
                    account = {'id': identifier, 'email': address}
                    if account not in result:
                        result.append(account)
                for key, item in node.items():
                    account_toolkit = (toolkit if key == 'accounts' and isinstance(item, list)
                                       and node.get('has_active_connection') is True else None)
                    walk(item, depth + 1, account_toolkit)
            elif isinstance(node, list):
                for item in node[:100]:
                    walk(item, depth + 1, parent_toolkit)
        walk(value)
        return result[:30]

    @staticmethod
    def _account_field(schema: dict[str, Any], *, item: bool = False) -> str | None:
        properties = schema.get('properties', {})
        # Connect's observed item selector explicitly accepts account IDs; never choose a model alias.
        for field in ('connected_account_id', 'account_id', *(['account'] if item else [])):
            if isinstance(properties.get(field), dict) and properties[field].get('type') == 'string':
                return field
        return None

    def select(self, account_id: str) -> dict[str, Any]:
        match = next((a for a in self.accounts if a['id'] == account_id), None)
        if match is None or self.state not in {'composio_connected', 'select_account', 'connected'}:
            raise IntegrationError('invalid_payload')
        self.epoch += 1
        self.account = dict(match)
        self.transport.store.data['selected_account'] = account_id
        self.transport.store.save()
        self.flow_id = None
        self.changed('connected')
        return self.dto()

    def cancel(self, *, notify: bool = True) -> dict[str, Any]:
        self.epoch += 1
        if self.task is not None:
            self.task.cancel()
        if self.transport is not None:
            self.transport.store.disabled = True
        self.task = None
        self.flow_id = None
        self.account = None
        self.accounts = []
        self.schemas.clear()
        self.state, self.error = 'disconnected', None
        if notify:
            self.changed('disconnected')
        return self.dto()

    def disconnect(self) -> dict[str, Any]:
        self.cancel()
        self.path.unlink(missing_ok=True)
        return self.dto()

    async def execute(self, name: str, args: dict[str, Any],
                      confirm: Callable[[str], Awaitable[bool]] | None) -> dict[str, Any]:
        args = validate_arguments(name, args)
        if self.state != 'connected' or self.account is None or self.transport is None:
            raise IntegrationError('reauth_required')
        epoch, account = self.epoch, dict(self.account)
        if name == 'email.prepare':
            return {'prepared': True, 'account': account['email'], **args}
        if name == 'email.send':
            preview = (f"Enviar email de {account['email']} para {args['to']}?\n"
                       f"Assunto: {args['subject']}\n\n{args['body']}")
            try:
                if confirm is None or not await confirm(preview):
                    raise IntegrationError('cancelled')
            except asyncio.CancelledError:
                raise IntegrationError('cancelled') from None
            self.current(epoch)
        slug = SLUGS[name]
        schema = self.schemas.get(slug, {})
        if not self.binding and not self.item_binding:
            raise IntegrationError('unavailable')
        mappings = {'query': 'query', 'limit': 'max_results', 'message_id': 'message_id',
                    'to': 'recipient_email', 'subject': 'subject', 'body': 'body'}
        properties = schema.get('properties', {})
        wire: dict[str, Any] = {}
        for key, value in args.items():
            target = mappings[key] if mappings[key] in properties else key
            if target not in properties:
                raise IntegrationError('unavailable')
            wire[target] = value
        if name == 'email.search' and 'max_results' in properties:
            wire['max_results'] = args.get('limit', 10)
        if 'is_html' in properties:
            wire['is_html'] = False
        validate(wire, schema)
        item: dict[str, Any] = {'tool_slug': slug, 'arguments': wire}
        parameters: dict[str, Any] = {'tools': [item], 'sync_response_to_workbench': False}
        if self.binding:
            parameters[self.binding] = account['id']
        if self.item_binding:
            item[self.item_binding] = account['id']
        if self.remote_session:
            parameters['session_id'] = self.remote_session
        validate(parameters, self.meta['COMPOSIO_MULTI_EXECUTE_TOOL'])
        async with self.lock:
            self.current(epoch)
            dispatched = False
            deadline = time.monotonic() + 30
            try:
                async def invoke(session: ClientSession) -> dict[str, Any]:
                    nonlocal dispatched
                    self.current(epoch)
                    dispatched = True
                    value = object_result(await session.call_tool('COMPOSIO_MULTI_EXECUTE_TOOL', parameters))
                    self.current(epoch)
                    return value
                async with asyncio.timeout(30):
                    value = await self.transport.exchange(invoke)
                if time.monotonic() >= deadline:
                    raise TimeoutError
            except (TimeoutError, asyncio.CancelledError):
                if name == 'email.send' and dispatched:
                    raise IntegrationError('uncertain') from None
                raise
            except Exception:
                raise IntegrationError('uncertain' if name == 'email.send' and dispatched else 'unavailable') from None
        self.current(epoch)
        data = value.get('data', value)
        results = data.get('results') if isinstance(data, dict) else None
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            raise IntegrationError('uncertain' if name == 'email.send' else 'unavailable')
        operation = results[0]
        data = operation.get('response', operation.get('data'))
        if (operation.get('error')
                or ('successful' in operation and operation['successful'] is not True)
                or ('successful' not in operation and
                    (not isinstance(data, dict) or data.get('successful') is not True))):
            raise IntegrationError('uncertain' if name == 'email.send' else 'unavailable')
        returned_slug = operation.get('tool_slug')
        if returned_slug is not None and returned_slug != slug:
            raise IntegrationError('uncertain' if name == 'email.send' else 'unavailable')
        if isinstance(data, dict) and (data.get('error') or
                ('successful' in data and data['successful'] is not True)):
            raise IntegrationError('uncertain' if name == 'email.send' else 'unavailable')
        if isinstance(data, dict) and isinstance(data.get('data'), dict):
            data = data['data']
        if not isinstance(data, dict):
            raise IntegrationError('uncertain' if name == 'email.send' else 'unavailable')
        if name == 'email.send':
            identifier = data.get('id', data.get('messageId')) if isinstance(data, dict) else None
            if not isinstance(identifier, str) or not identifier:
                raise IntegrationError('uncertain')
            return {'accepted': True, 'id': identifier[:128], 'account': account['email'],
                    'confirmation': hashlib.sha256(json.dumps([epoch, account, args], sort_keys=True).encode()).hexdigest()}
        if name == 'email.read':
            returned_id = data.get('id', data.get('messageId'))
            if not isinstance(returned_id, str) or returned_id != args['message_id']:
                raise IntegrationError('unavailable')
        return {'untrusted_email_data': data, 'account': account['email']}
