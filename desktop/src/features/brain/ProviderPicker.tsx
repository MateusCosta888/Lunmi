/**
 * Seletor de provedores em pílulas (`desktop/src/features/brain/ProviderPicker.tsx`).
 *
 * Substitui o dropdown por uma linha de botões-pílula com a marca real de cada provedor, no
 * padrão de "toggle group" (linha de pílulas, seleção única) documentado em 21st.dev e adaptado
 * aos tokens da Lunmi — nenhuma dependência copiada, implementação própria. Os grupos separam
 * claramente API em nuvem, endpoint local e CLI local a partir dos valores REAIS do DTO
 * (`kind`: `model_api`/`agent_runtime`; `location`: `cloud_api`/`local_endpoint`/`local_cli`).
 *
 * Regras de honestidade:
 * - um provedor indisponível (`available === false`, campos opcionais que a Root vai expor)
 *   continua visível, fica desativado e diz o motivo; nunca é escondido nem fingido;
 * - `installed === true` pode rotular "instalado"; não se infere instalação de nada;
 * - a escolha não dispara nenhum comando: aplicar continua a ser o botão próprio do painel
 *   (contrato `brain.set` inalterado);
 * - teclado: as pílulas são botões normais (Tab + Enter/Space), com `aria-pressed` a dizer a
 *   seleção — sem `role="radio"` fingido que exigiria navegação por setas.
 */
import React from "react";
import type { BrainProviderInfoDto } from "../../shared/types";
import { ProviderIcon } from "./ProviderIcon";

export interface ProviderGroup {
  title: string;
  hint: string;
  items: BrainProviderInfoDto[];
}

/** Agrupação pelos valores canónicos do backend (não pelos de fixtures antigas). */
export function groupProviders(providers: BrainProviderInfoDto[]): ProviderGroup[] {
  const cloud = providers.filter(p => p.kind === "model_api" && p.location === "cloud_api");
  const local = providers.filter(p => p.kind === "model_api" && p.location === "local_endpoint");
  const cli = providers.filter(p => p.kind === "agent_runtime" || p.location === "local_cli");
  const taken = new Set<BrainProviderInfoDto>([...cloud, ...local, ...cli]);
  const other = providers.filter(p => !taken.has(p));
  const groups: ProviderGroup[] = [
    { title: "API em nuvem", hint: "A resposta é gerada nos servidores do provedor, com a tua chave de API.", items: cloud },
    { title: "Endpoint local", hint: "Serviço num endereço configurado por ti.", items: local },
    { title: "CLI local", hint: "Comando nesta máquina; usa a autenticação da própria CLI.", items: cli },
  ];
  if (other.length > 0) groups.push({ title: "Outros", hint: "Sem classificação de tipo ou localização no backend.", items: other });
  return groups.filter(group => group.items.length > 0);
}

export interface ProviderPickerProps {
  /** Rótulo acessível do grupo (ex.: "Provedor" / "Provider do AppAgent"). */
  label: string;
  /** Prefixo de testId preservado do dropdown: pílulas usam `${testId}-btn-<id>`. */
  testId: string;
  value: string;
  providers: BrainProviderInfoDto[];
  disabled?: boolean;
  /** AppAgent: pílula extra que significa "herda o provedor principal" (valor vazio). */
  allowInherit?: boolean;
  onSelect: (id: string) => void;
}

export function ProviderPicker({
  label, testId, value, providers, disabled = false, allowInherit = false, onSelect,
}: ProviderPickerProps): React.ReactElement {
  const groups = groupProviders(providers);
  const pill = (id: string, title: string, options: { flag?: string; installed?: boolean; unavailable?: string }) => {
    const selected = value === id;
    const unavailable = options.unavailable !== undefined;
    return (
      <button key={id || "inherit"} type="button" data-testid={`${testId}-btn-${id || "inherit"}`}
        className="lunmi-brain-provider-btn" aria-pressed={selected}
        disabled={disabled || unavailable} title={unavailable ? options.unavailable : title}
        onClick={() => onSelect(id)}>
        {id ? <ProviderIcon providerId={id} size={18} testId={`${testId}-icon-${id}`} /> : null}
        <span className="lunmi-brain-provider-name">{title}</span>
        {options.installed ? <span className="lunmi-brain-provider-flag" data-testid={`${testId}-installed-${id}`}>instalado</span> : null}
        {!options.installed && options.flag ? <span className="lunmi-brain-provider-flag">{options.flag}</span> : null}
        {unavailable ? <span className="lunmi-brain-provider-flag lunmi-brain-provider-flag-off" data-testid={`${testId}-off-${id}`}>indisponível</span> : null}
      </button>
    );
  };
  return (
    <div className="lunmi-brain-providers" data-testid={testId} role="group" aria-label={label}>
      {allowInherit ? (
        <div className="lunmi-brain-provider-group" data-testid={`${testId}-group-inherit`}>
          <span className="lunmi-brain-provider-group-title">Herdar</span>
          <div className="lunmi-brain-provider-row">
            {pill("", "(mesmo do principal)", {})}
          </div>
        </div>
      ) : null}
      {groups.map(group => {
        const reasons = group.items.filter(item => item.available === false)
          .map(item => `${item.label}: ${item.unavailable_reason?.trim() || "não disponível nesta máquina."}`);
        return (
          <div className="lunmi-brain-provider-group" key={group.title} data-testid={`${testId}-group-${group.title}`}>
            <span className="lunmi-brain-provider-group-title">{group.title}</span>
            <span className="lunmi-brain-provider-group-hint">{group.hint}</span>
            <div className="lunmi-brain-provider-row">
              {group.items.map(item => pill(item.id, item.label, {
                flag: item.requires_api_key ? "chave" : undefined,
                installed: item.installed === true,
                unavailable: item.available === false ? (item.unavailable_reason?.trim() || "") : undefined,
              }))}
            </div>
            {reasons.length > 0 ? (
              <p className="lunmi-brain-provider-reasons" data-testid={`${testId}-reasons-${group.title}`}>{reasons.join(" · ")}</p>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
