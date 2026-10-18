/**
 * Kinetic Text — Magic UI, adapted for the Lunmi header (`desktop/src/shared/KineticText.tsx`).
 *
 * Origin: the official Magic UI registry item `@magicui/kinetic-text`
 * (https://magicui.design/docs/components/kinetic-text). The upstream file ships Tailwind
 * arbitrary variants and a `cn` helper; this project has no Tailwind, so the same effect is
 * expressed as authored CSS in `KineticText.css` (see `desktop/THIRD-PARTY-MOTION.md`).
 * License: MIT (© Magic UI); the original notice is kept in
 * `desktop/licenses/magicui-KineticText.txt`.
 *
 * Effect: every character is a span. The base weight is 300; on hover the character reaches 900
 * with a text stroke, its immediate neighbours step to 600 and the characters two away to 400,
 * over a 0.4 s transition. The letters are decorative (`aria-hidden`); a visually hidden label
 * keeps the real word readable, so the mark is never read twice nor lost when the effect is off.
 */
import React from "react";
import "./KineticText.css";

type KineticTextTag = "h1" | "h2" | "h3" | "h4" | "h5" | "h6" | "p" | "span";

export interface KineticTextProps extends React.HTMLAttributes<HTMLElement> {
  text: string;
  as?: KineticTextTag;
}

export function KineticText({ text, as: Tag = "span", className = "", style, ...rest }: KineticTextProps) {
  const mergedStyle = {
    "--hover-padding": "calc(1em / 12)",
    "--text-stroke-width": "calc(1em * 125 / 6000)",
    ...(style as React.CSSProperties | undefined)
  } as React.CSSProperties;

  return (
    <Tag {...rest} data-kinetic-engine="magicui-kinetic-text" className={["lunmi-kinetic", className].filter(Boolean).join(" ")} style={mergedStyle}>
      {text.split("").map((letter, index) => (
        <span key={index} aria-hidden="true" className="lunmi-kinetic-letter">
          {letter === " " ? "\u00A0" : letter}
        </span>
      ))}
      <span className="lunmi-kinetic-sr">{text}</span>
    </Tag>
  );
}
