/**
 * Chat do mini companheiro (`desktop/src/features/chat/useCompanionChat.ts`).
 *
 * Reutiliza `useChatTask` e a projeção do processo principal: mesma conversa, mesmos
 * `message_id`, mesmas ações — o mini não abre sessão, não chama o controller por outro caminho
 * e não recebe comandos fora da allowlist (`chat.send`, `task.cancel`, `voice.stop`, `voice.get`).
 */
import { useMemo } from "react";
import { BridgeError, getBridgeApi, toBridgeError, type SendCommandFn } from "../../shared/ipc/client";
import { getPresenceApi, usePresenceSession } from "../../shared/ipc/presence";
import { useChatTask, type ChatTaskState } from "./useChatTask";

/** Comando permitido ao companheiro; o main rejeita qualquer outro tipo. */
export const COMPANION_COMMAND_ALLOWLIST = [
  "chat.send",
  "task.cancel",
  "voice.stop",
  "voice.get",
] as const;

export function useCompanionChat(): ChatTaskState {
  const { session, sessionVersion } = usePresenceSession();

  const sendCommand = useMemo<SendCommandFn>(() => {
    const presence = getPresenceApi();
    const main = getBridgeApi();
    return async function sendCompanionCommand<TResult = Record<string, unknown>>(
      type: string,
      payload: Record<string, unknown> = {}
    ): Promise<TResult> {
      try {
        if (presence?.sendCommand) {
          return await presence.sendCommand<TResult>(type, payload);
        }
        if (main) {
          return await main.sendCommand<TResult>(type, payload);
        }
        throw new BridgeError("bridge_unavailable", "Ponte do companheiro indisponível.");
      } catch (err) {
        throw toBridgeError(err);
      }
    };
  }, [sessionVersion]);

  return useChatTask(sendCommand, session, sessionVersion);
}
