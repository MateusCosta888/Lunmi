import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Pencil, Trash2, X } from 'lucide-react';
import { describeError, getBridgeApi, type SendCommandFn } from '../../shared/ipc/client';
import type { ChatHistorySnapshotDto, ConversationDto, HistoryListDto } from '../../shared/types';
import { Icon } from '../../shared/ui';
import { BranchedMenu, type BranchedMenuItem } from '../../shared/BranchedMenu';
import './history.css';

export function HistoryDialog({ title, children, onClose }: {
  title: string; children: React.ReactNode; onClose: () => void;
}): React.ReactElement {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const opener = document.activeElement;
    (ref.current?.querySelector<HTMLElement>('input') ?? ref.current?.querySelector<HTMLElement>('button'))?.focus();
    return () => { if (opener instanceof HTMLElement && opener.isConnected) opener.focus(); };
  }, []);
  return createPortal(<div className="lunmi-history-backdrop" onClick={event => {
    if (event.target === event.currentTarget) onClose();
  }}>
    <div ref={ref} className="lunmi-history-dialog" role="dialog" aria-modal="true" aria-label={title}
      onKeyDown={event => {
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); onClose(); }
        if (event.key !== 'Tab') return;
        const nodes = Array.from(ref.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled)') ?? []);
        const target = event.shiftKey ? nodes.at(-1) : nodes[0];
        if (document.activeElement === (event.shiftKey ? nodes[0] : nodes.at(-1))) {
          event.preventDefault(); target?.focus();
        }
      }}>
      <header><h2>{title}</h2><button type="button" aria-label="Fechar" onClick={onClose}><X size={18} /></button></header>
      {children}
    </div>
  </div>, document.body);
}

function dateGroup(stamp: string): string {
  const date = new Date(stamp);
  const today = new Date();
  if (date.toDateString() === today.toDateString()) return 'Hoje';
  today.setDate(today.getDate() - 1);
  if (date.toDateString() === today.toDateString()) return 'Ontem';
  return date.toLocaleDateString('pt-PT', { month: 'long', year: 'numeric' });
}

export function HistoryNavigation({ sendCommand, activeId, collapsed, locked, onSelected,
  expanded = true, drawerOpen = false, onDrawerClose }: {
  sendCommand: SendCommandFn; activeId?: string; collapsed: boolean; locked: boolean;
  onSelected: () => void;
  expanded?: boolean; drawerOpen?: boolean; onDrawerClose?: () => void;
}): React.ReactElement {
  const [items, setItems] = useState<ConversationDto[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [version, setVersion] = useState(0);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState<{ item: ConversationDto; action: 'rename' | 'delete' } | null>(null);
  const [title, setTitle] = useState('');
  const generation = useRef(0);
  useEffect(() => getBridgeApi()?.onEvent(event => {
    if (event.type === 'chat.history.changed') setVersion(value => value + 1);
  }), []);
  useEffect(() => {
    const current = ++generation.current;
    const timer = window.setTimeout(() => {
      setLoading(true);
      void sendCommand<HistoryListDto>('chat.history.list', { query: '' }).then(page => {
        if (generation.current !== current) return;
        setItems(page.conversations); setHasMore(page.has_more); setError('');
      }).catch(err => { if (generation.current === current) setError(describeError(err)); })
        .finally(() => { if (generation.current === current) setLoading(false); });
    }, 0);
    return () => { window.clearTimeout(timer); ++generation.current; };
  }, [sendCommand, version]);

  const run = useCallback(async (operation: string, payload: Record<string, unknown>) => {
    setBusy(true); setError('');
    try {
      await sendCommand(`chat.history.${operation}`, payload);
      setEditing(null); setVersion(value => value + 1);
      if (operation === 'select' || operation === 'create') { onDrawerClose?.(); onSelected(); }
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  }, [sendCommand, onSelected, onDrawerClose]);
  const disabled = locked || busy;
  const rows: BranchedMenuItem[] = [
    { value: 'new', content: <button type="button" className="lunmi-history-action" data-testid="history-new" disabled={disabled}
      onClick={() => void run('create', {})}><Icon name="plus" size={16} />Nova conversa</button> },
    { value: 'temporary', content: <button type="button" className="lunmi-history-action" data-testid="history-temporary" disabled={disabled}
      onClick={() => void run('create', { temporary: true })}>Conversa temporária</button> },
  ];
  items.forEach((item, index) => {
    const group = dateGroup(item.updated_at);
    if (index === 0 || group !== dateGroup(items[index - 1].updated_at)) {
      rows.push({ value: `date-${index}`, heading: true, content: <h3>{group}</h3> });
    }
    rows.push({ value: item.id, content: <div className="lunmi-history-row" data-active={item.id === activeId}>
      <button type="button" className="lunmi-history-open" disabled={disabled} title={item.title}
        data-conversation-id={item.id} aria-current={item.id === activeId ? 'page' : undefined}
        onClick={() => void run('select', { conversation_id: item.id })}>
        <span>{item.title}</span><time dateTime={item.updated_at}>{new Date(item.updated_at).toLocaleDateString('pt-PT')}</time>
      </button>
      <button type="button" disabled={busy} aria-label={`Renomear ${item.title}`} onClick={() => { setTitle(item.title); setEditing({ item, action: 'rename' }); }}><Pencil size={13} /></button>
      <button type="button" disabled={disabled} aria-label={`Apagar ${item.title}`} onClick={() => setEditing({ item, action: 'delete' })}><Trash2 size={13} /></button>
    </div> });
  });
  const footer = <div className="lunmi-history-footer">
    {locked && <p role="status">Conclua ou pare a tarefa para mudar de conversa.</p>}
    {error && <p role="alert">{error}</p>}
    {loading && <p role="status">A carregar conversas…</p>}
    {!loading && !items.length && <p>As novas conversas ficam guardadas neste computador.</p>}
    {hasMore && <button type="button" disabled={loading} onClick={() => {
      const current = generation.current;
      setLoading(true);
      void sendCommand<HistoryListDto>('chat.history.list', { query: '', offset: items.length }).then(page => {
        if (generation.current !== current) return;
        setItems(previous => [...previous, ...page.conversations.filter(item => !previous.some(old => old.id === item.id))]);
        setHasMore(page.has_more);
      }).catch(err => { if (generation.current === current) setError(describeError(err)); })
        .finally(() => { if (generation.current === current) setLoading(false); });
    }}>Carregar mais conversas</button>}
  </div>;
  return <>
    {!collapsed && <BranchedMenu id="conversation-history" items={rows} activeValue={activeId} open={expanded} footer={footer} />}
    {collapsed && drawerOpen && <HistoryDialog title="Conversas" onClose={() => { if (!busy) onDrawerClose?.(); }}>
      <BranchedMenu id="conversation-history" items={rows} activeValue={activeId} open footer={footer} />
    </HistoryDialog>}
    {editing && <HistoryDialog title={editing.action === 'rename' ? 'Renomear conversa' : 'Apagar conversa'}
      onClose={() => { if (!busy) setEditing(null); }}>
      <form onSubmit={event => { event.preventDefault(); void run(editing.action, {
        conversation_id: editing.item.id, ...(editing.action === 'rename' ? { title } : { confirmed: true }),
      }); }}>
        {editing.action === 'rename' ? <label>Título<input autoFocus value={title} maxLength={80} required onChange={event => setTitle(event.target.value)} /></label>
          : <p>Apagar «{editing.item.title}» e as suas mensagens? Os factos da memória pessoal mantêm-se.</p>}
        {error && <p role="alert">{error}</p>}
        <footer><button type="button" disabled={busy} onClick={() => setEditing(null)}>Cancelar</button>
          <button type="submit" disabled={busy || editing.action === 'delete' && locked}>{editing.action === 'rename' ? 'Guardar título' : 'Apagar conversa'}</button></footer>
      </form>
    </HistoryDialog>}
  </>;
}

export function HistorySettings({ sendCommand, history, locked }: {
  sendCommand: SendCommandFn; history: ChatHistorySnapshotDto; locked: boolean;
}): React.ReactElement {
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const apply = async (command: string, payload: Record<string, unknown>) => {
    setBusy(true); setError('');
    try { await sendCommand(command, payload); setConfirm(false); }
    catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  };
  return <section className="lunmi-history-settings" aria-label="Histórico de conversas">
    <h3>Histórico de conversas</h3>
    <label><input type="checkbox" checked={history.options.enabled} disabled={busy}
      onChange={event => void apply('chat.history.options.set', { enabled: event.target.checked })} />Guardar conversas neste computador</label>
    <p>Desligar afeta as próximas mensagens. As conversas existentes e a memória pessoal mantêm-se.</p>
    <label>Ao iniciar a Lunmi<select value={history.options.startup} disabled={busy}
      onChange={event => void apply('chat.history.options.set', { startup: event.target.value })}>
      <option value="resume">Retomar a última conversa</option><option value="new">Começar uma nova</option>
    </select></label>
    <button type="button" disabled={busy || locked} onClick={() => setConfirm(true)}>Limpar histórico</button>
    {locked && <p>Conclua ou pare a tarefa antes de limpar o histórico.</p>}
    {error && <p role="alert">{error}</p>}
    {confirm && <HistoryDialog title="Limpar todo o histórico" onClose={() => { if (!busy) setConfirm(false); }}>
      <p>Apagar todas as conversas guardadas neste computador? Esta ação não apaga factos da memória pessoal.</p>
      {error && <p role="alert">{error}</p>}
      <footer><button type="button" disabled={busy} onClick={() => setConfirm(false)}>Cancelar</button>
        <button type="button" disabled={busy || locked} onClick={() => void apply('chat.history.clear', { confirmed: true })}>Apagar todo o histórico</button></footer>
    </HistoryDialog>}
  </section>;
}
