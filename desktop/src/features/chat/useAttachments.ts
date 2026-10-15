/**
 * Anexos do compositor (`desktop/src/features/chat/useAttachments.ts`).
 *
 * Contrato (Codador, 2026-10-15): `window.lunmi.pickAttachments?.()` abre o diálogo nativo no
 * main e devolve apenas texto UTF-8 já lido pelo backend — nenhum caminho passa do renderer.
 * Limites espelhados do backend, validados sobre o conjunto (anexos já listados + novos):
 * 4 arquivos, 120 chars no nome, 32 KiB por arquivo, 64 KiB no total. Nada é descartado em
 * silêncio: todo descarte vira aviso explícito. Cancelar não é erro; selecionar não envia; o
 * conteúdo é dado não confiável, nunca instrução de sistema, consentimento ou memória. Erros
 * mostram o motivo, nunca o conteúdo do arquivo.
 */
import { useCallback, useRef, useState } from "react";
import { getBridgeApi } from "../../shared/ipc/client";
import type { AttachmentDto } from "../../shared/types";

export const MAX_ATTACHMENTS = 4;
export const MAX_ATTACHMENT_NAME_CHARS = 120;
export const MAX_ATTACHMENT_BYTES = 32768;
export const MAX_ATTACHMENT_TOTAL_BYTES = 65536;

interface PickResult {
  cancelled: boolean;
  attachments: Array<{ id: string; name: string; size_bytes: number; sha256: string; text: string }>;
  errors: string[];
}

type BridgeWithPicks = { pickAttachments?: () => Promise<PickResult> };

export interface AttachmentsState {
  items: AttachmentDto[];
  /** Conteúdo textual por id; só via payload de `chat.send`, nunca exibido cru. */
  contents: Map<string, string>;
  notice: string | null;
  supported: boolean;
  picking: boolean;
  pick: () => Promise<void>;
  remove: (id: string) => void;
  clear: () => void;
  dismissNotice: () => void;
}

export function useAttachments(): AttachmentsState {
  const [items, setItems] = useState<AttachmentDto[]>([]);
  const [contents, setContents] = useState<Map<string, string>>(new Map());
  const [notice, setNotice] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const itemsRef = useRef(items);
  itemsRef.current = items;
  const supported = Boolean((getBridgeApi() as (BridgeWithPicks & object) | undefined)?.pickAttachments);

  const pick = useCallback(async () => {
    const api = getBridgeApi() as BridgeWithPicks | undefined;
    if (!api?.pickAttachments) {
      setNotice("Anexos ainda não estão disponíveis nesta versão da ponte.");
      return;
    }
    if (picking) {
      return;
    }
    setPicking(true);
    setNotice(null);
    try {
      const result = await api.pickAttachments();
      if (result.cancelled) {
        return;
      }
      const previous = itemsRef.current;
      const accepted: AttachmentDto[] = [];
      const nextContents = new Map<string, string>();
      const problems: string[] = [...(result.errors ?? [])];
      let total = previous.reduce((sum, item) => sum + item.size, 0);
      let slots = MAX_ATTACHMENTS - previous.length;
      for (const file of result.attachments ?? []) {
        const name = file.name ?? "";
        const bytes = Number(file.size_bytes ?? new TextEncoder().encode(file.text ?? "").length);
        if (slots <= 0) {
          problems.push(`${name}: limite de ${MAX_ATTACHMENTS} anexos por envio.`);
          continue;
        }
        if (name.length > MAX_ATTACHMENT_NAME_CHARS) {
          problems.push(`${name}: nome acima de ${MAX_ATTACHMENT_NAME_CHARS} caracteres.`);
          continue;
        }
        if (bytes > MAX_ATTACHMENT_BYTES) {
          problems.push(`${name}: acima de 32 KiB por arquivo.`);
          continue;
        }
        if (total + bytes > MAX_ATTACHMENT_TOTAL_BYTES) {
          problems.push(`${name}: o conjunto de anexos passaria de 64 KiB.`);
          continue;
        }
        slots -= 1;
        total += bytes;
        accepted.push({ id: file.id, name, size: bytes, status: "ready" });
        nextContents.set(file.id, file.text ?? "");
      }
      setItems([...previous, ...accepted]);
      setContents((prev) => {
        const next = new Map(prev);
        for (const id of nextContents.keys()) {
          next.set(id, nextContents.get(id) ?? "");
        }
        return next;
      });
      setNotice(problems.length ? problems.join(" ") : null);
    } catch {
      setNotice("Não foi possível ler os arquivos escolhidos. Texto UTF-8 até 32 KiB é aceito; imagens e PDF não.");
    } finally {
      setPicking(false);
    }
  }, [picking]);

  const remove = useCallback((id: string) => {
    setItems((prev) => prev.filter((item) => item.id !== id));
    setContents((prev) => {
      const next = new Map(prev);
      next.delete(id);
      return next;
    });
  }, []);

  const clear = useCallback(() => {
    setItems([]);
    setContents(new Map());
    setNotice(null);
  }, []);

  const dismissNotice = useCallback(() => setNotice(null), []);

  return { items, contents, notice, supported, picking, pick, remove, clear, dismissNotice };
}
