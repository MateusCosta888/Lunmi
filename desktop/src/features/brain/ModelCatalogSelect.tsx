/**
 * Seletor de catálogo pesquisável (`desktop/src/features/brain/ModelCatalogSelect.tsx`).
 *
 * Escolha SEMPRE por clique no catálogo real do provider consultado: não existe entrada de ID
 * nem texto livre de modelo (decisão do usuário). O campo de busca apenas filtra as opções
 * carregadas — nenhuma requisição por tecla. Estados honestos: carregando, vazio, erro e "cache"
 * rotulado pelo listing do backend. O modelo atualmente aplicado que não está no catálogo
 * aparece como "atual indisponível", nunca como opção falsamente válida; o mesmo vale depois de
 * trocar chave/endpoint no mesmo provider (o selo "não validado" assume o lugar).
 * A opção "(padrão do provider)" só existe quando o provider tem capacidade real de padrão
 * (CLI); para APIs, modelo vazio não é catálogo e não infere nada.
 * Teclado: Escape fecha e devolve o foco ao gatilho; setas/Home/End navegam opções; Enter
 * escolhe; sair do foco do popover fecha.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import type { ModelInfoDto } from "../../shared/types";

export type CatalogState = "idle" | "loading" | "ready" | "empty" | "error";

export interface ModelCatalogSelectProps {
  label: string;
  testId: string;
  value: string;
  models: ModelInfoDto[];
  state: CatalogState;
  detail?: string;
  cached?: boolean;
  disabled?: boolean;
  /** Só providers com capacidade real de padrão (CLI) oferecem a opção vazia. */
  allowDefault?: boolean;
  /** Valor selecionado cujo catálogo não vale para a credencial/endpoint atual. */
  unverified?: boolean;
  /** Modelo aplicado que não está no catálogo; exibido como indisponível. */
  currentUnavailable?: string;
  onSelect: (id: string) => void;
  onRefresh: () => void;
  /** Chamado quando o seletor abre ainda sem catálogo para esta identidade. */
  onOpenEmpty?: () => void;
}

export function ModelCatalogSelect({
  label, testId, value, models, state, detail, cached, disabled,
  allowDefault, unverified, currentUnavailable, onSelect, onRefresh, onOpenEmpty,
}: ModelCatalogSelectProps): React.ReactElement {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  const close = () => {
    setOpen(false);
    triggerRef.current?.focus();
  };

  const optionSelector = () => `[role="option"]`;

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    if (!open) return;
    const items = Array.from(rootRef.current?.querySelectorAll<HTMLButtonElement>(optionSelector()) ?? []);
    if (items.length === 0) return;
    const active = document.activeElement as HTMLElement | null;
    const index = active ? items.indexOf(active as HTMLButtonElement) : -1;
    const move = (next: number) => {
      event.preventDefault();
      items[Math.max(0, Math.min(items.length - 1, next))]?.focus();
    };
    if (event.key === "ArrowDown") { index < 0 ? items[0]?.focus() : move(index + 1); }
    else if (event.key === "ArrowUp") { event.preventDefault(); if (index <= 0) searchRef.current?.focus(); else move(index - 1); }
    else if (event.key === "Home") move(0);
    else if (event.key === "End") move(items.length - 1);
  };

  // Fechar quando o foco sai do popover (onBlur borbulha como focusout).
  const onBlurOut = (event: React.FocusEvent) => {
    if (open && !rootRef.current?.contains(event.relatedTarget as Node | null)) setOpen(false);
  };

  const visible = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (!needle) return models;
    return models.filter(m => m.id.toLowerCase().includes(needle) || (m.label || "").toLowerCase().includes(needle));
  }, [filter, models]);

  const selected = models.find(m => m.id === value);
  const shown = selected ? (selected.label || selected.id)
    : value ? value
    : "escolha um modelo do catálogo";

  const toggle = () => {
    if (disabled) return;
    const next = !open;
    setOpen(next);
    setFilter("");
    if (next && state === "idle") onOpenEmpty?.();
  };

  return (
    <div className="lunmi-brain-catalog" ref={rootRef} onKeyDown={onKeyDown} onBlur={onBlurOut}>
      <span className="lunmi-brain-label">{label}</span>
      <div className="lunmi-brain-catalog-row">
        <button ref={triggerRef} type="button" data-testid={testId} className="lunmi-brain-catalog-trigger"
          aria-haspopup="listbox" aria-expanded={open} disabled={disabled} onClick={toggle}>
          <span className="lunmi-brain-catalog-value" title={shown}>{shown}</span>
          {unverified && value ? <span className="lunmi-brain-badge" data-testid={`${testId}-unverified`}>lista por atualizar</span> : null}
          {cached && state === "ready" && !unverified ? <span className="lunmi-brain-badge">cache</span> : null}
        </button>
        <button type="button" data-testid={`${testId}-refresh`} className="lunmi-action-button lunmi-action-quiet"
          disabled={disabled || state === "loading"} onClick={onRefresh}>
          Atualizar catálogo
        </button>
      </div>
      {currentUnavailable && !selected ? (
        <p className="lunmi-brain-warn" data-testid={`${testId}-unavailable`}>
          Modelo atual “{currentUnavailable}” não está no catálogo deste provider/endpoint.
        </p>
      ) : null}
      {open ? (
        <div className="lunmi-brain-catalog-pop" data-testid={`${testId}-popover`}>
          <input ref={searchRef} type="search" data-testid={`${testId}-search`} className="lunmi-brain-catalog-search"
            aria-label={`Filtrar ${label}`} placeholder="Filtrar modelos…" value={filter}
            onChange={event => setFilter(event.target.value)} autoFocus />
          {state === "loading" ? <p className="lunmi-brain-muted" data-testid={`${testId}-loading`}>Consultando catálogo…</p> : null}
          {state === "error" ? (
            <p role="alert" className="lunmi-brain-error-text" data-testid={`${testId}-error`}>
              {detail || "O catálogo não está disponível."}
            </p>
          ) : null}
          {state === "empty" ? (
            <p className="lunmi-brain-muted" data-testid={`${testId}-empty`}>
              {detail || "Este provider não devolveu modelos. Atualize o catálogo ou confira chave e endpoint."}
            </p>
          ) : null}
          {state === "ready" && visible.length === 0 && !allowDefault ? (
            <p className="lunmi-brain-muted" data-testid={`${testId}-no-match`}>Nenhum modelo corresponde ao filtro.</p>
          ) : null}
          {state === "ready" && (visible.length > 0 || allowDefault) ? (
            <ul className="lunmi-brain-catalog-list" role="listbox" aria-label={label} data-testid={`${testId}-list`}>
              {allowDefault ? (
                <li>
                  <button type="button" role="option" aria-selected={value === ""} data-testid={`${testId}-option-default`}
                    className="lunmi-brain-catalog-option" onClick={() => { onSelect(""); setOpen(false); triggerRef.current?.focus(); }}>
                    (padrão do provider)
                  </button>
                </li>
              ) : null}
              {visible.map(m => (
                <li key={m.id}>
                  <button type="button" role="option" aria-selected={value === m.id} data-testid={`${testId}-option-${m.id}`}
                    className="lunmi-brain-catalog-option" onClick={() => { onSelect(m.id); setOpen(false); triggerRef.current?.focus(); }}>
                    <span>{m.label || m.id}</span>
                    {m.default ? <span className="lunmi-brain-badge">padrão</span> : null}
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
