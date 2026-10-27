/*
 * Offline tests for the real kit extension. No network: an in-memory socket
 * double speaks protocol v2 with its own node:crypto HMAC. The Spicetify object
 * is the test fixture only; the extension code is the shipped one.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHmac } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const extensionPath = path.join(here, '../../app/integrations/spotify_assets/lunmi-spotify.js');
const fakePath = path.join(here, 'fake-spicetify.js');

globalThis.LUNMI_SPOTIFY_KIT_NO_AUTOSTART = true;
vm.runInThisContext(readFileSync(fakePath, 'utf8'), { filename: 'fake-spicetify.js' });
vm.runInThisContext(readFileSync(extensionPath, 'utf8'), {
  filename: 'lunmi-spotify.js'
});

const S1 = globalThis.LunmiSpotifyKit;
const Fake = globalThis.FakeSpicetify;
const SECRET = '11'.repeat(32);
const SERVER_NONCE = 'aa'.repeat(32);
const SESSION = 'bb'.repeat(16);
const URL = 'ws://127.0.0.1:45871';

function mac(role, serverNonce, clientNonce, epoch, mode, scope, support) {
  return createHmac('sha256', Buffer.from(SECRET, 'hex'))
    .update(S1.macInput(role, serverNonce, clientNonce, epoch, mode, scope, support))
    .digest('hex');
}

class FakeSocket {
  constructor() {
    this.sent = [];
    this.listeners = new Map();
    this.closed = false;
    this.closeCode = null;
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) {
      this.listeners.set(type, []);
    }
    this.listeners.get(type).push(handler);
  }

  emit(type, event) {
    for (const handler of this.listeners.get(type) || []) {
      handler(event);
    }
  }

  send(raw) {
    this.sent.push(JSON.parse(raw));
  }

  close(code, reason) {
    if (this.closed) {
      return;
    }
    this.closed = true;
    this.closeCode = code || 1000;
    this.emit('close', { code: this.closeCode, reason: reason || '' });
  }

  deliver(payload) {
    if (this.closed) {
      return;
    }
    const data = typeof payload === 'string' ? payload : JSON.stringify(payload);
    this.emit('message', { data });
  }
}

function supportMap(text) {
  const map = {};
  for (const action of S1.ACTIONS) {
    map[action] = false;
  }
  for (const name of text.split(',').filter(Boolean)) {
    map[name] = true;
  }
  return map;
}

function createServer(options = {}) {
  const mode = options.mode || 'read_only';
  let scope = options.deviceScope || 'local';
  const server = {
    socket: null,
    sockets: [],
    results: [],
    errors: [],
    authFrame: null,
    authVerified: false,
    autoReady: options.autoReady !== false,
    tamperServerProof: options.tamperServerProof === true,
    readyMode: options.readyMode || mode,
    refuseCount:
      typeof options.refuseCount === 'number'
        ? options.refuseCount
        : options.refuseFirst === true
          ? 1
          : 0,
    refused: 0,
    rejectCode: options.rejectCode || null,
    deliveries: 0
  };

  // The signed scope of the next handshake, not a rewrite of a live session.
  server.setScope = (value) => {
    scope = value;
  };

  server.factory = (url) => {
    const socket = new FakeSocket();
    server.socket = socket;
    server.sockets.push(socket);
    const rawSend = socket.send.bind(socket);
    socket.send = (raw) => {
      rawSend(raw);
      const msg = socket.sent[socket.sent.length - 1];
      if (msg.type === 'auth') {
        server.authFrame = msg;
        const expected = mac(
          'client',
          SERVER_NONCE,
          msg.client_nonce,
          1,
          msg.mode,
          msg.device_scope,
          msg.support
        );
        server.authVerified = expected === msg.client_proof;
        if (server.autoReady) {
          const proof = server.tamperServerProof
            ? 'ff'.repeat(32)
            : mac(
                'server',
                SERVER_NONCE,
                msg.client_nonce,
                1,
                msg.mode,
                msg.device_scope,
                msg.support
              );
          socket.deliver({
            v: 2,
            type: 'ready',
            id: msg.id,
            server_proof: proof,
            session: SESSION,
            epoch: 1,
            mode: server.readyMode,
            device_scope: scope,
            capabilities: S1.capabilitiesFor(supportMap(msg.support), mode, scope),
            origin_filter: 'kit'
          });
        }
      } else if (msg.type === 'result') {
        server.results.push(msg);
      } else if (msg.type === 'error') {
        server.errors.push(msg);
      }
    };

    queueMicrotask(() => {
      if (server.rejectCode) {
        socket.deliver({
          v: 2,
          type: 'error',
          id: '',
          code: server.rejectCode
        });
        socket.close(1008, server.rejectCode);
        return;
      }
      if (server.refused < server.refuseCount) {
        server.refused += 1;
        socket.close(1006, 'refused');
        return;
      }
      socket.emit('open', {});
      socket.deliver({
        v: 2,
        type: 'hello',
        server_nonce: SERVER_NONCE,
        epoch: 1,
        auth_deadline_ms: 2000,
        mode
      });
    });
    return socket;
  };

  server.command = (id, action, args = {}, deadline = 1000) => {
    server.deliveries += 1;
    server.socket.deliver({
      v: 2,
      type: 'command',
      id,
      session: SESSION,
      action: action || 'get_player_state',
      args,
      deadline_ms: deadline
    });
  };

  return server;
}

async function waitFor(predicate, timeout = 2000) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    if (predicate()) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error('condition not met in time');
}

function newClient(server, options = {}) {
  globalThis.Spicetify = options.fake || Fake.create(options.fakeOptions);
  if (options.deviceScope === null) {
    delete globalThis.LUNMI_SPOTIFY_KIT_DEVICE_SCOPE;
  } else {
    globalThis.LUNMI_SPOTIFY_KIT_DEVICE_SCOPE = options.deviceScope || 'local';
  }
  return S1.createClient({
    config: { url: URL, secretHex: SECRET },
    createSocket: server.factory,
    autoReconnect: options.autoReconnect === true,
    spicetify: globalThis.Spicetify,
    ...options
  });
}

test('handshake verifies the server and one read is verified with zero writes', async () => {
  const server = createServer();
  const client = newClient(server);
  const info = await client.connect();

  assert.equal(client.status, 'authenticated');
  assert.equal(server.authVerified, true);
  assert.equal(info.session, SESSION);
  assert.equal(info.mode, 'read_only');
  assert.equal(info.device_scope, 'local');
  assert.equal(info.capabilities.play, 'blocked_read_only');

  server.command('cmd-1', 'get_player_state');
  await waitFor(() => server.results.length === 1);
  const result = server.results[0];
  assert.equal(result.status, 'SUCCESS_VERIFIED');
  assert.equal(result.data.track_uri, 'spotify:track:qa-s1-0001');
  assert.equal(result.data.title, 'QA Kit Track');
  assert.equal(result.data.artist, 'QA Fixture Artist');
  assert.equal(result.data.album, 'QA Fixture Album');
  assert.equal(result.data.is_playing, true);
  assert.equal(result.data.volume, 0.42);
  assert.deepEqual(result.evidence, { kind: 'player_state', observed: true });
  assert.equal(globalThis.Spicetify.writes(), 0);
  client.dispose();
});

test('cold helpers delay the socket until GraphQL and Platform are ready', async () => {
  const fake = Fake.create();
  const graphql = fake.GraphQL;
  const history = fake.Platform.History;
  const playback = fake.Platform.PlaybackAPI;
  const connect = fake.Platform.ConnectAPI;
  delete fake.GraphQL;
  delete fake.Platform.History;
  delete fake.Platform.PlaybackAPI;
  delete fake.Platform.ConnectAPI;

  const server = createServer();
  const client = newClient(server, { fake, deviceScope: null });
  const pending = client.connect();
  await new Promise((resolve) => setTimeout(resolve, 120));
  assert.equal(server.sockets.length, 0);
  assert.equal(fake.calls.graphql, 0);
  assert.equal(fake.writes(), 0);

  fake.GraphQL = graphql;
  fake.Platform.History = history;
  fake.Platform.PlaybackAPI = playback;
  fake.Platform.ConnectAPI = connect;
  await pending;

  assert.equal(server.sockets.length, 1);
  assert.equal(server.authFrame.support.split(',').includes('search_tracks'), true);
  assert.equal(client.capabilities.search_tracks, 'available');
  assert.equal(fake.calls.graphql, 0);
  assert.equal(fake.writes(), 0);
  client.dispose();
});

test('warm helpers open the socket without waiting for the warm-up bound', async () => {
  const server = createServer();
  const client = newClient(server);
  const started = Date.now();
  await client.connect();
  const elapsed = Date.now() - started;

  assert.equal(server.sockets.length, 1);
  assert.equal(elapsed < 1000, true);
  assert.equal(server.authFrame.support.split(',').includes('search_tracks'), true);
  client.dispose();
});

test('helpers absent at the warm-up deadline open with honest capabilities', async () => {
  const fake = Fake.create();
  delete fake.GraphQL;
  delete fake.Platform.History;
  delete fake.Platform.PlaybackAPI;
  delete fake.Platform.ConnectAPI;

  const server = createServer({ deviceScope: 'unknown' });
  const client = newClient(server, { fake, deviceScope: null });
  const started = Date.now();
  await client.connect();
  const elapsed = Date.now() - started;

  assert.equal(elapsed >= 1400, true);
  assert.equal(elapsed < 2600, true);
  assert.equal(client.status, 'authenticated');
  assert.equal(client.capabilities.search_tracks, 'unsupported');
  assert.equal(client.deviceScope, 'unknown');
  assert.equal(server.authFrame.support.split(',').includes('search_tracks'), false);
  assert.equal(fake.calls.graphql, 0);
  assert.equal(fake.writes(), 0);
  client.dispose();
});

test('a disconnect during warm-up rejects before any socket is opened', async () => {
  const fake = Fake.create();
  delete fake.GraphQL;
  delete fake.Platform.History;
  delete fake.Platform.PlaybackAPI;
  delete fake.Platform.ConnectAPI;

  const server = createServer();
  const client = newClient(server, { fake });
  const pending = client.connect();
  await waitFor(() => client.status === 'connecting');
  await new Promise((resolve) => setTimeout(resolve, 80));
  client.dispose();

  await assert.rejects(pending, /CLIENT_DISCONNECTED/);
  await new Promise((resolve) => setTimeout(resolve, 150));
  assert.equal(server.sockets.length, 0);
  assert.equal(client.status, 'closed');
  assert.equal(fake.calls.graphql, 0);
  assert.equal(fake.writes(), 0);
});

test('a command before ready is ignored and executed nowhere', async () => {
  const server = createServer({ autoReady: false });
  const client = newClient(server);
  const pending = client.connect();
  await waitFor(() => server.socket !== null);
  server.command('cmd-early', 'play');
  await waitFor(() => client.counters.preauth_commands === 1);
  assert.equal(server.results.length, 0);
  assert.equal(globalThis.Spicetify.writes(), 0);
  client.dispose();
  await pending.catch(() => {});
});

test('play already playing is an honest no-op without touching the player', async () => {
  const server = createServer({ mode: 'control' });
  const client = newClient(server);
  await client.connect();
  server.command('cmd-play', 'play');
  await waitFor(() => server.results.length === 1);
  assert.equal(server.results[0].status, 'SUCCESS_VERIFIED');
  assert.equal(globalThis.Spicetify.calls.play, 0);
  client.dispose();
});

test('pause is called once and verified', async () => {
  const server = createServer({ mode: 'control' });
  const client = newClient(server);
  await client.connect();
  server.command('cmd-pause', 'pause');
  await waitFor(() => server.results.length === 1);
  assert.equal(server.results[0].status, 'SUCCESS_VERIFIED');
  assert.equal(server.results[0].data.is_playing, false);
  assert.equal(globalThis.Spicetify.calls.pause, 1);
  client.dispose();
});

test('set_volume applies the level once and verifies within tolerance', async () => {
  const server = createServer({ mode: 'control' });
  const client = newClient(server);
  await client.connect();
  server.command('cmd-vol', 'set_volume', { level: 0.3 });
  await waitFor(() => server.results.length === 1);
  assert.equal(server.results[0].status, 'SUCCESS_VERIFIED');
  assert.equal(globalThis.Spicetify.calls.setVolume, 1);
  assert.equal(server.results[0].data.volume, 0.3);
  client.dispose();
});

test('set_volume outside tolerance is NOT_VERIFIED with no data', async () => {
  const server = createServer({ mode: 'control' });
  const client = newClient(server, { fakeOptions: { volumeSticks: false, volume: 0.1 } });
  await client.connect();
  server.command('cmd-vol', 'set_volume', { level: 0.3 }, 120);
  await waitFor(() => server.results.length === 1);
  const result = server.results[0];
  assert.equal(result.status, 'NOT_VERIFIED');
  assert.equal(result.data, null);
  assert.deepEqual(result.evidence, { kind: 'player_state', observed: false });
  assert.equal(globalThis.Spicetify.calls.setVolume, 1);
  client.dispose();
});

test('next verified only when the observed URI changes', async () => {
  const server = createServer({ mode: 'control' });
  const client = newClient(server);
  await client.connect();
  server.command('cmd-next', 'next');
  await waitFor(() => server.results.length === 1);
  assert.equal(server.results[0].status, 'SUCCESS_VERIFIED');
  assert.equal(server.results[0].data.track_uri, 'spotify:track:qa-s1-0002');
  assert.equal(globalThis.Spicetify.calls.next, 1);
  client.dispose();
});

test('next with an unchanged URI is NOT_VERIFIED and never retried', async () => {
  const server = createServer({ mode: 'control' });
  const client = newClient(server, { fakeOptions: { nextChanges: false } });
  await client.connect();
  server.command('cmd-next', 'next', {}, 120);
  await waitFor(() => server.results.length === 1);
  const result = server.results[0];
  assert.equal(result.status, 'NOT_VERIFIED');
  assert.equal(result.data, null);
  assert.equal(globalThis.Spicetify.calls.next, 1);
  client.dispose();
});

// Migrated: a restart near the start is ambiguous (the position barely moves),
// so it stays NOT_VERIFIED with exactly one `back`. The proven-restart case is
// covered by the positive test below.
test('previous restart near the start is ambiguous and NOT_VERIFIED', async () => {
  const server = createServer({ mode: 'control' });
  const client = newClient(server, {
    fakeOptions: { backRestarts: true, position: 200 }
  });
  await client.connect();
  server.command('cmd-prev', 'previous', {}, 120);
  await waitFor(() => server.results.length === 1);
  assert.equal(server.results[0].status, 'NOT_VERIFIED');
  assert.equal(server.results[0].data, null);
  assert.equal(globalThis.Spicetify.calls.back, 1);
  client.dispose();
});

test('previous proves a restart from a meaningful position', async () => {
  const server = createServer({ mode: 'control' });
  const client = newClient(server, {
    fakeOptions: { backRestarts: true, position: 12000, backPosition: 0 }
  });
  await client.connect();
  server.command('cmd-prev', 'previous', {}, 400);
  await waitFor(() => server.results.length === 1);
  const result = server.results[0];
  assert.equal(result.status, 'SUCCESS_VERIFIED');
  assert.deepEqual(result.evidence, { kind: 'player_state', observed: true });
  assert.equal(result.data.track_uri, 'spotify:track:qa-s1-0001');
  assert.ok(result.data.position_ms <= 1500);
  assert.equal(globalThis.Spicetify.calls.back, 1);
  client.dispose();
});

test('previous with an unchanged position is NOT_VERIFIED', async () => {
  const server = createServer({ mode: 'control' });
  const client = newClient(server, {
    fakeOptions: { backRestarts: true, position: 12000, backPosition: 12000 }
  });
  await client.connect();
  server.command('cmd-prev', 'previous', {}, 200);
  await waitFor(() => server.results.length === 1);
  assert.equal(server.results[0].status, 'NOT_VERIFIED');
  assert.equal(server.results[0].data, null);
  assert.equal(globalThis.Spicetify.calls.back, 1);
  client.dispose();
});

test('previous below the restart threshold is NOT_VERIFIED', async () => {
  const server = createServer({ mode: 'control' });
  const client = newClient(server, {
    fakeOptions: { backRestarts: true, position: 2500, backPosition: 0 }
  });
  await client.connect();
  server.command('cmd-prev', 'previous', {}, 200);
  await waitFor(() => server.results.length === 1);
  assert.equal(server.results[0].status, 'NOT_VERIFIED');
  assert.equal(server.results[0].data, null);
  assert.equal(globalThis.Spicetify.calls.back, 1);
  client.dispose();
});

test('next keeps URI-only verification and never accepts a restart', async () => {
  const server = createServer({ mode: 'control' });
  const client = newClient(server, {
    fakeOptions: { nextChanges: false, position: 12000 }
  });
  await client.connect();
  server.command('cmd-next-same', 'next', {}, 200);
  await waitFor(() => server.results.length === 1);
  assert.equal(server.results[0].status, 'NOT_VERIFIED');
  assert.equal(server.results[0].data, null);
  assert.equal(globalThis.Spicetify.calls.next, 1);
  client.dispose();
});

test('read_only mode refuses a mutation locally with zero writes', async () => {
  const server = createServer({ mode: 'read_only' });
  const client = newClient(server);
  await client.connect();
  server.command('cmd-play', 'play');
  await waitFor(() => server.errors.length === 1);
  assert.equal(server.errors[0].code, 'MODE_READ_ONLY');
  assert.equal(client.counters.blocked_read_only, 1);
  assert.equal(globalThis.Spicetify.writes(), 0);
  assert.equal(server.results.length, 0);
  client.dispose();
});

test('unknown device scope refuses a mutation locally with zero writes', async () => {
  const server = createServer({ mode: 'control', deviceScope: 'unknown' });
  const client = newClient(server, { deviceScope: 'unknown' });
  await client.connect();
  assert.equal(client.capabilities.play, 'blocked_device_scope');
  server.command('cmd-play', 'play');
  await waitFor(() => server.errors.length === 1);
  assert.equal(server.errors[0].code, 'DEVICE_SCOPE_BLOCKED');
  assert.equal(client.counters.blocked_device_scope, 1);
  assert.equal(globalThis.Spicetify.writes(), 0);
  client.dispose();
});

test('a duplicate command id executes once and answers DUPLICATE_ID', async () => {
  const server = createServer({ mode: 'control' });
  const client = newClient(server);
  await client.connect();
  server.command('cmd-dup', 'pause');
  server.command('cmd-dup', 'pause');
  await waitFor(() => server.results.length === 1 && server.errors.length === 1);
  assert.equal(globalThis.Spicetify.calls.pause, 1);
  assert.equal(server.errors[0].code, 'DUPLICATE_ID');
  assert.equal(client.counters.duplicate_commands, 1);
  client.dispose();
});

test('a wrong server proof is rejected before accepting any command', async () => {
  const server = createServer({ tamperServerProof: true });
  const client = newClient(server);
  await assert.rejects(client.connect(), /SERVER_PROOF_INVALID/);
  assert.equal(client.counters.server_proof_failures, 1);
  assert.equal(client.status, 'rejected');
  client.dispose();
});

test('a ready that disagrees with the proven mode is a protocol error', async () => {
  const server = createServer({ mode: 'read_only', readyMode: 'control' });
  const client = newClient(server);
  await assert.rejects(client.connect(), /PROTOCOL_ERROR/);
  assert.equal(server.results.length, 0);
  client.dispose();
});

test('malformed frames close the client', async () => {
  const server = createServer();
  const client = newClient(server);
  await client.connect();
  server.socket.deliver('{not json');
  await waitFor(() => client.counters.protocol_errors === 1);
  assert.equal(server.socket.closed, true);
  assert.equal(server.socket.closeCode, 1008);
  client.dispose();
});

test('unexpected arguments fail the frame schema without touching the player', async () => {
  const server = createServer({ mode: 'control' });
  const client = newClient(server);
  await client.connect();
  server.command('cmd-args', 'play', { level: 0.2 });
  await waitFor(() => client.counters.protocol_errors === 1);
  assert.equal(server.socket.closed, true);
  assert.equal(server.errors.length, 0);
  assert.equal(globalThis.Spicetify.writes(), 0);
  client.dispose();
});

test('an unknown action fails the frame schema before any effect', async () => {
  const server = createServer({ mode: 'control' });
  const client = newClient(server);
  await client.connect();
  server.command('cmd-seek', 'seek', { position: 1 });
  await waitFor(() => client.counters.protocol_errors === 1);
  assert.equal(server.socket.closed, true);
  assert.equal(server.socket.closeCode, 1008);
  assert.equal(server.results.length, 0);
  assert.equal(globalThis.Spicetify.writes(), 0);
  client.dispose();
});

test('a duplicate JSON key is rejected before any effect, even escaped', async () => {
  const server = createServer({ mode: 'control' });
  const client = newClient(server);
  await client.connect();
  const raw =
    '{"v":2,"type":"command","id":"cmd-dup-json","session":"' +
    SESSION +
    '","action":"pause","act\\u0069on":"play","args":{},"deadline_ms":1000}';
  server.socket.deliver(raw);
  await waitFor(() => client.counters.protocol_errors === 1);
  assert.equal(server.socket.closed, true);
  assert.equal(server.socket.closeCode, 1008);
  assert.equal(globalThis.Spicetify.writes(), 0);
  assert.equal(server.results.length, 0);
  client.dispose();
});

test('an extra envelope field is rejected before any effect', async () => {
  const server = createServer({ mode: 'control' });
  const client = newClient(server);
  await client.connect();
  server.socket.deliver({
    v: 2,
    type: 'command',
    id: 'cmd-extra',
    session: SESSION,
    action: 'play',
    args: {},
    deadline_ms: 1000,
    unexpected: true
  });
  await waitFor(() => client.counters.protocol_errors === 1);
  assert.equal(server.socket.closed, true);
  assert.equal(globalThis.Spicetify.writes(), 0);
  client.dispose();
});

test('a non-integer or out-of-range deadline is rejected before any effect', async () => {
  for (const deadline of ['200', 200.5, 99, 5001]) {
    const server = createServer({ mode: 'control' });
    const client = newClient(server);
    await client.connect();
    server.socket.deliver({
      v: 2,
      type: 'command',
      id: 'cmd-deadline',
      session: SESSION,
      action: 'play',
      args: {},
      deadline_ms: deadline
    });
    await waitFor(() => client.counters.protocol_errors === 1);
    assert.equal(globalThis.Spicetify.writes(), 0);
    client.dispose();
  }
});

test('a second command sees BUSY and cannot mutate while one is pending', async () => {
  const server = createServer({ mode: 'control' });
  const client = newClient(server, { fakeOptions: { volumeSticks: false } });
  await client.connect();
  server.command('pending-volume', 'set_volume', { level: 0.8 }, 200);
  await waitFor(() => globalThis.Spicetify.writes() === 1);
  server.command('parallel-play', 'play', {}, 200);
  await waitFor(() => server.errors.length === 1);
  assert.equal(server.errors[0].code, 'BUSY');
  assert.equal(client.counters.busy_commands, 1);
  assert.equal(globalThis.Spicetify.writes(), 1);
  client.dispose();
});

test('an old verification never publishes onto a newly authenticated session', async () => {
  const server = createServer({ mode: 'control' });
  const client = newClient(server, { fakeOptions: { volumeSticks: false } });
  await client.connect();
  server.command('old-operation', 'set_volume', { level: 0.8 }, 200);
  await waitFor(() => globalThis.Spicetify.writes() === 1);
  server.socket.close(1006, 'transport');
  const pending = client.connect();
  await waitFor(() => server.sockets.length === 2);
  await pending;
  await new Promise((resolve) => setTimeout(resolve, 220));
  assert.equal(globalThis.Spicetify.writes(), 1);
  assert.equal(
    server.sockets[1].sent.filter((item) => item.id === 'old-operation').length,
    0
  );
  assert.equal(client.counters.results_dropped >= 1, true);
  client.dispose();
});

test('reconnect after a closed socket re-authenticates without repeating work', async () => {
  const server = createServer({ mode: 'control' });
  const client = newClient(server, { autoReconnect: true });
  await client.connect();
  server.command('cmd-once', 'pause');
  await waitFor(() => server.results.length === 1);
  assert.equal(globalThis.Spicetify.calls.pause, 1);
  server.socket.close(1006, 'transport');
  await waitFor(() => client.status === 'authenticated' && server.sockets.length === 2, 3000);
  assert.equal(client.counters.reconnects, 1);
  assert.equal(globalThis.Spicetify.calls.pause, 1);
  assert.equal(server.results.length, 1);
  client.dispose();
});

test('a socket refused before hello recovers once without a reload', async () => {
  const server = createServer({ mode: 'control', refuseFirst: true });
  const client = newClient(server, { autoReconnect: true });
  const started = client.start();
  await assert.rejects(started, /CLOSED_BEFORE_READY/);

  await waitFor(() => client.status === 'authenticated', 2000);
  assert.equal(server.sockets.length, 2);
  assert.equal(client.counters.startup_retries, 1);
  assert.equal(client.counters.mutations_executed, 0);
  assert.equal(globalThis.Spicetify.writes(), 0);
  client.dispose();
});

test('without autoReconnect a refused socket is not retried', async () => {
  const server = createServer({ refuseFirst: true });
  const client = newClient(server);
  await assert.rejects(client.start(), /CLOSED_BEFORE_READY/);
  await new Promise((resolve) => setTimeout(resolve, 500));

  assert.equal(server.sockets.length, 1);
  assert.equal(client.counters.startup_retries, 0);
  client.dispose();
});

test('server MODE_MISMATCH and ROLE_VIOLATION refusals are terminal', async () => {
  for (const code of ['MODE_MISMATCH', 'ROLE_VIOLATION']) {
    const server = createServer({ mode: 'control', rejectCode: code });
    const client = newClient(server, { autoReconnect: true });
    const started = client.start();
    await assert.rejects(started, new RegExp(code));
    await new Promise((resolve) => setTimeout(resolve, 700));

    assert.equal(server.sockets.length, 1, code + ' must not retry');
    assert.equal(client.counters.startup_retries, 0, code);
    assert.equal(client.status, 'failed', code);
    assert.equal(server.authFrame, null, code);
    client.dispose();
  }
});

test('transport retries are capped, never exhausted, and a late host is reached', async () => {
  const server = createServer({ mode: 'control', refuseCount: 2 });
  const client = newClient(server, { autoReconnect: true });
  client.start().catch(() => {});
  try {
    await waitFor(() => server.sockets.length >= 2, 3000);
    // Já não existe RECONNECT_EXHAUSTED: o host ausente continua a ser tentado
    // com backoff limitado, e a terceira tentativa autentica sem reload.
    await waitFor(() => client.status === 'authenticated', 4000);
    assert.equal(server.sockets.length, 3);
    assert.equal(client.counters.startup_retries, 2);
    assert.equal(client.status, 'authenticated');
    assert.equal(client.counters.mutations_executed, 0);
    assert.equal(globalThis.Spicetify.writes(), 0);
  } finally {
    client.dispose();
  }
});

test('focus wakes a scheduled transport retry without touching a latched failure', async () => {
  const handlers = {};
  const previous = globalThis.addEventListener;
  globalThis.addEventListener = (type, handler) => {
    handlers[type] = handler;
  };
  const server = createServer({ mode: 'control', refuseFirst: true });
  const client = newClient(server, { autoReconnect: true });
  const started = client.start();
  started.catch(() => {});
  try {
    await assert.rejects(started, /CLOSED_BEFORE_READY/);
    assert.equal(client.status, 'reconnecting');
    assert.equal(server.sockets.length, 1);
    assert.equal(typeof handlers.focus, 'function');
    handlers.focus();
    await waitFor(() => client.status === 'authenticated', 1500);
    assert.equal(server.sockets.length, 2);
    assert.equal(globalThis.Spicetify.writes(), 0);
  } finally {
    client.dispose();
    globalThis.addEventListener = previous;
  }
});

test('a terminal auth refusal stays latched when focus fires', async () => {
  const handlers = {};
  const previous = globalThis.addEventListener;
  globalThis.addEventListener = (type, handler) => {
    handlers[type] = handler;
  };
  const server = createServer({ mode: 'control', rejectCode: 'AUTH_FAILED' });
  const client = newClient(server, { autoReconnect: true });
  const started = client.start();
  started.catch(() => {});
  try {
    await assert.rejects(started, /AUTH_FAILED/);
    await new Promise((resolve) => setTimeout(resolve, 300));
    const before = server.sockets.length;
    if (typeof handlers.focus === 'function') {
      handlers.focus();
    }
    await new Promise((resolve) => setTimeout(resolve, 400));
    assert.equal(server.sockets.length, before);
    assert.equal(client.status, 'failed');
  } finally {
    client.dispose();
    globalThis.addEventListener = previous;
  }
});

test('a local device that appears after auth is re-signed, never promoted in place', async () => {
  const fake = Fake.create({ scope: 'discordant' });
  const server = createServer({ mode: 'control', deviceScope: 'unknown' });
  const client = newClient(server, {
    fake,
    deviceScope: null,
    autoReconnect: true
  });
  await client.connect();
  const firstCapabilities = client.capabilities;
  assert.equal(client.deviceScope, 'unknown');
  assert.equal(firstCapabilities.play, 'blocked_device_scope');

  fake.setScope('local');
  server.setScope('local');
  await waitFor(
    () =>
      client.status === 'authenticated' &&
      client.deviceScope === 'local' &&
      client.counters.session_refreshes === 1,
    2000
  );

  assert.equal(server.sockets.length, 2);
  assert.equal(server.authFrame.device_scope, 'local');
  assert.equal(client.capabilities.play, 'available');
  assert.equal(firstCapabilities.play, 'blocked_device_scope');
  assert.equal(server.results.length, 0);
  assert.equal(fake.calls.graphql, 0);
  assert.equal(fake.writes(), 0);
  client.dispose();
});

test('an observation that never proves local is not re-signed', async () => {
  const fake = Fake.create({ scope: 'discordant' });
  const server = createServer({ mode: 'control', deviceScope: 'unknown' });
  const client = newClient(server, {
    fake,
    deviceScope: null,
    autoReconnect: true
  });
  await client.connect();
  await new Promise((resolve) => setTimeout(resolve, 900));

  assert.equal(server.sockets.length, 1);
  assert.equal(client.counters.session_refreshes, 0);
  assert.equal(client.status, 'authenticated');
  assert.equal(client.deviceScope, 'unknown');
  client.dispose();
});

test('a settled signed snapshot opens no further socket', async () => {
  const server = createServer({ mode: 'control' });
  const client = newClient(server, { autoReconnect: true });
  await client.connect();
  await new Promise((resolve) => setTimeout(resolve, 800));

  assert.equal(server.sockets.length, 1);
  assert.equal(client.counters.session_refreshes, 0);
  assert.equal(client.status, 'authenticated');
  client.dispose();
});

test('a rejected server proof is terminal even with autoReconnect', async () => {
  const server = createServer({ tamperServerProof: true });
  const client = newClient(server, { autoReconnect: true });
  await assert.rejects(client.connect(), /SERVER_PROOF_INVALID/);
  await new Promise((resolve) => setTimeout(resolve, 600));

  assert.equal(server.sockets.length, 1);
  assert.equal(client.counters.startup_retries, 0);
  assert.equal(client.status, 'rejected');
  client.dispose();
});

test('a disconnect stops the startup-local watch', async () => {
  const fake = Fake.create({ scope: 'discordant' });
  const server = createServer({ mode: 'control', deviceScope: 'unknown' });
  const client = newClient(server, {
    fake,
    deviceScope: null,
    autoReconnect: true
  });
  await client.connect();
  client.dispose();
  fake.setScope('local');
  await new Promise((resolve) => setTimeout(resolve, 900));

  assert.equal(server.sockets.length, 1);
  assert.equal(client.counters.session_refreshes, 0);
  assert.equal(client.status, 'closed');
});

test('an in-flight read is never cut by a session refresh', async () => {
  const fake = Fake.create({ scope: 'discordant', readyAfterMs: 300 });
  const server = createServer({ mode: 'control', deviceScope: 'unknown' });
  const client = newClient(server, {
    fake,
    deviceScope: null,
    autoReconnect: true
  });
  await client.connect();
  server.command('cmd-read-startup', 'get_player_state', {}, 1500);
  fake.setScope('local');
  server.setScope('local');

  await waitFor(() => server.results.length === 1, 2000);
  assert.equal(server.results[0].status, 'SUCCESS_VERIFIED');
  assert.equal(client.counters.session_refreshes, 0);
  assert.equal(server.sockets.length, 1);

  await waitFor(
    () =>
      client.counters.session_refreshes === 1 &&
      client.deviceScope === 'local' &&
      client.status === 'authenticated',
    2000
  );
  assert.equal(server.sockets.length, 2);
  assert.equal(server.results.length, 1);
  assert.equal(fake.writes(), 0);
  assert.equal(client.counters.mutations_executed, 0);
  client.dispose();
});

test('dispose closes the socket and stops reconnect attempts', async () => {
  const server = createServer();
  const client = newClient(server, { autoReconnect: true });
  await client.connect();
  client.dispose();
  assert.equal(client.status, 'closed');
  assert.equal(server.socket.closed, true);
  await new Promise((resolve) => setTimeout(resolve, 400));
  assert.equal(server.sockets.length, 1);
});

test('a player without a method reports it unsupported and refuses the command', async () => {
  const fake = Fake.create();
  delete fake.Player.pause;
  const server = createServer({ mode: 'control' });
  const client = newClient(server, { fake });
  await client.connect();
  assert.equal(client.capabilities.pause, 'unsupported');
  server.command('cmd-pause', 'pause');
  await waitFor(() => server.errors.length === 1);
  assert.equal(server.errors[0].code, 'CAPABILITY_UNSUPPORTED');
  assert.equal(globalThis.Spicetify.writes(), 0);
  client.dispose();
});

test('an empty player state is null metadata, never an invented track', async () => {
  const server = createServer();
  const client = newClient(server, { fakeOptions: { noTrack: true } });
  await client.connect();
  server.command('cmd-read', 'get_player_state');
  await waitFor(() => server.results.length === 1);
  const data = server.results[0].data;
  assert.equal(server.results[0].status, 'SUCCESS_VERIFIED');
  assert.equal(data.track_uri, null);
  assert.equal(data.title, null);
  assert.equal(data.artist, null);
  assert.equal(data.album, null);
  assert.equal(data.is_playing, true);
  assert.equal(data.volume, 0.42);
  client.dispose();
});

test('a defined getter that is not readable yet is waited for, not reported as error', async () => {
  const server = createServer();
  const client = newClient(server, { fakeOptions: { readyAfterMs: 120 } });
  await client.connect();
  const started = Date.now();
  server.command('cmd-late-ready', 'get_player_state', {}, 1000);
  await waitFor(() => server.results.length === 1 || server.errors.length === 1);
  const elapsed = Date.now() - started;
  assert.equal(server.errors.length, 0);
  assert.equal(server.results[0].status, 'SUCCESS_VERIFIED');
  assert.equal(server.results[0].data.track_uri, 'spotify:track:qa-s1-0001');
  assert.equal(globalThis.Spicetify.writes(), 0);
  assert.equal(elapsed >= 80, true);
  client.dispose();
});

test('a player that never becomes readable fails once at the deadline with a stage', async () => {
  const server = createServer();
  const client = newClient(server, { fakeOptions: { neverReady: true } });
  await client.connect();
  const started = Date.now();
  server.command('cmd-never', 'get_player_state', {}, 300);
  await waitFor(() => server.errors.length === 1, 3000);
  const elapsed = Date.now() - started;
  const error = server.errors[0];
  assert.equal(error.code, 'PLAYER_NOT_READY');
  assert.equal(error.detail, 'volume');
  assert.deepEqual(Object.keys(error).sort(), ['code', 'detail', 'id', 'type', 'v']);
  assert.equal(server.results.length, 0);
  assert.equal(client.status, 'authenticated');
  assert.equal(elapsed >= 200, true);
  assert.equal(elapsed < 900, true);
  await new Promise((resolve) => setTimeout(resolve, 150));
  assert.equal(server.errors.length, 1);
  client.dispose();
});

test('the failing getter names the stage and no raw exception reaches the wire', async () => {
  const server = createServer();
  const client = newClient(server, { fakeOptions: { failGetter: 'getProgress' } });
  await client.connect();
  server.command('cmd-progress', 'get_player_state', {}, 200);
  await waitFor(() => server.errors.length === 1, 3000);
  const error = server.errors[0];
  assert.equal(error.code, 'PLAYER_NOT_READY');
  assert.equal(error.detail, 'progress');
  assert.equal(JSON.stringify(error).includes('TypeError'), false);
  assert.equal(JSON.stringify(error).includes('fixture'), false);
  assert.equal(server.results.length, 0);
  client.dispose();
});

test('a mutation on an unreadable player is refused with zero effects', async () => {
  const server = createServer({ mode: 'control' });
  const client = newClient(server, { fakeOptions: { neverReady: true } });
  await client.connect();
  server.command('cmd-play', 'play', {}, 200);
  await waitFor(() => server.errors.length === 1, 3000);
  assert.equal(server.errors[0].code, 'PLAYER_NOT_READY');
  assert.equal(globalThis.Spicetify.writes(), 0);
  client.dispose();
});

test('a disconnect during the readiness wait publishes nothing and mutates nothing', async () => {
  const server = createServer({ mode: 'control' });
  const client = newClient(server, { fakeOptions: { neverReady: true } });
  await client.connect();
  server.command('cmd-pause', 'pause', {}, 1000);
  await new Promise((resolve) => setTimeout(resolve, 60));
  server.socket.close(1006, 'transport');
  await new Promise((resolve) => setTimeout(resolve, 250));
  assert.equal(server.errors.length, 0);
  assert.equal(server.results.length, 0);
  assert.equal(globalThis.Spicetify.writes(), 0);
  assert.equal(client.counters.results_dropped >= 1, true);
  client.dispose();
});

test('device scope is local only when Connect and PlaybackAPI agree', async () => {
  for (const [scope, expected] of [
    ['local', 'local'],
    ['remote', 'remote'],
    ['unknown', 'unknown'],
    ['discordant', 'unknown']
  ]) {
    const fake = Fake.create({ scope });
    const server = createServer({ deviceScope: expected, mode: 'control' });
    const client = newClient(server, { fake, deviceScope: null });
    await client.connect();
    assert.equal(client.deviceScope, expected, scope);
    client.dispose();
  }
});

test('get_device_scope reports current getters without rewriting the auth snapshot', async () => {
  const fake = Fake.create({ scope: 'local' });
  const server = createServer({ mode: 'control', deviceScope: 'local' });
  const client = newClient(server, { fake, deviceScope: null });
  await client.connect();
  assert.equal(client.deviceScope, 'local');
  assert.equal(client.capabilities.play, 'available');

  fake.setScope('remote');
  server.command('cmd-device-scope', 'get_device_scope');
  await waitFor(() => server.results.length === 1);
  const result = server.results[0];
  assert.equal(result.status, 'SUCCESS_VERIFIED');
  assert.deepEqual(result.data, {
    device_scope: 'remote',
    connect_active: true,
    connect_local: false,
    playback_local: false
  });
  assert.deepEqual(Object.keys(result.data).sort(), [
    'connect_active',
    'connect_local',
    'device_scope',
    'playback_local'
  ]);
  assert.deepEqual(result.evidence, { kind: 'device_scope', observed: true });
  assert.equal(client.deviceScope, 'local');
  assert.equal(client.capabilities.play, 'available');
  assert.equal(fake.writes(), 0);

  server.command('cmd-device-scope-play', 'play', {}, 200);
  await waitFor(() => server.errors.length === 1);
  assert.equal(server.errors[0].code, 'DEVICE_SCOPE_BLOCKED');
  assert.equal(fake.writes(), 0);
  client.dispose();
});

test('an unknown diagnostic is honest and authorizes no mutation', async () => {
  const fake = Fake.create({ scope: 'unknown' });
  const server = createServer({ mode: 'control', deviceScope: 'unknown' });
  const client = newClient(server, { fake, deviceScope: null });
  await client.connect();
  assert.equal(client.capabilities.play, 'blocked_device_scope');

  server.command('cmd-device-unknown', 'get_device_scope');
  await waitFor(() => server.results.length === 1);
  assert.deepEqual(server.results[0].data, {
    device_scope: 'unknown',
    connect_active: null,
    connect_local: null,
    playback_local: false
  });
  assert.equal(server.results[0].evidence.kind, 'device_scope');

  server.command('cmd-device-unknown-play', 'play', {}, 200);
  await waitFor(() => server.errors.length === 1);
  assert.equal(server.errors[0].code, 'DEVICE_SCOPE_BLOCKED');
  assert.equal(fake.writes(), 0);
  client.dispose();
});

test('get_device_scope returns nulls when its getters disappear, never an inferred local', async () => {
  const fake = Fake.create({ scope: 'local' });
  const server = createServer({ mode: 'control', deviceScope: 'local' });
  const client = newClient(server, { fake, deviceScope: null });
  await client.connect();
  delete fake.Platform.ConnectAPI;
  delete fake.Platform.PlaybackAPI;

  server.command('cmd-device-missing', 'get_device_scope');
  await waitFor(() => server.results.length === 1);
  assert.deepEqual(server.results[0].data, {
    device_scope: 'unknown',
    connect_active: null,
    connect_local: null,
    playback_local: null
  });
  assert.equal(fake.writes(), 0);
  client.dispose();
});

test('a scope change after auth blocks the mutation with zero writes', async () => {
  const fake = Fake.create({ scope: 'local' });
  const server = createServer({ mode: 'control', deviceScope: 'local' });
  const client = newClient(server, { fake, deviceScope: null });
  await client.connect();
  assert.equal(client.capabilities.play, 'available');
  fake.setScope('remote');
  server.command('cmd-play', 'play', {}, 400);
  await waitFor(() => server.errors.length === 1, 3000);
  assert.equal(server.errors[0].code, 'DEVICE_SCOPE_BLOCKED');
  assert.equal(server.results.length, 0);
  assert.equal(fake.writes(), 0);
  client.dispose();
});

test('search_tracks returns a strict limited list and never plays', async () => {
  const fake = Fake.create();
  const server = createServer({ mode: 'read_only' });
  const client = newClient(server, { fake });
  await client.connect();
  server.command('cmd-search', 'search_tracks', { query: 'qa one', limit: 2 }, 800);
  await waitFor(() => server.results.length === 1, 3000);
  const result = server.results[0];
  assert.equal(result.status, 'SUCCESS_VERIFIED');
  assert.deepEqual(result.evidence, { kind: 'effect', observed: true });
  assert.equal(result.data.tracks.length, 2);
  assert.equal(result.data.tracks[0].uri, 'spotify:track:' + '0'.repeat(21) + '1');
  assert.equal(result.data.tracks[0].title, 'QA Search One');
  assert.equal(fake.historyPaths.length, 0);
  assert.equal(fake.calls.graphql, 1);
  assert.deepEqual(fake.searchRequest().variables, {
    searchTerm: 'qa one',
    offset: 0,
    limit: 2,
    includePreReleases: false,
    includeAlbumPreReleases: false,
    numberOfTopResults: 20
  });
  assert.equal(fake.writes(), 0);
  client.dispose();
});

test('search uses the requested deadline and survives a 1600ms query within 2500ms', async () => {
  const fake = Fake.create({ searchDelayMs: 1600 });
  const server = createServer({ mode: 'read_only' });
  const client = newClient(server, { fake });
  await client.connect();
  const started = Date.now();
  server.command('cmd-search-long', 'search_tracks', { query: 'qa long', limit: 1 }, 2500);
  await waitFor(() => server.results.length === 1 || server.errors.length === 1, 4000);
  const elapsed = Date.now() - started;
  assert.equal(server.errors.length, 0);
  assert.equal(server.results[0].status, 'SUCCESS_VERIFIED');
  assert.equal(fake.calls.graphql, 1);
  assert.equal(elapsed >= 1500, true);
  assert.equal(fake.writes(), 0);
  client.dispose();
});

test('a short search deadline fails once without retry', async () => {
  const fake = Fake.create({ searchDelayMs: 1600 });
  const server = createServer({ mode: 'read_only' });
  const client = newClient(server, { fake });
  await client.connect();
  server.command('cmd-search-short', 'search_tracks', { query: 'qa short', limit: 1 }, 300);
  await waitFor(() => server.errors.length === 1, 2000);
  assert.equal(server.errors[0].code, 'SEARCH_FAILED');
  assert.equal(server.errors[0].detail, 'query');
  assert.equal(fake.calls.graphql, 1);
  assert.equal(server.results.length, 0);
  await new Promise((resolve) => setTimeout(resolve, 250));
  assert.equal(server.errors.length, 1);
  assert.equal(fake.calls.graphql, 1);
  client.dispose();
});

test('search navigates the native route once when the definition is absent', async () => {
  const fake = Fake.create({ searchDefinition: 'onPush', definitionDelayMs: 40 });
  const server = createServer({ mode: 'read_only' });
  const client = newClient(server, { fake });
  await client.connect();
  server.command('cmd-route', 'search_tracks', { query: 'qa one', limit: 1 }, 800);
  await waitFor(() => server.results.length === 1, 3000);
  assert.equal(server.results[0].status, 'SUCCESS_VERIFIED');
  assert.deepEqual(fake.historyPaths, ['/search/qa%20one']);
  client.dispose();
});

test('an unexpected search shape fails honestly and never invents a list', async () => {
  const fake = Fake.create({
    searchPayload: {
      data: { searchV2: { tracksV2: { items: [{ item: { data: { __typename: 'Track' } } }] } } }
    }
  });
  const server = createServer({ mode: 'read_only' });
  const client = newClient(server, { fake });
  await client.connect();
  server.command('cmd-shape', 'search_tracks', { query: 'qa', limit: 2 }, 400);
  await waitFor(() => server.errors.length === 1, 3000);
  assert.equal(server.errors[0].code, 'SEARCH_FAILED');
  assert.equal(server.errors[0].detail, 'shape');
  assert.equal(server.results.length, 0);
  client.dispose();
});

test('search still works when the player is not readable', async () => {
  const fake = Fake.create({ neverReady: true });
  const server = createServer({ mode: 'read_only' });
  const client = newClient(server, { fake });
  await client.connect();
  server.command('cmd-unready-search', 'search_tracks', { query: 'qa', limit: 1 }, 800);
  await waitFor(() => server.results.length === 1, 3000);
  assert.equal(server.results[0].status, 'SUCCESS_VERIFIED');
  assert.equal(server.results[0].data.tracks.length, 1);
  assert.equal(fake.writes(), 0);
  client.dispose();
});

test('GraphQL errors beside valid data fail the whole search and never echo messages', async () => {
  const fake = Fake.create({
    searchPayload: {
      errors: [{ message: 'PRIVATE_BACKEND_ERROR' }],
      data: { searchV2: { tracksV2: { items: [] } } }
    }
  });
  const server = createServer({ mode: 'read_only' });
  const client = newClient(server, { fake });
  await client.connect();
  server.command('cmd-errors', 'search_tracks', { query: 'qa', limit: 2 }, 400);
  await waitFor(() => server.errors.length === 1, 3000);
  assert.equal(server.errors[0].code, 'SEARCH_FAILED');
  assert.equal(server.errors[0].detail, 'errors');
  assert.equal(server.results.length, 0);
  assert.equal(JSON.stringify(server.errors).includes('PRIVATE_BACKEND_ERROR'), false);
  client.dispose();
});

test('a non-list errors field is refused instead of treated as empty', async () => {
  const fake = Fake.create({
    searchPayload: {
      errors: 'not-a-list',
      data: { searchV2: { tracksV2: { items: [] } } }
    }
  });
  const server = createServer({ mode: 'read_only' });
  const client = newClient(server, { fake });
  await client.connect();
  server.command('cmd-errors', 'search_tracks', { query: 'qa', limit: 1 }, 400);
  await waitFor(() => server.errors.length === 1, 3000);
  assert.equal(server.errors[0].detail, 'errors');
  assert.equal(server.results.length, 0);
  client.dispose();
});

test('an empty errors array keeps an otherwise valid search valid', async () => {
  const fake = Fake.create({
    searchPayload: {
      errors: [],
      data: {
        searchV2: {
          tracksV2: {
            items: [
              {
                item: {
                  data: {
                    __typename: 'Track',
                    uri: 'spotify:track:' + '0'.repeat(21) + '3',
                    name: 'QA Empty Errors',
                    artists: { items: [{ profile: { name: 'QA Fixture Artist' } }] },
                    albumOfTrack: { name: 'QA Fixture Album' },
                    playability: { playable: true }
                  }
                }
              }
            ]
          }
        }
      }
    }
  });
  const server = createServer({ mode: 'read_only' });
  const client = newClient(server, { fake });
  await client.connect();
  server.command('cmd-errors', 'search_tracks', { query: 'qa', limit: 1 }, 400);
  await waitFor(() => server.results.length === 1, 3000);
  assert.equal(server.results[0].status, 'SUCCESS_VERIFIED');
  assert.equal(server.results[0].data.tracks.length, 1);
  client.dispose();
});

test('play_uri counts as one executed mutation in the diagnostic counter', async () => {
  const target = 'spotify:track:' + '0'.repeat(21) + '9';
  const fake = Fake.create();
  const server = createServer({ mode: 'control' });
  const client = newClient(server, { fake });
  await client.connect();
  server.command('cmd-play-uri', 'play_uri', { uri: target }, 800);
  await waitFor(() => server.results.length === 1, 3000);
  assert.equal(client.counters.mutations_executed, 1);
  assert.equal(fake.calls.playUri, 1);
  client.dispose();
});

test('a disconnect while the route loads never starts the GraphQL query', async () => {
  const fake = Fake.create({ searchDefinition: 'onPush', definitionDelayMs: 30 });
  const server = createServer({ mode: 'read_only' });
  const client = newClient(server, { fake });
  await client.connect();
  server.command('cmd-route', 'search_tracks', { query: 'qa', limit: 1 }, 800);
  await new Promise((resolve) => setTimeout(resolve, 5));
  server.socket.close(1006, 'transport');
  await new Promise((resolve) => setTimeout(resolve, 120));
  assert.equal(fake.calls.graphql, 0);
  assert.equal(server.results.length, 0);
  assert.equal(server.errors.length, 0);
  client.dispose();
});

test('play_uri verifies the exact URI and the playing state', async () => {
  const target = 'spotify:track:' + '0'.repeat(21) + '7';
  const fake = Fake.create();
  const server = createServer({ mode: 'control' });
  const client = newClient(server, { fake });
  await client.connect();
  server.command('cmd-play-uri', 'play_uri', { uri: target }, 800);
  await waitFor(() => server.results.length === 1, 3000);
  const result = server.results[0];
  assert.equal(result.status, 'SUCCESS_VERIFIED');
  assert.equal(result.data.track_uri, target);
  assert.equal(result.data.is_playing, true);
  assert.equal(fake.calls.playUri, 1);
  client.dispose();
});

test('play_uri without the exact URI is NOT_VERIFIED, never retried', async () => {
  const target = 'spotify:track:' + '0'.repeat(21) + '8';
  const fake = Fake.create({ playUriIgnores: true });
  const server = createServer({ mode: 'control' });
  const client = newClient(server, { fake });
  await client.connect();
  server.command('cmd-play-uri', 'play_uri', { uri: target }, 200);
  await waitFor(() => server.results.length === 1, 3000);
  assert.equal(server.results[0].status, 'NOT_VERIFIED');
  assert.equal(server.results[0].data, null);
  assert.equal(fake.calls.playUri, 1);
  client.dispose();
});

test('an unknown device scope refuses play_uri before the wire', async () => {
  const fake = Fake.create({ scope: 'unknown' });
  const server = createServer({ mode: 'control', deviceScope: 'unknown' });
  const client = newClient(server, { fake, deviceScope: null });
  await client.connect();
  assert.equal(client.capabilities.play_uri, 'blocked_device_scope');
  server.command('cmd-play-uri', 'play_uri', { uri: 'spotify:track:' + '0'.repeat(22) }, 400);
  await waitFor(() => server.errors.length === 1, 3000);
  assert.equal(server.errors[0].code, 'DEVICE_SCOPE_BLOCKED');
  assert.equal(fake.calls.playUri, 0);
  client.dispose();
});

test('activate_local is available in control regardless of scope, and blocked in read_only', async () => {
  const controlServer = createServer({ mode: 'control', deviceScope: 'unknown' });
  const control = newClient(controlServer, {
    fake: Fake.create({ scope: 'unknown' }),
    deviceScope: null
  });
  await control.connect();
  assert.equal(control.capabilities.activate_local, 'available');
  assert.equal(control.capabilities.play_uri, 'blocked_device_scope');
  assert.equal(controlServer.authFrame.support.split(',').includes('activate_local'), true);
  control.dispose();

  const readOnlyServer = createServer({ mode: 'read_only' });
  const readOnly = newClient(readOnlyServer);
  await readOnly.connect();
  assert.equal(readOnly.capabilities.activate_local, 'blocked_read_only');
  readOnly.dispose();
});

test('activate_local makes one pullToLocal call and verifies only the observed local device', async () => {
  const fake = Fake.create({ scope: 'unknown' });
  const server = createServer({ mode: 'control', deviceScope: 'unknown' });
  const client = newClient(server, { fake, deviceScope: null });
  await client.connect();
  assert.equal(client.deviceScope, 'unknown');
  server.command('cmd-activate', 'activate_local', {}, 1500);
  await waitFor(() => server.results.length === 1, 3000);
  const result = server.results[0];
  assert.equal(result.status, 'SUCCESS_VERIFIED');
  assert.equal(fake.calls.pullToLocal, 1);
  assert.deepEqual(result.data, {
    device_scope: 'local',
    connect_active: true,
    connect_local: true,
    playback_local: true
  });
  assert.deepEqual(result.evidence, { kind: 'device_scope', observed: true });
  // Activation is not a music mutation and the signed snapshot is not rewritten.
  assert.equal(fake.writes(), 0);
  assert.equal(client.counters.mutations_executed, 0);
  assert.equal(client.counters.activation_calls, 1);
  assert.equal(client.deviceScope, 'unknown');
  client.dispose();
});

test('a pullToLocal promise that resolves without a local device is not success', async () => {
  const fake = Fake.create({ scope: 'unknown', activate: 'none' });
  const server = createServer({ mode: 'control', deviceScope: 'unknown' });
  const client = newClient(server, { fake, deviceScope: null });
  await client.connect();
  server.command('cmd-activate', 'activate_local', {}, 300);
  await waitFor(() => server.results.length === 1, 3000);
  assert.equal(server.results[0].status, 'NOT_VERIFIED');
  assert.equal(server.results[0].data, null);
  assert.deepEqual(server.results[0].evidence, { kind: 'device_scope', observed: false });
  assert.equal(fake.calls.pullToLocal, 1);
  assert.equal(fake.writes(), 0);
  client.dispose();
});

test('a rejected pullToLocal is called once and never retried', async () => {
  const fake = Fake.create({ scope: 'unknown', activate: 'reject' });
  const server = createServer({ mode: 'control', deviceScope: 'unknown' });
  const client = newClient(server, { fake, deviceScope: null });
  await client.connect();
  server.command('cmd-activate', 'activate_local', {}, 300);
  await waitFor(() => server.results.length === 1, 3000);
  assert.equal(server.results[0].status, 'NOT_VERIFIED');
  assert.equal(fake.calls.pullToLocal, 1);
  client.dispose();
});

test('a synchronous pullToLocal throw fails once with ACTIVATION_FAILED', async () => {
  const fake = Fake.create({ scope: 'unknown', activate: 'throw' });
  const server = createServer({ mode: 'control', deviceScope: 'unknown' });
  const client = newClient(server, { fake, deviceScope: null });
  await client.connect();
  server.command('cmd-activate', 'activate_local', {}, 400);
  await waitFor(() => server.errors.length === 1, 3000);
  assert.equal(server.errors[0].code, 'ACTIVATION_FAILED');
  assert.equal(client.counters.activation_calls, 1);
  assert.equal(fake.writes(), 0);
  client.dispose();
});

test('read_only refuses activate_local before any native call', async () => {
  const fake = Fake.create({ scope: 'unknown' });
  const server = createServer({ mode: 'read_only', deviceScope: 'unknown' });
  const client = newClient(server, { fake, deviceScope: null });
  await client.connect();
  server.command('cmd-activate', 'activate_local', {}, 400);
  await waitFor(() => server.errors.length === 1, 3000);
  assert.equal(server.errors[0].code, 'MODE_READ_ONLY');
  assert.equal(fake.calls.pullToLocal, 0);
  assert.equal(fake.writes(), 0);
  client.dispose();
});

test('activate_local accepts no device identifier or extra argument', async () => {
  const fake = Fake.create({ scope: 'unknown' });
  const server = createServer({ mode: 'control', deviceScope: 'unknown' });
  const client = newClient(server, { fake, deviceScope: null });
  await client.connect();
  server.command('cmd-activate', 'activate_local', { device_id: 'abc' }, 400);
  await waitFor(() => client.counters.protocol_errors === 1, 3000);
  assert.equal(server.socket.closed, true);
  assert.equal(fake.calls.pullToLocal, 0);
  assert.equal(fake.writes(), 0);
  client.dispose();
});

test('connecting, reading and ordinary play never activate the local device', async () => {
  const fake = Fake.create({ isPaused: true });
  const server = createServer({ mode: 'control' });
  const client = newClient(server, { fake });
  await client.connect();
  assert.equal(fake.calls.pullToLocal, 0);
  server.command('cmd-read', 'get_player_state');
  await waitFor(() => server.results.length === 1, 3000);
  server.command('cmd-play', 'play');
  await waitFor(() => server.results.length === 2, 3000);
  assert.equal(fake.calls.play, 1);
  assert.equal(fake.calls.pullToLocal, 0);
  client.dispose();
});

test('a missing config never opens a socket', async () => {
  const server = createServer();
  const client = S1.createClient({
    config: { url: 'https://example.com', secretHex: SECRET },
    createSocket: server.factory,
    spicetify: Fake.create()
  });
  assert.equal(client.status, 'unconfigured');
  await assert.rejects(client.connect(), /UNCONFIGURED/);
  assert.equal(server.sockets.length, 0);
});
