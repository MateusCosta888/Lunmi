/**
 * Estado de conversa e tarefa do renderer (`desktop/src/features/chat/useChatTask.ts`, F2a).
 *
 * O hook é a única fonte de verdade do chat: o backend Python já emite `chat.message` para o
 * usuário e para a assistente, então o renderer nunca inventa histórico nem duplica bolhas — ele
 * apenas ordena os eventos reais. Nada aqui declara paridade: cada campo vem do
 * `TaskResultDto` publicado pela ponte.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { SessionReadyPayload } from "../../shared/protocol";
import type { SendCommandFn } from "../../shared/ipc/client";
import { describeError, getBridgeApi, toBridgeError } from "../../shared/ipc/client";
import type {
  ChatMessageDto,
  ChatMode,
  ConfirmRequestDto,
  InputRequestDto,
  TaskResultDto,
  TaskStateDto,
} from "../../shared/types";

/** Limite prático do campo de escrita: o controller recusa instrução acima de 4000 caracteres. */
export const MAX_COMPOSER_CHARS = 4000;

const BACKEND_FAILURE_CODES = new Set<string>([
  "backend_exited",
  "backend_spawn_failed",
  "backend_pipe_error",
]);

/**
 * Resultado de um envio. O campo `accepted` é o que autoriza a UI a limpar o rascunho: uma
 * recusa (`task_busy`, `reconfiguring`, capacidade ausente, ponte fora) preserva o texto digitado.
 * Nada aqui reexecuta nem restaura uma instrução que já foi aceita.
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

function initialState(task: TaskStateDto | null, phase: TaskStateDto["phase"], requestId: string): TaskStateDto | null {
  if (!task) {
    return task;
  }
  return { ...task, phase, pending_request_id: requestId };
}

function resumeIfPending(task: TaskStateDto | null, taskId: string | undefined, requestId: string): TaskStateDto | null {
  // A continuação pode terminar ou perguntar de novo antes de command.result chegar.
  return task && task.task_id === taskId && task.pending_request_id === requestId
    && (task.phase === "awaiting_confirm" || task.phase === "awaiting_input")
    ? { ...task, phase: "running", pending_request_id: undefined } : task;
}

export function useChatTask(
  sendCommand: SendCommandFn,
  session: SessionReadyPayload | null,
  sessionVersion: number
): ChatTaskState {
  const [messages, setMessages] = useState<ChatMessageDto[]>([]);
  const [recentTasks, setRecentTasks] = useState<TaskStateDto[]>([]);
  const [task, setTask] = useState<TaskStateDto | null>(null);
  const [confirmRequest, setConfirmRequest] = useState<ConfirmRequestDto | null>(null);
  const [inputRequest, setInputRequest] = useState<InputRequestDto | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [lastError, setLastError] = useState<string | null>(null);
  const [memoryVersion, setMemoryVersion] = useState(0);
  const taskRef = useRef<TaskStateDto | null>(null);

  useEffect(() => {
    taskRef.current = task;
  }, [task]);

  // Uma sessão nova (hello ou restart) zera o histórico: nada é reexecutado automaticamente.
  useEffect(() => {
    setMessages([]);
    setRecentTasks([]);
    setTask(null);
    setConfirmRequest(null);
    setInputRequest(null);
    setSubmitting(false);
    setLastError(null);
  }, [sessionVersion]);

  useEffect(() => {
    const api = getBridgeApi();
    if (!api) {
      return;
    }
    const unsubscribe = api.onEvent((event) => {
      const payload = (event.payload ?? {}) as Record<string, unknown>;
      switch (event.type) {
        case "chat.message": {
          const message = payload as unknown as ChatMessageDto;
          if (typeof message.text !== "string") {
            return;
          }
          setMessages((prev) => [...prev, message]);
          return;
        }
        case "task.started": {
          const taskId = String(payload.task_id ?? "");
          const mode = (payload.mode === "converse" ? "converse" : "execute") as ChatMode;
          setTask({
            task_id: taskId,
            mode,
            instruction: String(payload.instruction ?? ""),
            phase: "running",
            progress: "",
            result: null,
            error: null,
          });
          setConfirmRequest(null);
          setInputRequest(null);
          setLastError(null);
          return;
        }
        case "task.progress": {
          const taskId = String(payload.task_id ?? "");
          setTask((prev) =>
            prev && (prev.task_id === taskId || taskId === "")
              ? { ...prev, progress: String(payload.state ?? "") }
              : prev
          );
          return;
        }
        case "task.need_confirm": {
          setConfirmRequest(payload as unknown as ConfirmRequestDto);
          setTask((prev) => initialState(prev, "awaiting_confirm", String(payload.request_id)));
          return;
        }
        case "task.need_input": {
          setInputRequest(payload as unknown as InputRequestDto);
          setTask((prev) => initialState(prev, "awaiting_input", String(payload.request_id)));
          return;
        }
        case "task.finished": {
          const result = payload as unknown as TaskResultDto;
          const phase =
            result.status === "SUCCESS"
              ? "finished"
              : result.status === "CANCELLED"
                ? "cancelled"
                : "failed";
          setTask((prev) => (prev ? { ...prev, phase, result, progress: "" } : prev));
          setConfirmRequest(null);
          setInputRequest(null);
          setSubmitting(false);
          return;
        }
        case "task.cancelled": {
          const result = (payload.result ?? null) as TaskResultDto | null;
          setTask((prev) => (prev ? { ...prev, phase: "cancelled", result } : prev));
          setConfirmRequest(null);
          setInputRequest(null);
          setSubmitting(false);
          return;
        }
        case "task.error": {
          const code = String(payload.code ?? "");
          // Falha de sessão é tratada pelo useSession (banner + restart); aqui não é erro de chat.
          if (BACKEND_FAILURE_CODES.has(code)) {
            setSubmitting(false);
            return;
          }
          setLastError(String(payload.message ?? "Falha ao executar a tarefa."));
          setTask((prev) => (prev ? { ...prev, phase: "failed", progress: "" } : prev));
          setSubmitting(false);
          return;
        }
        case "memory.changed": {
          setMemoryVersion((v) => v + 1);
          return;
        }
        default:
          return;
      }
    });
    return () => {
      unsubscribe();
    };
  }, []);

  useEffect(() => {
    if (!task) return;
    setRecentTasks(previous => [task, ...previous.filter(item => item.task_id !== task.task_id)].slice(0, 6));
  }, [task]);

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
      if (session && session.capabilities[mode] === false) {
        const message =
          mode === "converse"
            ? "O modo Conversar não foi anunciado por esta sessão."
            : "O modo Executar não foi anunciado por esta sessão."
        ;
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
    try {
      await sendCommand("task.cancel", { task_id: current.task_id });
    } catch (err) {
      setLastError(describeError(err));
    }
  }, [sendCommand]);

  const approve = useCallback(
    async (requestId: string, approved: boolean) => {
      const current = taskRef.current;
      try {
        await sendCommand("task.confirm", {
          request_id: requestId,
          approved,
          task_id: current?.task_id ?? "",
        });
        setConfirmRequest(prev => prev?.request_id === requestId ? null : prev);
        setTask(prev => resumeIfPending(prev, current?.task_id, requestId));
      } catch (err) {
        setLastError(describeError(err));
      }
    },
    [sendCommand]
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
        setInputRequest(prev => prev?.request_id === requestId ? null : prev);
        setTask(prev => resumeIfPending(prev, current?.task_id, requestId));
      } catch (err) {
        setLastError(describeError(err));
      }
    },
    [sendCommand]
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
        setInputRequest(prev => prev?.request_id === requestId ? null : prev);
        setTask(prev => resumeIfPending(prev, current?.task_id, requestId));
      } catch (err) {
        setLastError(describeError(err));
      }
    },
    [sendCommand]
  );

  const dismissError = useCallback(() => setLastError(null), []);

  const busy = useMemo(() => {
    if (!task) {
      return false;
    }
    return (
      task.phase === "running" ||
      task.phase === "awaiting_confirm" ||
      task.phase === "awaiting_input"
    );
  }, [task]);

  return {
    messages,
    recentTasks,
    task,
    confirmRequest,
    inputRequest,
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
