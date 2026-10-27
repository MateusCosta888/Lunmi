/**
 * Paridade do contrato Spotify I0: TypeScript ↔ golden compartilhado.
 *
 * O par Python é `tests/test_spotify_contract.py`. Nenhum teste aqui arranca Spotify,
 * rede, Electron, instalação ou ficheiros do utilizador.
 */
import { describe, expect, it } from "vitest";
import golden from "../src/shared/ipc_golden_v1.json";
import { COMMAND_TYPES, EVENT_TYPES } from "../src/shared/protocol";
import {
  CONSENT_ID_PATTERN,
  JOB_ID_PATTERN,
  PLAN_HASH_PATTERN,
  SPOTIFY_ACTIONS,
  SPOTIFY_APP_CHANNELS,
  SPOTIFY_ARCHITECTURES,
  SPOTIFY_CANCEL_REASONS,
  SPOTIFY_CHANGED_KEYS,
  SPOTIFY_CLI_STATES,
  SPOTIFY_COMMAND_PAYLOADS,
  SPOTIFY_COMMANDS,
  SPOTIFY_CONNECTION_STATES,
  SPOTIFY_CONSENT_KEYS,
  SPOTIFY_CONSENT_TTL_SECONDS,
  SPOTIFY_DEVICES,
  SPOTIFY_DETECTION_KEYS,
  SPOTIFY_ERROR_CODES,
  SPOTIFY_ERROR_KEYS,
  SPOTIFY_EVENT,
  SPOTIFY_JOB_KEYS,
  SPOTIFY_JOB_STAGES,
  SPOTIFY_JOB_STATUSES,
  SPOTIFY_LINK_STATES,
  SPOTIFY_OPEN_TARGETS,
  SPOTIFY_PINNED_CLI,
  SPOTIFY_PINNED_SPOTIFY,
  SPOTIFY_PLAN_CHANGES_KEYS,
  SPOTIFY_PLAN_KEYS,
  SPOTIFY_PLAN_REQUIRES_KEYS,
  SPOTIFY_RESULT_KEYS,
  SPOTIFY_SETUP_STATES,
  SPOTIFY_STATE_KEYS,
  SPOTIFY_VERSION_FIT,
  SESSION_ID_PATTERN,
} from "../src/shared/spotify";
import type { SpotifyChangedDto, SpotifyPlanDto, SpotifyStateDto } from "../src/shared/spotify";

const spotify = golden.spotify;

describe("contrato Spotify I0", () => {
  it("expõe os seis comandos e o evento no contrato v1", () => {
    expect([...SPOTIFY_COMMANDS]).toEqual(spotify.commands);
    for (const command of SPOTIFY_COMMANDS) {
      expect([...COMMAND_TYPES]).toContain(command);
      expect(golden.commands).toContain(command);
    }
    expect(SPOTIFY_EVENT).toBe(spotify.event);
    expect(SPOTIFY_EVENT).toBe("spotify.changed");
    expect([...EVENT_TYPES]).toContain(SPOTIFY_EVENT);
    expect(golden.events).toContain(SPOTIFY_EVENT);
    expect(spotify.capability).toBe("spotify");
    expect(golden.default_capabilities.spotify).toBe(false);
  });

  it("mantém payloads e resultados fechados por comando", () => {
    for (const command of SPOTIFY_COMMANDS) {
      expect([...SPOTIFY_COMMAND_PAYLOADS[command]].sort()).toEqual(
        [...spotify.command_payloads[command]].sort()
      );
      expect([...SPOTIFY_RESULT_KEYS[command]].sort()).toEqual(
        [...spotify.result_keys[command]].sort()
      );
    }
  });

  it("espelha estados, ações e estágios do golden", () => {
    expect([...SPOTIFY_SETUP_STATES]).toEqual(spotify.states.setup);
    expect([...SPOTIFY_CONNECTION_STATES]).toEqual(spotify.states.connection);
    expect([...SPOTIFY_DEVICES]).toEqual(spotify.states.device);
    expect([...SPOTIFY_ACTIONS]).toEqual(spotify.actions);
    expect([...SPOTIFY_JOB_STAGES]).toEqual(spotify.stages);
    expect([...SPOTIFY_JOB_STATUSES]).toEqual(spotify.job_statuses);
    expect([...SPOTIFY_CANCEL_REASONS]).toEqual(spotify.cancel_reasons);
    expect([...SPOTIFY_OPEN_TARGETS]).toEqual(spotify.open_targets);
    expect([...SPOTIFY_APP_CHANNELS]).toEqual(spotify.detection.app);
    expect([...SPOTIFY_VERSION_FIT]).toEqual(spotify.detection.version);
    expect([...SPOTIFY_ARCHITECTURES]).toEqual(spotify.detection.architecture);
    expect([...SPOTIFY_CLI_STATES]).toEqual(spotify.detection.cli);
    expect([...SPOTIFY_LINK_STATES]).toEqual(spotify.detection.link);
  });

  it("espelha códigos de erro, chaves e padrões do golden", () => {
    expect([...SPOTIFY_ERROR_CODES].sort()).toEqual([...spotify.error_codes].sort());
    expect([...SPOTIFY_STATE_KEYS].sort()).toEqual([...spotify.dto_keys.state].sort());
    expect([...SPOTIFY_DETECTION_KEYS].sort()).toEqual([...spotify.dto_keys.detection].sort());
    expect([...SPOTIFY_JOB_KEYS].sort()).toEqual([...spotify.dto_keys.job].sort());
    expect([...SPOTIFY_ERROR_KEYS].sort()).toEqual([...spotify.dto_keys.error].sort());
    expect([...SPOTIFY_PLAN_KEYS].sort()).toEqual([...spotify.dto_keys.plan].sort());
    expect([...SPOTIFY_PLAN_CHANGES_KEYS].sort()).toEqual(
      [...spotify.dto_keys.plan_changes].sort()
    );
    expect([...SPOTIFY_PLAN_REQUIRES_KEYS].sort()).toEqual(
      [...spotify.dto_keys.plan_requires].sort()
    );
    expect([...SPOTIFY_CONSENT_KEYS].sort()).toEqual([...spotify.dto_keys.consent].sort());
    expect([...SPOTIFY_CHANGED_KEYS].sort()).toEqual([...spotify.dto_keys.changed].sort());
    expect(SESSION_ID_PATTERN.source).toBe(spotify.patterns.session_id);
    expect(CONSENT_ID_PATTERN.source).toBe(spotify.patterns.consent_id);
    expect(JOB_ID_PATTERN.source).toBe(spotify.patterns.job_id);
    expect(PLAN_HASH_PATTERN.source).toBe(spotify.patterns.plan_hash);
    expect(SPOTIFY_PINNED_SPOTIFY).toBe(spotify.pins.spotify);
    expect(SPOTIFY_PINNED_CLI).toBe(spotify.pins.cli);
    expect(SPOTIFY_CONSENT_TTL_SECONDS).toBe(spotify.pins.consent_ttl_seconds);
  });

  it("aceita um DTO de estado e plano exatamente com as chaves publicadas", () => {
    const state: SpotifyStateDto = {
      setup: "configured",
      connection: "connected",
      device: "local",
      revision: 4,
      epoch: 2,
      job: { id: `spj-${"0".repeat(16)}`, action: "install", stage: "verify", status: "done",
        error_code: null },
      detection: {
        app: "win32",
        version: "supported",
        architecture: "x64",
        cli: "pinned",
        link: "present",
        app_running: true,
      },
      error: null,
    };
    const changed: SpotifyChangedDto = { session_id: `sess-${"0".repeat(16)}`, spotify: state };
    expect(Object.keys(changed).sort()).toEqual([...spotify.dto_keys.changed].sort());
    expect(SESSION_ID_PATTERN.test(changed.session_id)).toBe(true);
    expect(Object.keys(changed.spotify).sort()).toEqual([...spotify.dto_keys.state].sort());
    expect(Object.keys(changed.spotify.detection).sort()).toEqual(
      [...spotify.dto_keys.detection].sort()
    );

    const plan: SpotifyPlanDto = {
      action: "install",
      plan_hash: "a".repeat(64),
      steps: ["detect", "obtain", "backup", "apply", "pair", "verify"],
      changes: { adds_link: true, replaces_files: 0, backup_bytes: 239104134 },
      requires: { close_spotify: true, network: true, elevation: false },
      expires_in: 600,
      version_pin: "1.3.5.3058",
      cli_version: "2.46.2",
    };
    expect(Object.keys(plan).sort()).toEqual([...spotify.dto_keys.plan].sort());
    expect(plan.requires.elevation).toBe(false);
    expect(JOB_ID_PATTERN.test(`spj-${"0".repeat(16)}`)).toBe(true);
    expect(SPOTIFY_OPEN_TARGETS).toEqual(["app", "download"]);
    expect(CONSENT_ID_PATTERN.test(`cns-${"0".repeat(32)}`)).toBe(true);
  });
});
