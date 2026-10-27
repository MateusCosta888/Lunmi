/**
 * Ícone de provedor (`desktop/src/features/brain/ProviderIcon.tsx`).
 *
 * As marcas são as oficiais do pacote LobeHub lobe-icons (MIT), vendadas localmente em
 * `assets/providers/providerMarks.ts` com commit fixo e SHA-256 por ficheiro — nada é carregado
 * de remoto na aplicação e nenhum traço foi desenhado à mão. Como os SVG de origem usam
 * `fill="currentColor"`, o glifo é renderizado inline (não `<img>`) para herdar a cor dos três
 * temas; o wrapper `<svg>` é nosso (viewBox 0 0 24 24).
 *
 * Providers sem marca própria — `custom` (OpenAI Compatible) e qualquer id que o backend venha a
 * acrescentar — recebem o glifo genérico do Lucide (ISC, dependência já instalada), nunca a marca
 * de outra empresa. O ícone é decorativo: o nome acessível do controlo vem do texto da pílula.
 */
import React from "react";
import { Globe } from "lucide-react";
import { PROVIDER_MARKS } from "../../assets/providers/providerMarks";

export interface ProviderIconProps {
  providerId: string;
  size?: number;
  /** TestId único por seletor (principal e AppAgent mostram os mesmos provedores). */
  testId?: string;
}

export function ProviderIcon({ providerId, size = 18, testId }: ProviderIconProps): React.ReactElement {
  const mark = PROVIDER_MARKS[providerId];
  const id = testId ?? `provider-icon-${providerId}`;
  if (!mark) {
    // Sem marca licenciada para este id: glifo genérico, sem fingir logótipo.
    return <Globe size={size} strokeWidth={1.65} aria-hidden="true" focusable="false" data-testid={id} data-mark="generic" />;
  }
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="currentColor" aria-hidden="true" focusable="false"
      data-testid={id} data-mark={mark.title}>
      {mark.paths.map((attrs, index) => <path key={index} {...attrs} />)}
    </svg>
  );
}
