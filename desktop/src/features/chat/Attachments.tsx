/**
 * Controle "Anexar" e chips de anexo (`desktop/src/features/chat/Attachments.tsx`).
 *
 * Fica no compositor antes do seletor de modelo (referência 79fad593). Selecionar nunca envia;
 * remover é imediato; o rascunho e os anexos sobrevivem a recusa/navegação e só saem após aceite.
 * O aviso de formatos é honesto: texto/código UTF-8, até 4 arquivos; imagem/PDF/binário recusado
 * com motivo visível.
 */
import React from "react";
import { Icon } from "../../shared/ui";
import type { AttachmentDto } from "../../shared/types";

export interface AttachmentsBarProps {
  items: AttachmentDto[];
  disabled: boolean;
  supported: boolean;
  notice: string | null;
  onPick: () => void;
  onRemove: (id: string) => void;
  onDismissNotice: () => void;
}

export function AttachmentsBar({
  items,
  disabled,
  supported,
  notice,
  onPick,
  onRemove,
  onDismissNotice,
}: AttachmentsBarProps): React.ReactElement {
  const full = items.length >= 4;
  const buttonDisabled = disabled || !supported || full;
  const title = !supported
    ? "Anexos ainda não disponíveis nesta versão"
    : full
      ? "Limite de 4 anexos por envio"
      : "Anexar texto ou código (UTF-8)";
  return (
    <div className="lunmi-attach-zone">
      <button
        type="button"
        className="lunmi-attach-button"
        data-testid="attach-button"
        disabled={buttonDisabled}
        title={title}
        aria-label="Anexar arquivo de texto"
        onClick={onPick}
      >
        <Icon name="attach" size={15} /><span>Anexar</span>
      </button>
      {items.length > 0 && (
        <ul className="lunmi-attach-list" aria-label="Arquivos anexados" data-testid="attachment-list">
          {items.map((item) => (
            <li key={item.id} className="lunmi-attach-chip" data-testid={`attachment-${item.id}`}>
              <Icon name="attach" size={12} />
              <span className="lunmi-attach-name" title={item.name}>{item.name}</span>
              <small>{Math.max(1, Math.round(item.size / 1024))} KiB</small>
              <button
                type="button"
                className="lunmi-attach-remove"
                data-testid={`attachment-remove-${item.id}`}
                aria-label={`Remover ${item.name}`}
                onClick={() => onRemove(item.id)}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
      {notice && (
        <p className="lunmi-attach-notice" role="status" data-testid="attachment-notice">
          {notice}
          <button type="button" className="lunmi-attach-dismiss" onClick={onDismissNotice} aria-label="Fechar aviso de anexos">×</button>
        </p>
      )}
    </div>
  );
}
