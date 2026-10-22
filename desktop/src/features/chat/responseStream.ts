/**
 * Compatibilidade de import: a implementação vive em `desktop/src/shared/responseStream.ts` para
 * ser reutilizada pela projeção do processo principal (BG2) sem um segundo parser.
 */
export { ResponseStream, upsertMessage } from "../../shared/responseStream";
