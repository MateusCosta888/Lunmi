/**
 * Modal de perfil (`desktop/src/features/profile/ProfileModal.tsx`).
 *
 * Abre na própria janela (sem nova aba), edita só nome e foto. Foco entra no diálogo, Tab fica
 * preso dentro, Escape/Fechar/Cancelar fecham sem salvar, Salvar chama o seam `onSave` (rota
 * canônica `ui.preferences.set` no App). Erros aparecem visíveis e nunca ecoam conteúdo de arquivo.
 */
import React, { useEffect, useRef, useState } from "react";
import { Icon } from "../../shared/ui";
import type { ProfileDto } from "../../shared/types";
import {
  PROFILE_NAME_MAX,
  normalizeProfileName,
  prepareAvatar,
} from "./useProfile";

export interface ProfileModalProps {
  profile: ProfileDto;
  busy: boolean;
  onClose: () => void;
  onSave: (next: ProfileDto) => Promise<string | null>;
}

const FOCUSABLE = 'button, input, textarea, select, [tabindex]:not([tabindex="-1"])';

export function ProfileModal({ profile, busy, onClose, onSave }: ProfileModalProps): React.ReactElement {
  const [name, setName] = useState(profile.name);
  const [avatar, setAvatar] = useState(profile.avatar_data_url || "");
  const [error, setError] = useState<string | null>(null);
  const [photoBusy, setPhotoBusy] = useState(false);
  const locked = busy || photoBusy;
  const dialogRef = useRef<HTMLDivElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const opener = useRef<Element | null>(document.activeElement);

  useEffect(() => {
    nameRef.current?.focus();
    return () => {
      if (opener.current instanceof HTMLElement) {
        opener.current.focus();
      }
    };
  }, []);

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      // Durante leitura da foto ou gravação despachada não há "cancelar" honesto: o diálogo fica aberto.
      if (!locked) {
        onClose();
      }
      return;
    }
    if (event.key !== "Tab") {
      return;
    }
    const nodes = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])
      .filter((node) => !node.hasAttribute("disabled"));
    if (nodes.length === 0) {
      return;
    }
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const pickPhoto = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) {
      return;
    }
    setPhotoBusy(true);
    const result = await prepareAvatar(file);
    setPhotoBusy(false);
    if (result.error || !result.avatarDataUrl) {
      setError(result.error ?? "A foto escolhida não é válida.");
      return;
    }
    setError(null);
    setAvatar(result.avatarDataUrl);
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const checked = normalizeProfileName(name);
    if (checked.error) {
      setError(checked.error);
      return;
    }
    const saveError = await onSave({ name: checked.value, avatar_data_url: avatar });
    if (saveError) {
      setError(saveError);
    }
  };

  return (
    <div className="lunmi-modal-backdrop" onKeyDown={onKeyDown} data-testid="profile-backdrop"
      onPointerDown={(event) => { if (event.target === event.currentTarget && !locked) onClose(); }}>
      <div ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="profile-title"
        className="lunmi-modal lunmi-profile-modal" data-testid="profile-modal">
        <header className="lunmi-modal-head">
          <h2 id="profile-title">Seu perfil</h2>
          <button type="button" className="lunmi-modal-close" data-testid="profile-close" aria-label="Fechar perfil" disabled={locked} onClick={onClose}>×</button>
        </header>

        <form onSubmit={(event) => void submit(event)} className="lunmi-profile-form">
          <div className="lunmi-profile-photo-row">
            <span className="lunmi-profile-avatar" data-testid="profile-avatar" aria-hidden={!avatar}>
              {avatar ? <img src={avatar} alt="" /> : <Icon name="user" size={26} />}
            </span>
            <div className="lunmi-profile-photo-actions">
              <label className="lunmi-modal-file">
                <input type="file" accept="image/png,image/jpeg" data-testid="profile-photo-input" aria-label="Escolher foto" onChange={(event) => void pickPhoto(event)} />
                Escolher foto
              </label>
              {avatar && (
                <button type="button" className="lunmi-link" data-testid="profile-photo-remove" onClick={() => setAvatar("")}>
                  Remover foto
                </button>
              )}
              <small>PNG ou JPEG, quadrada até 128px. A foto fica só neste computador e nunca vai ao provedor.</small>
            </div>
          </div>

          <label className="lunmi-modal-field" htmlFor="profile-name-input">Nome de tratamento</label>
          <input id="profile-name-input" ref={nameRef} type="text" data-testid="profile-name-input"
            value={name} maxLength={PROFILE_NAME_MAX + 12} placeholder="Como a Lunmi deve te chamar"
            onChange={(event) => setName(event.target.value)} />
          <small className="lunmi-modal-hint">Até {PROFILE_NAME_MAX} caracteres. O nome entra no contexto das conversas; é independente da memória.</small>

          {error && <p role="alert" className="lunmi-modal-error" data-testid="profile-error">{error}</p>}

          <div className="lunmi-modal-actions">
            <button type="button" className="lunmi-modal-quiet" data-testid="profile-cancel" disabled={locked} onClick={onClose}>Cancelar</button>
            <button type="submit" className="lunmi-modal-primary" data-testid="profile-save" disabled={busy || photoBusy}>
              {busy ? "Salvando…" : photoBusy ? "Lendo foto…" : "Salvar"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
