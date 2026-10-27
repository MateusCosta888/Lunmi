import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { HistoryNavigation, HistorySettings } from '../src/features/chat/HistoryNavigation';
import { RubberNavigation } from '../src/shared/RubberNavigation';
import { applyChatDelta, applyChatEvent, createChatProjection, mergeChatDelta, projectChat } from '../src/shared/chatProjection';
import { ChatProjectionStore } from '../electron/chat-projection';
import { useChatTask } from '../src/features/chat/useChatTask';
import type { LunmiBridgeApi } from '../src/shared/ipc/client';
import type { SendCommandFn } from '../src/shared/ipc/client';
import type { ChatHistorySnapshotDto, ChatProjectionDeltaDto } from '../src/shared/types';
import type { SessionReadyPayload } from '../src/shared/protocol';

const conversation = { id: 'c1', title: 'Projeto azul', created_at: '2026-10-25T10:00:00Z', updated_at: '2026-10-25T10:00:00Z', saved: true, temporary: false };
const history: ChatHistorySnapshotDto = { conversation, messages: [
  { role: 'user', text: 'Azul', message_id: 'u1', task_id: 't1', conversation_id: 'c1' },
  { role: 'assistant', text: 'Sim', message_id: 'a1', task_id: 't1', conversation_id: 'c1' },
], options: { enabled: true, startup: 'resume' }, error: null, has_more: false, before: 1 };
const session = { session_id: 's1', chat_history: history } as SessionReadyPayload;

describe('histórico e projeção das duas janelas', () => {
  it('confirma com a identidade atual mesmo antes de publicar um frame React', async () => {
    const previousApi = window.lunmi;
    let listener!: Parameters<LunmiBridgeApi['onEvent']>[0];
    window.lunmi = { onEvent: (callback: typeof listener) => { listener = callback; return () => undefined; } } as unknown as LunmiBridgeApi;
    const send = vi.fn().mockResolvedValue({}) as unknown as SendCommandFn;
    const currentSession = { ...session, chat_history: undefined,
      capabilities: { converse: true, execute: true, memory: false, brain: false,
        web_search: false, browser_use: false, chat_history: false } };
    try {
      const hook = renderHook(() => useChatTask(send, currentSession, 1));
      await act(async () => {
        listener({ v: 1, id: 'start', type: 'task.started', payload: { task_id: 'current-task', mode: 'execute' } });
        listener({ v: 1, id: 'confirm', type: 'task.need_confirm', payload: { task_id: 'current-task', request_id: 'confirm-current', message: 'Confirmar?' } });
        await hook.result.current.approve('confirm-current', true);
      });
      expect(send).toHaveBeenCalledWith('task.confirm', { task_id: 'current-task', request_id: 'confirm-current', approved: true });
    } finally { window.lunmi = previousApi; }
  });

  it('uma leitura atrasada do preload legado não recupera a sessão anterior', async () => {
    const previousApi = window.lunmi;
    window.lunmi = { onEvent: () => () => undefined } as unknown as LunmiBridgeApi;
    let resolveOld!: (value: ChatHistorySnapshotDto) => void;
    const old = new Promise<ChatHistorySnapshotDto>(resolve => { resolveOld = resolve; });
    const current = { ...history, conversation: { ...conversation, id: 'c2' }, messages: [] };
    const send = vi.fn().mockReturnValueOnce(old).mockResolvedValue(current) as unknown as SendCommandFn;
    const state = (id: string): SessionReadyPayload => ({ v: 1, pid: 1, session_id: id,
      capabilities: { converse: true, execute: true, memory: false, brain: false,
        web_search: false, browser_use: false, chat_history: true },
      limits: { max_line_bytes: 5242880, max_import_bytes: 4194304, max_chat_text_chars: 8000 } });
    try {
      const hook = renderHook(({ id }) => useChatTask(send, state(id), 1), { initialProps: { id: 's1' } });
      hook.rerender({ id: 's2' });
      await waitFor(() => expect(hook.result.current.conversationId).toBe('c2'));
      await act(async () => { resolveOld(history); await old; });
      expect(hook.result.current.conversationId).toBe('c2');
    } finally { window.lunmi = previousApi; }
  });

  it('hidrata por sessão uma só vez, sem tarefas, confirmações ou streams', () => {
    const deltas: ChatProjectionDeltaDto[] = [];
    const store = new ChatProjectionStore(delta => deltas.push(delta));
    store.syncSession(session);
    const main = deltas.reduce(applyChatDelta, createChatProjection());
    const mini = deltas.reduce(applyChatDelta, createChatProjection());
    expect(projectChat(main)).toEqual(store.snapshot());
    expect(projectChat(mini)).toEqual(store.snapshot());
    expect(store.snapshot().task).toBeNull();
    expect(store.snapshot().confirm).toBeNull();
    expect(store.syncSession(session)).toBe(false);
    expect(store.snapshot().messages).toHaveLength(2);
  });

  it('troca a conversa e rejeita deltas da conversa anterior; pagina sem duplicar', () => {
    const store = new ChatProjectionStore(() => undefined);
    store.syncSession(session);
    store.apply({ v: 1, id: 'select', type: 'chat.history.changed', payload: { ...history,
      conversation: { ...conversation, id: 'c2' }, conversation_id: 'c2', messages: [], reason: 'selected', session_id: 's1' } });
    expect(store.snapshot().messages).toHaveLength(0);
    expect(store.apply({ v: 1, id: 'old', type: 'chat.message', payload: {
      conversation_id: 'c1', role: 'assistant', text: 'stale', task_id: 't1' } })).toBe(false);
    store.apply({ v: 1, id: 'page', type: 'chat.history.changed', payload: {
      conversation_id: 'c2', reason: 'page', messages: history.messages, has_more: false, before: null } });
    store.apply({ v: 1, id: 'page-duplicate', type: 'chat.history.changed', payload: {
      conversation_id: 'c2', reason: 'page', messages: history.messages, has_more: false, before: null } });
    expect(store.snapshot().messages).toHaveLength(2);
  });

  it('um reset coalescido conserva a identidade e a primeira mensagem nova', () => {
    const state = createChatProjection('s1');
    const selected = applyChatEvent(state, { type: 'chat.history.changed', payload: {
      ...history, conversation_id: 'c1', reason: 'selected' } })!;
    const next = applyChatEvent(selected.state, { type: 'chat.message', payload: {
      conversation_id: 'c1', role: 'user', text: 'Continua', task_id: 't2', message_id: 'u2' } })!;
    const renderer = applyChatDelta(state, mergeChatDelta(selected.delta, next.delta));
    expect(renderer.conversationId).toBe('c1');
    expect(renderer.messages.map(item => item.text)).toEqual(['Azul', 'Sim', 'Continua']);
  });
});

describe('gestão do histórico', () => {
  function sender() {
    return vi.fn(async (type: string) => type === 'chat.history.list'
      ? { conversations: [conversation], has_more: false } : {}) as unknown as SendCommandFn;
  }

  it('remove a pesquisa da interface e exige confirmação própria para apagar', async () => {
    const send = sender();
    render(<HistoryNavigation sendCommand={send} activeId="c1" collapsed={false} locked={false} onSelected={vi.fn()} />);
    await screen.findByRole('button', { name: 'Apagar Projeto azul' });
    expect(screen.queryByRole('searchbox')).toBeNull();
    expect(send).toHaveBeenCalledWith('chat.history.list', { query: '' });
    fireEvent.click(screen.getByRole('button', { name: 'Apagar Projeto azul' }));
    expect(send).not.toHaveBeenCalledWith('chat.history.delete', expect.anything());
    fireEvent.click(screen.getByRole('button', { name: 'Apagar conversa' }));
    await waitFor(() => expect(send).toHaveBeenCalledWith('chat.history.delete', { conversation_id: 'c1', confirmed: true }));
  });

  it('gaveta acessível restaura foco com Escape e bloqueia a gestão durante tarefas', async () => {
    const send = sender();
    function Drawer() {
      const [open, setOpen] = useState(false);
      return <><button onClick={() => setOpen(true)}>Conversa</button>
        <HistoryNavigation sendCommand={send} collapsed locked onSelected={vi.fn()}
          drawerOpen={open} onDrawerClose={() => setOpen(false)} /></>;
    }
    render(<Drawer />);
    const opener = screen.getByRole('button', { name: 'Conversa' });
    opener.focus(); fireEvent.click(opener);
    const dialog = screen.getByRole('dialog', { name: 'Conversas' });
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Fechar' }));
    expect((screen.getByRole('button', { name: 'Nova conversa' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Conversa temporária' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(opener);
    expect(send).not.toHaveBeenCalledWith('chat.history.create', expect.anything());
  });

  it('coloca a sublista sob Conversa e mantém as setas nas abas principais', async () => {
    const send = sender();
    function Navigation() {
      const [view, setView] = useState('chat');
      return <RubberNavigation items={[{ id: 'chat', label: 'Conversa', icon: 'chat' },
        { id: 'memory', label: 'Memória', icon: 'memory' }]} value={view} disabled={false} onChange={setView}
        branch={{ id: 'chat', controls: 'conversation-history', expanded: view === 'chat', content:
          <HistoryNavigation sendCommand={send} activeId="c1" collapsed={false} locked={false}
            expanded={view === 'chat'} onSelected={vi.fn()} /> }} />;
    }
    const view = render(<Navigation />);
    await screen.findByRole('button', { name: 'Apagar Projeto azul' });
    const chat = screen.getByTestId('nav-chat');
    const branch = view.container.querySelector('#conversation-history')!;
    expect(chat.nextElementSibling).toBe(branch);
    expect(branch.hasAttribute('data-open')).toBe(true);
    fireEvent.keyDown(chat, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(screen.getByTestId('nav-memory'));
    expect(branch.hasAttribute('inert')).toBe(true);
    expect(chat.getAttribute('aria-expanded')).toBe('false');
  });

  it('renomeia e cria uma conversa temporária sem chamada ao Brain', async () => {
    const send = sender();
    render(<HistoryNavigation sendCommand={send} collapsed={false} locked={false} onSelected={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Renomear Projeto azul' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Título' }), { target: { value: 'Órbita' } });
    fireEvent.click(screen.getByRole('button', { name: 'Guardar título' }));
    await waitFor(() => expect(send).toHaveBeenCalledWith('chat.history.rename', { conversation_id: 'c1', title: 'Órbita' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    fireEvent.click(screen.getByRole('button', { name: 'Conversa temporária' }));
    await waitFor(() => expect(send).toHaveBeenCalledWith('chat.history.create', { temporary: true }));
    expect(vi.mocked(send).mock.calls.every(([type]) => type.startsWith('chat.history.'))).toBe(true);
  });

  it('carrega uma lista extensa em páginas de cinquenta sem repetir linhas', async () => {
    const items = Array.from({ length: 55 }, (_, i) => ({ ...conversation, id: `c${i}`, title: `Conversa ${i}` }));
    const send = vi.fn(async (_type: string, payload: Record<string, unknown>) => {
      const offset = Number(payload.offset ?? 0);
      return { conversations: items.slice(offset, offset + 50), has_more: offset === 0 };
    }) as unknown as SendCommandFn;
    const view = render(<HistoryNavigation sendCommand={send} collapsed={false} locked={false} onSelected={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Carregar mais conversas' }));
    await waitFor(() => expect(view.container.querySelectorAll('.lunmi-history-row')).toHaveLength(55));
    expect(send).toHaveBeenCalledWith('chat.history.list', { query: '', offset: 50 });
  });

  it('controla gravação e arranque e pede uma confirmação separada para limpar tudo', async () => {
    const send = sender();
    render(<HistorySettings sendCommand={send} history={history} locked={false} />);
    fireEvent.click(screen.getByRole('checkbox'));
    await waitFor(() => expect(send).toHaveBeenCalledWith('chat.history.options.set', { enabled: false }));
    await act(async () => undefined);
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'new' } });
    await waitFor(() => expect(send).toHaveBeenCalledWith('chat.history.options.set', { startup: 'new' }));
    await act(async () => undefined);
    fireEvent.click(screen.getByRole('button', { name: 'Limpar histórico' }));
    expect(screen.getByRole('dialog', { name: 'Limpar todo o histórico' })).toBeTruthy();
    expect(send).not.toHaveBeenCalledWith('chat.history.clear', expect.anything());
    fireEvent.click(screen.getByRole('button', { name: 'Apagar todo o histórico' }));
    await waitFor(() => expect(send).toHaveBeenCalledWith('chat.history.clear', { confirmed: true }));
  });
});
