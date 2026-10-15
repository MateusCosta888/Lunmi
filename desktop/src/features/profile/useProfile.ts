/**
 * Perfil local (`desktop/src/features/profile/useProfile.ts`).
 *
 * Persistência canônica é `ui.preferences` (contrato Codador 2026-10-15): `profile.name` e
 * `profile.avatar_data_url`, teto de 40 chars no nome e 40.960 chars / 30.720 bytes na foto
 * PNG/JPEG. Nada de localStorage, URL remota ou caminho de arquivo; a foto nunca vai ao provedor
 * — só o nome entra no contexto de tratamento, independente da memória.
 */
import type { ProfileDto } from "../../shared/types";

export const PROFILE_NAME_MAX = 40;
export const AVATAR_MAX_DATA_CHARS = 40960;
export const AVATAR_EDGE = 128;
/** Teto do arquivo-fonte antes da leitura; fotos reais cabem aqui e são reduzidas para 128px. */
export const AVATAR_SOURCE_CEILING = 5 * 1024 * 1024;
/** Guard de dimensão antes do decode pesado. */
export const AVATAR_MAX_EDGE_SOURCE = 8192;

export const EMPTY_PROFILE: ProfileDto = { name: "", avatar_data_url: "" };

export interface NameCheck {
  value: string;
  error: string | null;
}

export function normalizeProfileName(raw: string): NameCheck {
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(raw)) {
    return { value: raw, error: "O nome não pode conter controles ou quebras de linha." };
  }
  const value = raw.replace(/\s+/g, " ").trim();
  if (value.length > PROFILE_NAME_MAX) {
    return { value, error: `O nome deve ter no máximo ${PROFILE_NAME_MAX} caracteres.` };
  }
  return { value, error: null };
}

export interface AvatarCheck {
  avatarDataUrl: string | null;
  error: string | null;
}

/**
 * Lê, decodifica e reduz a foto para até 128px de lado. Decode que falha é recusado
 * visivelmente — nunca se aceita a data URL bruta no lugar. Saída sempre dentro do teto de
 * 40.960 chars (~30 KiB). Erros nunca ecoam bytes do arquivo.
 */
export async function prepareAvatar(file: File): Promise<AvatarCheck> {
  if (file.type !== "image/png" && file.type !== "image/jpeg") {
    return { avatarDataUrl: null, error: "A foto deve ser PNG ou JPEG." };
  }
  if (file.size > AVATAR_SOURCE_CEILING) {
    return { avatarDataUrl: null, error: "A imagem escolhida é grande demais (máx. 5 MiB)." };
  }
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(new Error("read_failed"));
    reader.readAsDataURL(file);
  }).catch(() => null);
  if (!dataUrl) {
    return { avatarDataUrl: null, error: "Não foi possível ler a imagem escolhida." };
  }
  const reduced = await shrinkToDataUrl(dataUrl, file.type).catch(() => null);
  if (!reduced) {
    return { avatarDataUrl: null, error: "A imagem não pôde ser decodificada como PNG/JPEG válido." };
  }
  if (reduced.length > AVATAR_MAX_DATA_CHARS) {
    return { avatarDataUrl: null, error: "A imagem não coube no limite local de 30 KiB. Escolha outra." };
  }
  return { avatarDataUrl: reduced, error: null };
}

async function shrinkToDataUrl(dataUrl: string, type: string): Promise<string> {
  const image = new Image();
  image.decoding = "async";
  await new Promise<void>((resolve, reject) => {
    image.onload = () => resolve();
    image.onerror = () => reject(new Error("decode_failed"));
    image.src = dataUrl;
    // Um decode que não termina é uma falha visível, não um aceite silencioso.
    setTimeout(() => reject(new Error("decode_timeout")), 2500);
  });
  const width = image.naturalWidth || image.width;
  const height = image.naturalHeight || image.height;
  if (!width || !height) {
    throw new Error("decode_failed");
  }
  if (width > AVATAR_MAX_EDGE_SOURCE || height > AVATAR_MAX_EDGE_SOURCE) {
    throw new Error("too_large");
  }
  const scale = Math.min(1, AVATAR_EDGE / Math.max(width, height));
  const targetWidth = Math.max(1, Math.round(width * scale));
  const targetHeight = Math.max(1, Math.round(height * scale));
  const canvas = document.createElement("canvas");
  canvas.width = targetWidth;
  canvas.height = targetHeight;
  const context = canvas.getContext("2d");
  if (!context) {
    throw new Error("no_canvas");
  }
  context.drawImage(image, 0, 0, targetWidth, targetHeight);
  // PNG preserva transparência; JPEG costuma caber no teto com qualidade moderada.
  const asPng = canvas.toDataURL("image/png");
  if (type === "image/jpeg" || asPng.length > AVATAR_MAX_DATA_CHARS) {
    const asJpeg = canvas.toDataURL("image/jpeg", 0.7);
    if (asJpeg.length <= AVATAR_MAX_DATA_CHARS) {
      return asJpeg;
    }
  }
  return asPng;
}
