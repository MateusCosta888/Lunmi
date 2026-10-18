"""Gmail through the real chat/controller path, not just the broker.

The Brain is scripted and Gmail is a fake: no provider, network or email is used.
All addresses use example.test and all content is synthetic.
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from app.brain.contracts import Capabilities
from app.bridge import protocol
from app.bridge.service import BridgeService
from app.core.controller import TaskResult
from app.core import automatic
from app.core.controller import TaskController
from app.integrations.oauth import IntegrationError
from app.llm import Completion
from app.memory import MemoryManager
from app.settings import Settings


class ScriptedBrain:
    capabilities = Capabilities.of()

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]]) -> Completion:
        self.calls.append(messages)
        return Completion(json.dumps(self.replies.pop(0)), 10, 3)

    async def close(self) -> None:
        pass


class FakeGmail:
    """Connected account and the synthetic shapes observed from the real broker."""

    state = 'connected'
    epoch = 7
    account = {'id': 'qa-account', 'email': 'qa@example.test'}

    def __init__(self, *, send_error: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.previews: list[str] = []
        self.send_error = send_error

    async def execute(self, name: str, args: dict[str, Any], confirm: Any) -> dict[str, Any]:
        self.calls.append((name, dict(args)))
        if name == 'email.prepare':
            return {'prepared': True, 'account': self.account['email'], **args}
        if name == 'email.send':
            preview = (f"Enviar email de {self.account['email']} para {args['to']}?\n"
                       f"Assunto: {args['subject']}\n\n{args['body']}")
            self.previews.append(preview)
            if confirm is None or not await confirm(preview):
                raise IntegrationError('cancelled')
            if self.send_error:
                raise IntegrationError(self.send_error)
            return {'accepted': True, 'id': 'qa-sent-1', 'account': self.account['email']}
        if name == 'email.search':
            # The real search returned messages as a list. The sanitized probe is only
            # a shape sketch, not a response format to build a cause on.
            return {'account': self.account['email'], 'untrusted_email_data': {
                'messages': [{
                    'messageId': 'qa-message', 'subject': 'Synthetic subject',
                    'sender': 'qa@example.test', 'to': self.account['email'],
                    'messageText': 'Synthetic body text', 'preview': {'subject': 'Synthetic subject',
                                                                     'body': 'Synthetic body text'}}]}}
        if name == 'email.read':
            return {'account': self.account['email'], 'untrusted_email_data': {
                'messageId': args['message_id'], 'subject': 'Synthetic subject',
                'sender': 'qa@example.test', 'messageText': 'Synthetic body text'}}
        raise AssertionError(name)


def controller(tmp_path: Any, replies: list[dict[str, Any]], gmail: FakeGmail, *,
               confirmations: list[bool] | None = None) -> TaskController:
    settings = Settings(provider='custom', model='fixture', api_key='synthetic',
                        base_url='http://127.0.0.1:1/v1', prewarm=False, app_index=False,
                        app_search=False, collect_metrics=False,
                        logs_path=tmp_path / 'logs', output_path=tmp_path / 'output',
                        state_root=tmp_path / 'state')
    answers = iter(confirmations or [True])
    async def confirm(_: str) -> bool:
        return next(answers, False)
    instance = TaskController(settings, confirm=confirm,
                              memory=MemoryManager(tmp_path / 'memory.sqlite3'))
    instance.brain = ScriptedBrain(replies)
    instance.integrations = gmail
    return instance


def send_request() -> str:
    return 'Manda um email para qa@example.test, assunto: Teste, mensagem: Texto de ensaio.'


def prepare(subject: str = 'Teste', body: str = 'Texto de ensaio.') -> dict[str, Any]:
    return {'kind': 'tool', 'operation': {'name': 'email.prepare', 'args': {
        'to': 'qa@example.test', 'subject': subject, 'body': body}}, 'finish': True}


def test_email_send_authorization_is_narrow_and_affirmative_is_not_generic():
    assert automatic.allows_email_send(send_request()) is True
    assert automatic.allows_email_send('Envia um email para qa@example.test.') is True
    assert automatic.allows_email_send('confirmo') is False
    assert automatic.allows_email_send('Manda uma mensagem.') is False
    assert automatic.allows_email_send('Não envie email para qa@example.test.') is False
    # The generic desktop gate stays conservative; only the email scope grew.
    assert automatic.allows_actions(send_request()) is False


def test_prepare_then_confirm_sends_the_same_draft_without_a_second_inference(tmp_path):
    async def scenario() -> None:
        gmail = FakeGmail()
        c = controller(tmp_path, [prepare()], gmail)
        try:
            first = await c.run(send_request(), automatic=True)
            assert first.status == 'SUCCESS'
            assert 'Rascunho local' in first.message and 'não enviado' in first.message
            assert first.evidence[0]['verified'] is False
            assert [name for name, _ in gmail.calls] == ['email.prepare']
            assert first.operation_states[-1]['state'] == 'completed_unverified'

            second = await c.run('confirmo', automatic=True)
            assert second.status == 'SUCCESS'
            assert [name for name, _ in gmail.calls] == ['email.prepare', 'email.send']
            sent = gmail.calls[-1][1]
            assert (sent['to'], sent['subject'], sent['body']) == (
                'qa@example.test', 'Teste', 'Texto de ensaio.')
            assert gmail.previews and 'Assunto: Teste' in gmail.previews[-1]
            assert len(c.brain.calls) == 1, 'confirmation must not need another model turn'
            assert 'aceitou a mensagem para envio' in second.message
        finally:
            await c.close()
    asyncio.run(scenario())


def test_a_direct_send_request_is_authorized_for_email_only(tmp_path):
    async def scenario() -> None:
        gmail = FakeGmail()
        c = controller(tmp_path, [{'kind': 'tool', 'operation': {'name': 'email.send',
            'args': {'to': 'qa@example.test', 'subject': 'Teste', 'body': 'Texto de ensaio.'}},
            'finish': True}], gmail)
        try:
            result = await c.run(send_request(), automatic=True)
            assert result.status == 'SUCCESS', (result.status, result.message)
            assert [name for name, _ in gmail.calls] == ['email.send']
            authorization = [m['content'] for m in c.brain.calls[0]
                             if m['role'] == 'system' and 'Backend authorization' in m['content']]
            assert authorization and 'email.send' in authorization[0]
        finally:
            await c.close()
    asyncio.run(scenario())


class RefusalController:
    """Bridge-level refusal: streamed prefix, then no operation and no provider error."""

    def __init__(self, settings: Any, on_state: Any = None, confirm: Any = None,
                 choose: Any = None, indicate: Any = None, memory: Any = None) -> None:
        self.settings = settings
        self.on_state = on_state or (lambda _: None)
        self.on_public = None
        self.memory = memory
        self.engine = type('Engine', (), {'host_pids': set(), 'process': None,
                                          'worker_ready': False})()
        self.active = False

    def cancel(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def run(self, instruction: str, **kwargs: Any) -> TaskResult:
        if self.on_public:
            self.on_public('response.text.delta', {'text': 'Vou preparar o envio. '})
        return TaskResult(status='FAILED', failure_kind='AUTHORIZATION_REFUSED',
                          message='Nenhuma alteração foi executada: o pedido não autorizou esta ação.',
                          scope='conversation', llm_calls=1)


def test_bridge_refusal_keeps_streamed_prefix_and_emits_no_response_error(tmp_path):
    events: list[tuple[str, dict[str, Any]]] = []
    finished = threading.Event()

    def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))
        if kind == 'task.finished':
            finished.set()

    service = BridgeService(emit=emit, sandbox_dir=tmp_path, controller_factory=RefusalController)
    try:
        service.dispatch(protocol.Envelope(v=1, id='refusal', type='chat.send',
                                          payload={'text': 'Não envie esse email.', 'mode': 'execute'}))
        assert finished.wait(timeout=5.0)
        assert 'response.error' not in [kind for kind, _ in events]
        completed = [payload for kind, payload in events if kind == 'response.completed'][-1]
        assert completed['status'] == 'FAILED'
        assert completed['text'].startswith('Vou preparar o envio. ')
        assert 'Nenhuma alteração foi executada' in completed['text']
        final = [payload for kind, payload in events if kind == 'task.finished'][-1]
        assert final['status'] == 'FAILED'
        assert final['failure_kind'] == 'AUTHORIZATION_REFUSED'
        assert final['operation_states'] == []
        assistant = [payload for kind, payload in events
                     if kind == 'chat.message' and payload.get('role') == 'assistant'][-1]
        assert 'não autorizou' in assistant['text']
        assert 'Resposta interrompida' not in assistant['text']
    finally:
        service.close()


def test_a_new_recipient_never_releases_the_old_draft(tmp_path):
    async def scenario() -> None:
        gmail = FakeGmail()
        newer = {'kind': 'tool', 'operation': {'name': 'email.send', 'args': {
            'to': 'other@example.test', 'subject': 'Outro', 'body': 'Conteúdo novo.'}},
            'finish': True}
        c = controller(tmp_path, [prepare(), newer], gmail)
        try:
            await c.run(send_request(), automatic=True)
            result = await c.run('Agora manda um email para other@example.test, assunto: Outro, '
                                 'mensagem: Conteúdo novo.', automatic=True)
            assert result.status == 'SUCCESS', result.message
            assert [name for name, _ in gmail.calls] == ['email.prepare', 'email.send']
            assert gmail.calls[-1][1] == {'to': 'other@example.test', 'subject': 'Outro',
                                          'body': 'Conteúdo novo.'}
            assert 'Assunto: Outro' in gmail.previews[-1]
        finally:
            await c.close()
    asyncio.run(scenario())


def test_latest_inbox_shows_the_full_body_from_one_search_and_one_read(tmp_path):
    async def scenario() -> None:
        gmail = FakeGmail()
        c = controller(tmp_path, [{'kind': 'tool', 'operation': {'name': 'email.search',
            'args': {'query': 'in:inbox', 'limit': 1}}, 'finish': True}], gmail)
        try:
            result = await c.run('Lê a última mensagem da Inbox.', automatic=True)
            assert result.status == 'SUCCESS', (result.status, result.message)
            assert 'Synthetic body text' in result.message
            assert [name for name, _ in gmail.calls] == ['email.search', 'email.read']
            assert gmail.calls[-1][1] == {'message_id': 'qa-message'}
            assert 'formato devolvido não contém texto simples' not in result.message
        finally:
            await c.close()
    asyncio.run(scenario())


def test_read_without_readable_text_is_not_verified_success(tmp_path):
    async def scenario() -> None:
        gmail = FakeGmail()
        async def no_text(name: str, args: dict[str, Any], confirm: Any) -> dict[str, Any]:
            gmail.calls.append((name, dict(args)))
            return {'account': gmail.account['email'], 'untrusted_email_data': {
                'messageId': args['message_id'], 'subject': 'Synthetic subject'}}
        gmail.execute = no_text  # type: ignore[method-assign]
        c = controller(tmp_path, [{'kind': 'tool', 'operation': {'name': 'email.read',
            'args': {'message_id': 'qa-message'}}, 'finish': True}], gmail)
        try:
            result = await c.run('Lê a mensagem qa-message.', automatic=True)
            assert result.status != 'SUCCESS'
            assert result.evidence[0]['verified'] is False
            assert 'texto simples' in result.message
        finally:
            await c.close()
    asyncio.run(scenario())


def test_an_uncertain_send_clears_the_pending_draft_and_is_never_replayed(tmp_path):
    async def scenario() -> None:
        gmail = FakeGmail(send_error='uncertain')
        c = controller(tmp_path, [prepare(), {'kind': 'answer', 'action': None,
            'status': 'FINISH', 'message': ['Não há envio pendente.']}], gmail)
        try:
            await c.run(send_request(), automatic=True)
            first = await c.run('confirmo', automatic=True)
            assert first.status != 'SUCCESS'
            second = await c.run('confirmo', automatic=True)
            assert 'Não há envio pendente' in second.message
            assert [name for name, _ in gmail.calls] == ['email.prepare', 'email.send']
        finally:
            await c.close()
    asyncio.run(scenario())
