/**
 * Estado de conversa e tarefa do renderer (`desktop/src/features/chat/useChatTask.ts`, F2a / BG2).
 *
 * A conversa tem um único dono: a projeção pública em RAM do processo principal, alimentada pelos
 * eventos reais da ponte e publicada como snapshot + deltas por cursor. A janela principal e o
 * mini chat aplicam os mesmos deltas; nenhum dos dois inventa histórico nem abre sessão.
 *
 * Compatibilidade: com um preload antigo sem `window.lunmiPresence`, o hook reduz os mesmos
 * envelopes com `applyChatEvent` — a mesma função pura, não um segundo parser nem um segundo
 * dispatch. Nesse caminho o texto continua agrupado por frame (um rAF), como antes.
 *
 * Ações do utilizador (Parar, aprovar, responder) reconciliam a vista imediatamente; a decisão
 * real continua a chegar do backend com o mesmo `request_id`/`task_id`.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { SessionReadyPayload } from "../../shared/protocol";
import type { SendCommandFn } from "../../shared/ipc/client";
import { describeError, getBridgeApi, toBridgeError } from "../../shared/ipc/client";
import { getCachedPresenceSnapshot, getPresenceApi, rememberPresenceSnapshot } from "../../shared/ipc/presence";
import type {
  ChatMessageDto,
  ChatMode,
  ChatProjectionDeltaDto,
  ConfirmRequestDto,
  InputRequestDto,
  TaskStateDto,
} from "../../shared/types";
import {
  acknowledgeChatRequest,
  applyChatDelta,
  applyChatEvent,
  chatStateFromDto,
  createChatProjection,
  markChatStreamCancelled,
  type ChatEventLike,
  type ChatProjectionState,
} from "../../shared/chatProjection";

/** Limite prático do campo de escrita: o controller recusa instrução acima de 4000 caracteres. */
export const MAX_COMPOSER_CHARS = 4000;

/**
 * Resultado de um envio. O campo `accepted` é o que autoriza a UI a limpar o rascunho: uma
 * recusa (`task_busy`, `reconfiguring`, capacidade ausente, ponte fora) preserva o texto digitado.
 * Nada aqui reexecuta nem restaura uma instrução que já foi aceite.
 */
export interface SendOutcome {
  accepted: boolean;
  error?: string;
}

/** Anexo conforme contrato da ponte: conteúdo textual já lido no backend; nunca um caminho. */
export interface ChatAttachmentPayload {
  name: string;
  text: string;
  size_bytes?: number;
  mime?: string;
}

export interface ChatTaskState {
  messages: ChatMessageDto[];
  recentTasks: TaskStateDto[];
  task: TaskStateDto | null;
  confirmRequest: ConfirmRequestDto | null;
  inputRequest: InputRequestDto | null;
  busy: boolean;
  submitting: boolean;
  lastError: string | null;
  memoryVersion: number;
  send: (mode: ChatMode, text: string, attachments?: ChatAttachmentPayload[]) => Promise<SendOutcome>;
  stop: () => Promise<void>;
  approve: (requestId: string, approved: boolean) => Promise<void>;
  answerChoice: (requestId: string, index: number | null) => Promise<void>;
  answerLocation: (requestId: string, path: string) => Promise<void>;
  dismissError: () => void;
}

export function useChatTask(
  sendCommand: SendCommandFn,
  session: SessionReadyPayload | null,
  sessionVersion: number
): ChatTaskState {
  const [projection, setProjection] = useState<ChatProjectionState>(() => {
    // Abrir o mini a meio de uma sessão não deve mostrar um frame vazio: o snapshot público já
    // lido por outra vista serve de base; a hidratação a seguir confirma-o.
    const cached = getCachedPresenceSnapshot();
    return cached ? chatStateFromDto(cached.chat) : createChatProjection(session?.session_id ?? "");
  });
  const [submitting, setSubmitting] = useState(false);
  const [lastError, setLastError] = useState<string | null>(null);
  const [memoryVersion, setMemoryVersion] = useState(0);
  const taskRef = useRef<TaskStateDto | null>(null);
  const liveStateRef = useRef<ChatProjectionState>(projection);
  const frameRef = useRef<number | null>(null);

  useEffect(() => {
    taskRef.current = projection.task;
  }, [projection.task]);

  const cancelPendingFrame = useCallback(() => {
    if (frameRef.current !== null) {
      cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
    }
  }, []);

  /** Aplica já: ações do utilizador e mudanças estruturais não esperam frame. */
  const commitLocal = useCallback(
    (next: ChatProjectionState) => {
      liveStateRef.current = next;
      cancelPendingFrame();
      setProjection(next);
    },
    [cancelPendingFrame]
  );

  // Sessão diferente zera a vista local antes do snapshot; nada é reexecutado automaticamente.
  // A sessão só chega depois do cache, por isso a comparação evita apagar uma conversa válida
  // no primeiro render (null → sessão atual).
  useEffect(() => {
    const nextId = session?.session_id ?? "";
    const current = liveStateRef.current.sessionId;
    if (!nextId || nextId === current) return;
    const fresh = createChatProjection(nextId);
    taskRef.current = null;
    liveStateRef.current = fresh;
    if (frameRef.current !== null) {
      cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
    }
    setProjection(fresh);
    setSubmitting(false);
    setLastError(null);
  }, [session?.session_id, sessionVersion]);

  // Projeção do main: subscrever primeiro, obter snapshot com watermark, descartar os eventos já
  // representados e aplicar só os posteriores. O main já agrupa deltas; aqui é imediato.
  useEffect(() => {
    const api = getPresenceApi();
    if (!api?.snapshot) {
      return;
    }
    let cancelled = false;
    let watermark = -1;
    // Só os deltas chegados antes do snapshot são guardados; depois dele aplicam-se já.
    // Um snapshot tardio é sempre mais velho que o que já se aplicou: cada delta traz cursor e
    // sessão, por isso a redução pura descarta duplicados, velhos e sessões alheias.
    let buffered: ChatProjectionDeltaDto[] = [];
    const apply = (delta: ChatProjectionDeltaDto) => {
      const next = applyChatDelta(liveStateRef.current, delta);
      liveStateRef.current = next;
      setProjection(next);
    };
    const off = api.onEvent((event) => {
      if (event.type !== "chat") return;
      const delta = event.delta;
      if (watermark < 0) {
        buffered.push(delta);
        return;
      }
      watermark = Math.max(watermark, delta.cursor);
      apply(delta);
    });
    void api
      .snapshot()
      .then((snapshot) => {
        if (cancelled) return;
        rememberPresenceSnapshot(snapshot);
        const chat = snapshot.chat;
        const base = chatStateFromDto(chat);
        liveStateRef.current = base;
        setProjection(base);
        watermark = chat.cursor;
        const replay = buffered.sort((left, right) => left.cursor - right.cursor);
        buffered = [];
        for (const delta of replay) {
          if (delta.cursor <= watermark) continue;
          watermark = delta.cursor;
          apply(delta);
        }
      })
      .catch(() => {
        if (cancelled) return;
        // Hidratação falhou: sair do estado de acumulação em vez de crescer buffered para
        // sempre. Os deltas seguintes aplicam-se já e o erro fica visível; sem polling.
        buffered = [];
        watermark = liveStateRef.current.cursor;
        setLastError("Não foi possível sincronizar a conversa. As mensagens novas continuam a aparecer.");
      });
    return () => {
      cancelled = true;
      off();
    };
    // Uma única subscrição: a sessão reinicia por delta `reset` do main; re-subscribir em cada
    // mudança de sessão perdia deltas emitidos entre o evento e a nova subscrição.
  }, []);

  // Um único listener dos envelopes: memória sempre; chat só quando não há projeção do main
  // (peer antigo), reduzido pela mesma função pura e com o agrupamento por frame de sempre.
  useEffect(() => {
    const api = getBridgeApi();
    if (!api) {
      return;
    }
    if (getPresenceApi()?.snapshot) {
      return api.onEvent((event) => {
        if (event.type === "memory.changed") setMemoryVersion((value) => value + 1);
      });
    }
    let local = createChatProjection(session?.session_id ?? "");
    liveStateRef.current = local;
    setProjection(local);
    const flush = () => {
      if (frameRef.current !== null) {
        cancelAnimationFrame(frameRef.current);
        frameRef.current = null;
      }
      setProjection(local);
    };
    const stage = () => {
      if (frameRef.current !== null) return;
      frameRef.current = requestAnimationFrame(() => {
        frameRef.current = null;
        setProjection(local);
      });
    };
    const off = api.onEvent((event) => {
      if (event.type === "memory.changed") {
        setMemoryVersion((value) => value + 1);
        return;
      }
      const outcome = applyChatEvent(local, event as unknown as ChatEventLike);
      if (!outcome) return;
      local = outcome.state;
      liveStateRef.current = outcome.state;
      const delta = outcome.delta;
      const structural =
        delta.task !== undefined ||
        delta.confirm !== undefined ||
        delta.input !== undefined ||
        delta.recent_tasks !== undefined;
      const terminal =
        event.type === "chat.message" ||
        event.type === "response.completed" ||
        event.type === "response.error" ||
        event.type === "response.cancelled" ||
        event.type === "task.finished" ||
        event.type === "task.cancelled" ||
        event.type === "task.error";
      if (structural || terminal) flush();
      else stage();
    });
    return () => {
      off();
      if (frameRef.current !== null) {
        cancelAnimationFrame(frameRef.current);
        frameRef.current = null;
      }
    };
  }, [session?.session_id, sessionVersion]);

  // O texto de erro de uma tarefa falhada continua visível uma vez, sem duplicar o evento.
  useEffect(() => {
    const task = projection.task;
    if (task && task.phase === "failed" && task.error) {
      setLastError(task.error);
    }
  }, [projection.task?.task_id, projection.task?.phase, projection.task?.error, projection.task]);

  const send = useCallback(
    async (mode: ChatMode, text: string, attachments?: ChatAttachmentPayload[]): Promise<SendOutcome> => {
      const instruction = text.trim();
      if (!instruction) {
        const message = "Escreva uma instrução antes de enviar.";
        setLastError(message);
        return { accepted: false, error: message };
      }
      if (instruction.length > MAX_COMPOSER_CHARS) {
        const message = `A instrução excede ${MAX_COMPOSER_CHARS} caracteres.`;
        setLastError(message);
        return { accepted: false, error: message };
      }
      // Capacidade anunciada é capacidade real: se a sessão não anunciou o modo, ele não vai.
      if (mode === "auto" ? session?.capabilities.auto !== true : session && session.capabilities[mode] === false) {
        const message =
          mode === "auto"
            ? "O chat automático não foi anunciado por esta sessão."
            : mode === "converse"
            ? "O modo Conversar não foi anunciado por esta sessão."
            : "O modo Executar não foi anunciado por esta sessão.";
        setLastError(message);
        return { accepted: false, error: message };
      }
      setSubmitting(true);
      setLastError(null);
      try {
        // Sem anexos, o payload continua exatamente { mode, text } — o fluxo normal não muda.
        const payload: Record<string, unknown> = { mode, text: instruction };
        if (attachments && attachments.length > 0) {
          payload.attachments = attachments;
        }
        await sendCommand("chat.send", payload);
        setSubmitting(false);
        return { accepted: true };
      } catch (err) {
        // Recusa antes de qualquer aceite: o texto continua na tela e nada é reexecutado.
        const message = describeError(err);
        setSubmitting(false);
        setLastError(message);
        return { accepted: false, error: message };
      }
    },
    [sendCommand, session]
  );

  const stop = useCallback(async () => {
    const current = taskRef.current;
    if (!current) {
      return;
    }
    // Invalida o texto em curso antes de esperar o IPC; deltas tardios não voltam a entrar.
    commitLocal(markChatStreamCancelled(liveStateRef.current));
    try {
      await sendCommand("task.cancel", { task_id: current.task_id });
    } catch (err) {
      setLastError(describeError(err));
    }
  }, [commitLocal, sendCommand]);

  const approve = useCallback(
    async (requestId: string, approved: boolean) => {
      const current = taskRef.current;
      try {
        await sendCommand("task.confirm", {
          request_id: requestId,
          approved,
          task_id: current?.task_id ?? "",
        });
        commitLocal(acknowledgeChatRequest(liveStateRef.current, current?.task_id, requestId));
      } catch (err) {
        setLastError(describeError(err));
      }
    },
    [commitLocal, sendCommand]
  );

  const answerChoice = useCallback(
    async (requestId: string, index: number | null) => {
      const current = taskRef.current;
      const payload: Record<string, unknown> = {
        request_id: requestId,
        kind: "choice",
        task_id: current?.task_id ?? "",
      };
      if (index !== null) {
        payload.index = index;
      }
      try {
        await sendCommand("task.answer", payload);
        commitLocal(acknowledgeChatRequest(liveStateRef.current, current?.task_id, requestId));
      } catch (err) {
        setLastError(describeError(err));
      }
    },
    [commitLocal, sendCommand]
  );

  const answerLocation = useCallback(
    async (requestId: string, path: string) => {
      const current = taskRef.current;
      try {
        await sendCommand("task.answer", {
          request_id: requestId,
          kind: "location",
          path,
          task_id: current?.task_id ?? "",
        });
        commitLocal(acknowledgeChatRequest(liveStateRef.current, current?.task_id, requestId));
      } catch (err) {
        setLastError(describeError(err));
      }
    },
    [commitLocal, sendCommand]
  );

  const dismissError = useCallback(() => setLastError(null), []);

  const busy = useMemo(() => {
    const task = projection.task;
    if (!task) {
      return false;
    }
    return (
      task.phase === "running" ||
      task.phase === "awaiting_confirm" ||
      task.phase === "awaiting_input"
    );
  }, [projection.task]);

  return {
    messages: projection.messages,
    recentTasks: projection.recentTasks,
    task: projection.task,
    confirmRequest: projection.confirm,
    inputRequest: projection.input,
    busy,
    submitting,
    lastError,
    memoryVersion,
    send,
    stop,
    approve,
    answerChoice,
    answerLocation,
    dismissError,
  };
}

/** Erro de comando com código, para a UI distinguir `not_implemented` de falha real. */
export function commandErrorCode(err: unknown): string {
  return toBridgeError(err).code;
}
