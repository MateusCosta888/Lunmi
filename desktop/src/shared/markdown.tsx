/**
 * Renderizador de Markdown sanitizado e seguro (`desktop/src/shared/markdown.tsx`).
 *
 * Regras de segurança (F3):
 * - Nunca usa `dangerouslySetInnerHTML`.
 * - Qualquer tag HTML/script enviada pelo modelo (`<script>`, `<img onerror=...>`, `<iframe>`)
 *   é renderizada exclusivamente como texto React escapado, jamais interpretada como HTML/DOM.
 * - Links só são renderizados como elementos seguros quando o esquema é `https:` ou `http:`,
 *   sem abrir navegação na janela Electron (`onClick` previne navegação direta). Esquemas como
 *   `javascript:`, `data:`, `file:` ou `vbscript:` são neutralizados como texto puro.
 */
import React from "react";

export function isSafeHttpUrl(rawUrl: string): boolean {
  try {
    const parsed = new URL(rawUrl.trim());
    return parsed.protocol === "https:" || parsed.protocol === "http:";
  } catch {
    return false;
  }
}

function renderInlineMarkdown(text: string, keyPrefix: string): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  // Suporta `code`, **bold**, e [label](url)
  const tokenRegex = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\))/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let idx = 0;

  while ((match = tokenRegex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(text.slice(lastIndex, match.index));
    }
    const token = match[0];
    idx += 1;
    const key = `${keyPrefix}-inl-${idx}`;

    if (token.startsWith("`") && token.endsWith("`") && token.length >= 2) {
      nodes.push(
        <code key={key} className="lunmi-inline-code">
          {token.slice(1, -1)}
        </code>
      );
    } else if (token.startsWith("**") && token.endsWith("**") && token.length >= 4) {
      nodes.push(<strong key={key}>{token.slice(2, -2)}</strong>);
    } else if (token.startsWith("[")) {
      const linkMatch = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(token);
      if (linkMatch) {
        const [, label, rawHref] = linkMatch;
        if (isSafeHttpUrl(rawHref)) {
          nodes.push(
            <span
              key={key}
              className="lunmi-safe-link"
              title={rawHref.trim()}
              data-href={rawHref.trim()}
            >
              {label} ↗
            </span>
          );
        } else {
          nodes.push(`${label} (${rawHref})`);
        }
      } else {
        nodes.push(token);
      }
    } else {
      nodes.push(token);
    }
    lastIndex = tokenRegex.lastIndex;
  }

  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }
  return nodes;
}

export function SafeMarkdown({ text }: { text: string }): React.ReactElement {
  const blocks: React.ReactNode[] = [];
  const raw = String(text ?? "").replace(/\r\n/g, "\n");
  const fenceRegex = /```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let blockIdx = 0;

  const pushTextSection = (section: string) => {
    const lines = section.split("\n");
    let listItems: string[] = [];
    let ordered = false;
    let listStart = 1;

    const flushList = () => {
      if (listItems.length > 0) {
        blockIdx += 1;
        const bKey = `ul-${blockIdx}`;
        blocks.push(React.createElement(ordered ? "ol" : "ul", {
          key: bKey, className: "lunmi-md-list", start: ordered ? listStart : undefined,
        }, listItems.map((item, i) => <li key={`${bKey}-li-${i}`}>{renderInlineMarkdown(item, `${bKey}-li-${i}`)}</li>)));
        listItems = [];
      }
    };

    for (const line of lines) {
      const trimmed = line.trim();
      const item = /^(?:([-*])\s+|(\d+)[.)]\s+)(.+)$/.exec(trimmed);
      if (item) {
        const nextOrdered = Boolean(item[2]);
        if (listItems.length && nextOrdered !== ordered) flushList();
        if (!listItems.length) listStart = Number(item[2] || 1);
        ordered = nextOrdered;
        listItems.push(item[3]);
        continue;
      }
      flushList();
      if (trimmed.length === 0) {
        continue;
      }
      blockIdx += 1;
      const pKey = `p-${blockIdx}`;
      const heading = /^(#{1,6})\s+(.+)$/.exec(trimmed);
      if (heading) {
        blocks.push(React.createElement(`h${Math.min(6, heading[1].length + 2)}`, {key:pKey, className:"lunmi-md-heading"}, renderInlineMarkdown(heading[2], pKey)));
        continue;
      }
      blocks.push(
        <p key={pKey} className="lunmi-md-p">
          {renderInlineMarkdown(line, pKey)}
        </p>
      );
    }
    flushList();
  };

  while ((match = fenceRegex.exec(raw)) !== null) {
    if (match.index > lastIndex) {
      pushTextSection(raw.slice(lastIndex, match.index));
    }
    blockIdx += 1;
    const lang = match[1] || "";
    const codeContent = match[2];
    blocks.push(
      <pre key={`code-${blockIdx}`} className="lunmi-code-block" data-lang={lang}>
        <code>{codeContent}</code>
      </pre>
    );
    lastIndex = fenceRegex.lastIndex;
  }

  if (lastIndex < raw.length) {
    pushTextSection(raw.slice(lastIndex));
  }

  return <div className="lunmi-markdown">{blocks}</div>;
}
