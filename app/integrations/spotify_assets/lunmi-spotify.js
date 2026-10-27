/*
 * Lunmi Spotify S1 kit — real Spicetify extension (classic script, no imports).
 *
 * Speaks the kit protocol v2 over 127.0.0.1: mutual HMAC-SHA256, nine allowlisted
 * operations, bounded effect verification, no retry after uncertainty.
 *
 * The public template intentionally carries no secret and no port. The kit's
 * `prepare` step prepends a private configuration (url + secretHex) to produce
 * the bundle that is copied, by a human, into the Spicetify extensions folder.
 *
 * Facts come only from the pinned Spicetify CLI v2.46.2 wrapper: Player.data is
 * the origin._state object (camelCase isPaused/item), so nothing here assumes the
 * snake_case PlayerState shape or invents device/account identity.
 *
 * Not proof that the Spotify CEF accepts WebSocket/WebCrypto. Device scope is
 * unknown unless a fixture states otherwise, and unknown blocks every mutation.
 */
(function (root) {
  'use strict';

  var VERSION = 2;
  var DOMAIN = 'lunmi-spotify-kit';
  var MAX_FRAME = 64 * 1024;
  var ACTIONS = [
    'get_player_state',
    'get_device_scope',
    'activate_local',
    'play',
    'pause',
    'set_volume',
    'next',
    'previous',
    'search_tracks',
    'play_uri'
  ];
  var MUTATIONS = ['play', 'pause', 'set_volume', 'next', 'previous', 'play_uri'];
  // Activation is its own capability, never a music mutation: it may target this
  // PC while the active device is remote/unknown, under a separate backend
  // approval. It still requires HMAC-authenticated control mode.
  var ACTIVATION = ['activate_local'];
  var SEARCH_LIMIT_MAX = 10;
  var SEARCH_QUERY_MAX = 200;
  var SEARCH_RESULT_MAX = 10;
  var MODES = ['read_only', 'control'];
  var SCOPES = ['local', 'remote', 'unknown'];
  var REPEATS = ['off', 'track', 'context'];
  var VOLUME_TOLERANCE = 0.02;
  // A restart is proven by the observed position, never assumed: a meaningful
  // position must fall to the very start in the same one `back`.
  var RESTART_BEFORE_MS = 3000;
  var RESTART_AFTER_MS = 1500;
  var RESTART_RECUO_MS = 2000;
  // P-1 (só transporte, nunca música): o host pode estar ausente por muito tempo
  // (Lunmi fechada ou Spotify aberto primeiro). Backoff progressivo com teto e sem
  // prazo fixo. AUTH/proof/mode/role/protocolo ficam latched e não são acordados
  // por foco/online; nenhum comando pendente é repetido depois de reconectar.
  var BACKOFF_MS = [250, 1000, 2000, 4000, 8000, 16000, 30000];
  var TRANSPORT_RECONNECT_CEILING_MS = 30000;
  var PLAYER_WAIT_MS = 15000;
  var EFFECT_BUDGET_MS = 1500;
  var SEARCH_BUDGET_MS = 4950;
  // Activation waits for the observation, not for the Promise; the wire deadline
  // still caps it so a slow switch cannot outlive the command.
  var ACTIVATION_BUDGET_MS = 4950;
  // Bounded pre-socket warm-up for Spicetify helpers that load after Player.
  // It never queries or mutates and never consumes the server's auth deadline.
  var WARMUP_MS = 1500;
  var WARMUP_INTERVAL_MS = 50;
  // Cold-start recovery is session-only and never repeats an effect. A failed
  // initial connection retries transport with the same capped backoff as above
  // (the host may simply not be running yet), and a signed snapshot that
  // under-observed the helpers (or the local device) is replaced by a NEW
  // handshake once the observation strictly improves. The observation window
  // stays finite; only transport retries are long-lived.
  var STARTUP_OBSERVE_MS = 15000;
  var STARTUP_OBSERVE_MS = 15000;
  var STARTUP_OBSERVE_INTERVAL_MS = 250;
  var MAX_SESSION_REFRESHES = 4;
  var MAX_SEEN_IDS = 256;

  var HEX64 = /^[0-9a-f]{64}$/;
  var HEX32 = /^[0-9a-f]{32}$/;
  var ID_RE = /^[A-Za-z0-9._:-]{1,64}$/;
  var URI_RE = /^spotify:[a-z]{1,20}:[A-Za-z0-9._:-]{1,120}$/;
  var TRACK_URI = /^spotify:track:[A-Za-z0-9]{22}$/;

  var encoder = new TextEncoder();

  function toHex(buffer) {
    var bytes = new Uint8Array(buffer);
    var out = '';
    for (var i = 0; i < bytes.length; i += 1) {
      out += bytes[i].toString(16).padStart(2, '0');
    }
    return out;
  }

  function fromHex(hex) {
    if (typeof hex !== 'string' || hex.length % 2 !== 0 || !/^[0-9a-f]*$/.test(hex)) {
      throw new Error('BAD_HEX');
    }
    var out = new Uint8Array(hex.length / 2);
    for (var i = 0; i < out.length; i += 1) {
      out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
    }
    return out;
  }

  function randomHex(bytes) {
    var buffer = new Uint8Array(bytes);
    root.crypto.getRandomValues(buffer);
    return toHex(buffer);
  }

  function macInput(role, serverNonce, clientNonce, epoch, mode, scope, support) {
    return (
      DOMAIN + '/' + role + '/v2\n' +
      serverNonce + '\n' +
      clientNonce + '\n' +
      epoch + '\n' +
      mode + '\n' +
      scope + '\n' +
      support
    );
  }

  async function importSecret(secretHex) {
    if (typeof secretHex !== 'string' || !HEX64.test(secretHex)) {
      throw new Error('BAD_SECRET');
    }
    return root.crypto.subtle.importKey(
      'raw',
      fromHex(secretHex),
      { name: 'HMAC', hash: 'SHA-256' },
      false,
      ['sign', 'verify']
    );
  }

  async function sign(key, text) {
    return toHex(await root.crypto.subtle.sign('HMAC', key, encoder.encode(text)));
  }

  async function verify(key, text, signatureHex) {
    if (!HEX64.test(signatureHex || '')) {
      return false;
    }
    return root.crypto.subtle.verify(
      'HMAC',
      key,
      fromHex(signatureHex),
      encoder.encode(text)
    );
  }

  function configFrom(value) {
    if (!value || typeof value !== 'object') {
      return null;
    }
    if (typeof value.url !== 'string' || !/^ws:\/\/127\.0\.0\.1:\d{1,5}$/.test(value.url)) {
      return null;
    }
    if (typeof value.secretHex !== 'string' || !HEX64.test(value.secretHex)) {
      return null;
    }
    return { url: value.url, secretHex: value.secretHex };
  }

  function readConfig() {
    return configFrom(root.LUNMI_SPOTIFY_KIT_CONFIG);
  }

  // Conservative observation: both Connect state and PlaybackAPI must agree, and a
  // fallback local device invented by the client (no active device) never counts.
  // No IDs or names leave this function; only local/remote/unknown.
  function observedScope(spicetify) {
    try {
      var source = spicetify || root.Spicetify;
      var platform = source && source.Platform;
      if (!platform || !platform.ConnectAPI || !platform.PlaybackAPI) {
        return 'unknown';
      }
      if (typeof platform.ConnectAPI.getState !== 'function') {
        return 'unknown';
      }
      var state = platform.ConnectAPI.getState();
      var active = state && state.activeDevice;
      if (!active || active.isActive !== true) {
        return 'unknown';
      }
      var apiLocal = platform.PlaybackAPI._isLocal;
      if (typeof active.isLocal !== 'boolean' || typeof apiLocal !== 'boolean') {
        return 'unknown';
      }
      if (active.isLocal === true && apiLocal === true) {
        return 'local';
      }
      if (active.isLocal === false && apiLocal === false) {
        return 'remote';
      }
    } catch (err) {
      return 'unknown';
    }
    return 'unknown';
  }

  function deviceScope() {
    // Declared test seam: the generated bundle never defines this global, so a
    // real Spotify session uses the observation above and stays blocked on doubt.
    var value = root.LUNMI_SPOTIFY_KIT_DEVICE_SCOPE;
    if (SCOPES.indexOf(value) >= 0) {
      return value;
    }
    return observedScope();
  }

  function deviceSnapshot(spicetify) {
    // Diagnostic read only: current booleans, no IDs/names/account/tokens. The
    // scope reuses observedScope; unknown is reported, never promoted to local.
    var platform = spicetify && spicetify.Platform;
    var connectActive = null;
    var connectLocal = null;
    var playbackLocal = null;
    try {
      if (
        platform &&
        platform.ConnectAPI &&
        typeof platform.ConnectAPI.getState === 'function'
      ) {
        var state = platform.ConnectAPI.getState();
        var active = state && state.activeDevice;
        if (active && typeof active.isActive === 'boolean') {
          connectActive = active.isActive;
        }
        if (active && typeof active.isLocal === 'boolean') {
          connectLocal = active.isLocal;
        }
      }
    } catch (err) {
      /* A missing/throwing getter stays null; never inferred. */
    }
    try {
      if (platform && platform.PlaybackAPI) {
        var local = platform.PlaybackAPI._isLocal;
        if (typeof local === 'boolean') {
          playbackLocal = local;
        }
      }
    } catch (err) {
      /* same: null, not an invented value */
    }
    return {
      device_scope: observedScope(spicetify),
      connect_active: connectActive,
      connect_local: connectLocal,
      playback_local: playbackLocal
    };
  }

  function probeSupport(spicetify) {
    var p = spicetify && spicetify.Player;
    var platform = spicetify && spicetify.Platform;
    var graphql = spicetify && spicetify.GraphQL;
    var fn = function (name) {
      return !!p && typeof p[name] === 'function';
    };
    var searchReady =
      !!graphql &&
      typeof graphql.Request === 'function' &&
      !!platform &&
      !!platform.History &&
      typeof platform.History.push === 'function';
    return {
      get_player_state:
        fn('getProgress') && fn('getVolume') && fn('isPlaying') &&
        fn('getShuffle') && fn('getRepeat'),
      get_device_scope: !!platform,
      activate_local: !!(
        platform &&
        platform.ConnectAPI &&
        typeof platform.ConnectAPI.pullToLocal === 'function'
      ),
      play: fn('play'),
      pause: fn('pause'),
      set_volume: fn('setVolume') && fn('getVolume'),
      next: fn('next'),
      previous: fn('back'),
      search_tracks: searchReady,
      play_uri: fn('playUri')
    };
  }

  function helpersReady(spicetify) {
    // Existence of the helper surfaces, not proof that they can answer yet. The
    // player itself is probed per command; search and device scope need these.
    // A partially initialized Platform can still throw from a getter; that is
    // "not ready", never a reason to abort the bounded wait.
    try {
      var platform = spicetify && spicetify.Platform;
      var graphql = spicetify && spicetify.GraphQL;
      return !!(
        graphql &&
        typeof graphql.Request === 'function' &&
        platform &&
        platform.History &&
        typeof platform.History.push === 'function' &&
        platform.PlaybackAPI &&
        platform.ConnectAPI &&
        typeof platform.ConnectAPI.getState === 'function'
      );
    } catch (err) {
      return false;
    }
  }

  function supportString(support) {
    var names = ACTIONS.filter(function (action) {
      return support[action] === true;
    });
    names.sort();
    return names.join(',');
  }

  function capabilitiesFor(support, mode, scope) {
    var out = {};
    ACTIONS.forEach(function (action) {
      if (!support[action]) {
        out[action] = 'unsupported';
      } else if (
        action === 'get_player_state' ||
        action === 'get_device_scope' ||
        action === 'search_tracks'
      ) {
        out[action] = 'available';
      } else if (ACTIVATION.indexOf(action) >= 0) {
        // Actionable exactly when it makes sense: control mode, whatever the
        // signed device scope. `available` is capability, not backend approval.
        out[action] = mode === 'control' ? 'available' : 'blocked_read_only';
      } else if (mode !== 'control') {
        out[action] = 'blocked_read_only';
      } else if (scope !== 'local') {
        out[action] = 'blocked_device_scope';
      } else {
        out[action] = 'available';
      }
    });
    return out;
  }

  function capsEqual(left, right) {
    if (!left || typeof left !== 'object' || !right || typeof right !== 'object') {
      return false;
    }
    if (Object.keys(left).length !== ACTIONS.length) {
      return false;
    }
    for (var i = 0; i < ACTIONS.length; i += 1) {
      if (left[ACTIONS[i]] !== right[ACTIONS[i]]) {
        return false;
      }
    }
    return true;
  }

  function clamp01(value) {
    var number = Number(value);
    if (!isFinite(number) || number < 0 || number > 1) {
      return null;
    }
    return number;
  }

  function textOrNull(value) {
    return typeof value === 'string' && value.length > 0 ? value : null;
  }

  function repeatOf(value) {
    if (value === 0 || value === 'off') {
      return 'off';
    }
    if (value === 1 || value === 'track') {
      return 'track';
    }
    if (value === 2 || value === 'context') {
      return 'context';
    }
    return null;
  }

  // Fixed stage tokens. They travel as error `detail` so a failure names where the
  // player surface was not ready without ever forwarding a raw exception.
  var READ_STAGES = ['player', 'volume', 'progress', 'shuffle', 'repeat', 'playing'];
  var SEARCH_STAGES = ['route', 'definition', 'query', 'shape', 'errors'];

  function probeState(spicetify) {
    var p = spicetify && spicetify.Player;
    if (!p) {
      return { state: null, stage: 'player' };
    }
    var required = ['isPlaying', 'getProgress', 'getVolume', 'getShuffle', 'getRepeat'];
    for (var i = 0; i < required.length; i += 1) {
      if (typeof p[required[i]] !== 'function') {
        return { state: null, stage: 'player' };
      }
    }
    // Read in dependency order: PlaybackAPI (volume), then origin._state (progress,
    // shuffle, repeat, playing). A defined method proves nothing until it can answer.
    var value;
    try {
      value = clamp01(p.getVolume());
    } catch (err) {
      return { state: null, stage: 'volume' };
    }
    if (value === null) {
      return { state: null, stage: 'volume' };
    }
    var volume = value;
    try {
      value = Math.round(Number(p.getProgress()));
    } catch (err) {
      return { state: null, stage: 'progress' };
    }
    if (!isFinite(value)) {
      return { state: null, stage: 'progress' };
    }
    var position = value;
    try {
      value = p.getShuffle() === true;
    } catch (err) {
      return { state: null, stage: 'shuffle' };
    }
    var shuffle = value;
    try {
      value = repeatOf(p.getRepeat());
    } catch (err) {
      return { state: null, stage: 'repeat' };
    }
    if (value === null) {
      return { state: null, stage: 'repeat' };
    }
    var repeat = value;
    try {
      value = p.isPlaying() === true;
    } catch (err) {
      return { state: null, stage: 'playing' };
    }
    var playing = value;
    var data = p.data && typeof p.data === 'object' ? p.data : null;
    var item = data && data.item && typeof data.item === 'object' ? data.item : null;
    var uri = item ? textOrNull(item.uri) : null;
    var title = item ? textOrNull(item.name) : null;
    if (!uri || !URI_RE.test(uri) || !title) {
      uri = null;
      title = null;
    }
    var artist = null;
    if (item && Array.isArray(item.artists) && item.artists.length > 0) {
      artist = textOrNull(item.artists[0] && item.artists[0].name);
    }
    var album = item && item.album ? textOrNull(item.album.name) : null;
    return {
      state: {
        track_uri: uri,
        title: title,
        artist: artist,
        album: album,
        is_playing: playing,
        position_ms: Math.max(0, Math.min(86400000, position)),
        volume: volume,
        shuffle: shuffle,
        repeat: repeat
      },
      stage: null
    };
  }

  function readState(spicetify) {
    return probeState(spicetify).state;
  }

  function validArgs(action, args) {
    if (!args || typeof args !== 'object' || Array.isArray(args)) {
      return false;
    }
    var keys = Object.keys(args);
    if (action === 'set_volume') {
      if (keys.length !== 1 || keys[0] !== 'level') {
        return false;
      }
      var level = args.level;
      if (typeof level !== 'number' || !isFinite(level) || level < 0 || level > 1) {
        return false;
      }
      return true;
    }
    if (action === 'search_tracks') {
      if (keys.length !== 2 || keys.indexOf('query') < 0 || keys.indexOf('limit') < 0) {
        return false;
      }
      var query = args.query;
      if (typeof query !== 'string' || query.length < 1 || query.length > SEARCH_QUERY_MAX) {
        return false;
      }
      if (query.trim() === '' || /[\u0000-\u001f\u007f]/.test(query)) {
        return false;
      }
      var limit = args.limit;
      return (
        typeof limit === 'number' &&
        Number.isInteger(limit) &&
        limit >= 1 &&
        limit <= SEARCH_LIMIT_MAX
      );
    }
    if (action === 'get_device_scope') {
      return keys.length === 0;
    }
    if (action === 'activate_local') {
      // Closed: the target is this PC by construction, so no identifier, method,
      // logging object, URL or path may accompany it.
      return keys.length === 0;
    }
    if (action === 'play_uri') {
      return keys.length === 1 && keys[0] === 'uri' && TRACK_URI.test(args.uri || '');
    }
    return keys.length === 0;
  }

  var HELLO_KEYS = ['v', 'type', 'server_nonce', 'epoch', 'auth_deadline_ms', 'mode'];
  var READY_KEYS = [
    'v', 'type', 'id', 'server_proof', 'session', 'epoch', 'mode',
    'device_scope', 'capabilities', 'origin_filter'
  ];
  var COMMAND_KEYS = ['v', 'type', 'id', 'session', 'action', 'args', 'deadline_ms'];
  var ERROR_KEYS = ['v', 'type', 'id', 'code', 'detail'];
  var CAP_STATES = [
    'available', 'blocked_read_only', 'blocked_device_scope', 'unsupported', 'unknown'
  ];

  function isInt(value, low, high) {
    return (
      typeof value === 'number' &&
      isFinite(value) &&
      Math.floor(value) === value &&
      value >= low &&
      value <= high
    );
  }

  function isText(value, limit) {
    return typeof value === 'string' && value.length > 0 && value.length <= limit;
  }

  function exactKeys(object, keys) {
    var actual = Object.keys(object);
    if (actual.length !== keys.length) {
      return false;
    }
    for (var i = 0; i < keys.length; i += 1) {
      if (!Object.prototype.hasOwnProperty.call(object, keys[i])) {
        return false;
      }
    }
    return true;
  }

  function validCapabilities(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      return false;
    }
    if (Object.keys(value).length !== ACTIONS.length) {
      return false;
    }
    for (var i = 0; i < ACTIONS.length; i += 1) {
      if (!Object.prototype.hasOwnProperty.call(value, ACTIONS[i])) {
        return false;
      }
      if (CAP_STATES.indexOf(value[ACTIONS[i]]) < 0) {
        return false;
      }
    }
    return true;
  }

  /*
   * Strict JSON for inbound frames, mirroring the Python side. Rejects duplicate
   * keys at ANY depth (after unescaping: "act\u0069on" collides with "action"),
   * NaN/Infinity literals, trailing content and excessive nesting. A duplicate is
   * never resolved by "last one wins".
   */
  function parseStrictJson(text) {
    var index = 0;
    var length = text.length;

    function fail() {
      throw new Error('STRICT_JSON');
    }

    function peek() {
      return text.charAt(index);
    }

    function skipWs() {
      while (index < length) {
        var code = text.charCodeAt(index);
        if (code === 32 || code === 9 || code === 10 || code === 13) {
          index += 1;
        } else {
          break;
        }
      }
    }

    function expect(word) {
      if (text.substr(index, word.length) !== word) {
        fail();
      }
      index += word.length;
    }

    function parseString() {
      index += 1;
      var out = '';
      while (index < length) {
        var ch = text.charAt(index);
        index += 1;
        if (ch === '"') {
          return out;
        }
        if (ch === '\\') {
          if (index >= length) {
            fail();
          }
          var esc = text.charAt(index);
          index += 1;
          if (esc === '"') {
            out += '"';
          } else if (esc === '\\') {
            out += '\\';
          } else if (esc === '/') {
            out += '/';
          } else if (esc === 'b') {
            out += '\b';
          } else if (esc === 'f') {
            out += '\f';
          } else if (esc === 'n') {
            out += '\n';
          } else if (esc === 'r') {
            out += '\r';
          } else if (esc === 't') {
            out += '\t';
          } else if (esc === 'u') {
            var hex = text.substr(index, 4);
            if (hex.length !== 4 || !/^[0-9a-fA-F]{4}$/.test(hex)) {
              fail();
            }
            out += String.fromCharCode(parseInt(hex, 16));
            index += 4;
          } else {
            fail();
          }
        } else {
          if (ch.charCodeAt(0) < 0x20) {
            fail();
          }
          out += ch;
        }
      }
      fail();
    }

    function parseNumber() {
      var start = index;
      if (peek() === '-') {
        index += 1;
      }
      if (peek() === '0') {
        index += 1;
      } else if (peek() >= '1' && peek() <= '9') {
        while (peek() >= '0' && peek() <= '9') {
          index += 1;
        }
      } else {
        fail();
      }
      if (peek() === '.') {
        index += 1;
        if (!(peek() >= '0' && peek() <= '9')) {
          fail();
        }
        while (peek() >= '0' && peek() <= '9') {
          index += 1;
        }
      }
      if (peek() === 'e' || peek() === 'E') {
        index += 1;
        if (peek() === '+' || peek() === '-') {
          index += 1;
        }
        if (!(peek() >= '0' && peek() <= '9')) {
          fail();
        }
        while (peek() >= '0' && peek() <= '9') {
          index += 1;
        }
      }
      var value = Number(text.slice(start, index));
      if (!isFinite(value)) {
        fail();
      }
      return value;
    }

    function parseObject(depth) {
      index += 1;
      var out = Object.create(null);
      var seen = Object.create(null);
      skipWs();
      if (peek() === '}') {
        index += 1;
        return out;
      }
      while (true) {
        skipWs();
        if (peek() !== '"') {
          fail();
        }
        var name = parseString();
        if (seen[name]) {
          fail();
        }
        seen[name] = true;
        skipWs();
        if (peek() !== ':') {
          fail();
        }
        index += 1;
        out[name] = parseValue(depth);
        skipWs();
        var separator = peek();
        if (separator === ',') {
          index += 1;
          continue;
        }
        if (separator === '}') {
          index += 1;
          return out;
        }
        fail();
      }
    }

    function parseArray(depth) {
      index += 1;
      var out = [];
      skipWs();
      if (peek() === ']') {
        index += 1;
        return out;
      }
      while (true) {
        out.push(parseValue(depth));
        skipWs();
        var separator = peek();
        if (separator === ',') {
          index += 1;
          continue;
        }
        if (separator === ']') {
          index += 1;
          return out;
        }
        fail();
      }
    }

    function parseValue(depth) {
      if (depth > 32) {
        fail();
      }
      skipWs();
      if (index >= length) {
        fail();
      }
      var ch = peek();
      if (ch === '{') {
        return parseObject(depth + 1);
      }
      if (ch === '[') {
        return parseArray(depth + 1);
      }
      if (ch === '"') {
        return parseString();
      }
      if (ch === 't') {
        expect('true');
        return true;
      }
      if (ch === 'f') {
        expect('false');
        return false;
      }
      if (ch === 'n') {
        expect('null');
        return null;
      }
      if (ch === '-' || (ch >= '0' && ch <= '9')) {
        return parseNumber();
      }
      fail();
    }

    var result = parseValue(0);
    skipWs();
    if (index !== length) {
      fail();
    }
    return result;
  }

  function validateIncoming(msg) {
    if (!msg || typeof msg !== 'object' || Array.isArray(msg) || msg.v !== VERSION) {
      return null;
    }
    if (msg.type === 'hello') {
      if (!exactKeys(msg, HELLO_KEYS)) {
        return null;
      }
      if (
        !HEX64.test(msg.server_nonce || '') ||
        !isInt(msg.epoch, 1, 2147483647) ||
        !isInt(msg.auth_deadline_ms, 100, 10000) ||
        MODES.indexOf(msg.mode) < 0
      ) {
        return null;
      }
      return 'hello';
    }
    if (msg.type === 'ready') {
      if (!exactKeys(msg, READY_KEYS)) {
        return null;
      }
      if (
        !ID_RE.test(msg.id || '') ||
        !HEX64.test(msg.server_proof || '') ||
        !HEX32.test(msg.session || '') ||
        !isInt(msg.epoch, 1, 2147483647) ||
        MODES.indexOf(msg.mode) < 0 ||
        SCOPES.indexOf(msg.device_scope) < 0 ||
        msg.origin_filter !== 'kit' ||
        !validCapabilities(msg.capabilities)
      ) {
        return null;
      }
      return 'ready';
    }
    if (msg.type === 'command') {
      if (!exactKeys(msg, COMMAND_KEYS)) {
        return null;
      }
      if (
        !ID_RE.test(msg.id || '') ||
        !HEX32.test(msg.session || '') ||
        ACTIONS.indexOf(msg.action) < 0 ||
        !msg.args ||
        typeof msg.args !== 'object' ||
        Array.isArray(msg.args) ||
        !validArgs(msg.action, msg.args) ||
        !isInt(msg.deadline_ms, 100, 5000)
      ) {
        return null;
      }
      return 'command';
    }
    if (msg.type === 'error') {
      var actual = Object.keys(msg);
      if (actual.length < 4 || actual.length > ERROR_KEYS.length) {
        return null;
      }
      for (var i = 0; i < actual.length; i += 1) {
        if (ERROR_KEYS.indexOf(actual[i]) < 0) {
          return null;
        }
      }
      if (
        !Object.prototype.hasOwnProperty.call(msg, 'id') ||
        !Object.prototype.hasOwnProperty.call(msg, 'code') ||
        !(msg.id === '' || ID_RE.test(msg.id)) ||
        !isText(msg.code, 64) ||
        (msg.detail !== undefined && !isText(msg.detail, 120))
      ) {
        return null;
      }
      return 'error';
    }
    return null;
  }

  function createClient(options) {
    var opts = options || {};
    var config = configFrom(opts.config) || readConfig();
    var createSocket =
      opts.createSocket ||
      function (url) {
        return new root.WebSocket(url);
      };
    var autoReconnect = opts.autoReconnect !== false;
    var counters = {
      commands_received: 0,
      commands_accepted: 0,
      results_sent: 0,
      mutations_executed: 0,
      activation_calls: 0,
      blocked_read_only: 0,
      blocked_device_scope: 0,
      duplicate_commands: 0,
      busy_commands: 0,
      results_dropped: 0,
      preauth_commands: 0,
      server_proof_failures: 0,
      protocol_errors: 0,
      duplicate_hellos: 0,
      reconnects: 0,
      startup_retries: 0,
      session_refreshes: 0
    };

    var socket = null;
    var status = config ? 'idle' : 'unconfigured';
    var key = null;
    var serverNonce = null;
    var clientNonce = null;
    var epoch = null;
    var authId = null;
    var session = null;
    var mode = 'read_only';
    var scope = 'unknown';
    var support = {};
    var supportText = '';
    var expectedCaps = null;
    var reconnectAttempts = 0;
    var startupRetries = 0;
    // Terminal auth/proof/mode/role/protocol failures never re-arm by wake/reconnect.
    var latched = false;
    var wakeListeners = [];
    var startupRefreshes = 0;
    var observationTimer = null;
    var reconnectTimer = null;
    var connectTimer = null;
    var playerWaitTimer = null;
    var warmUpTimer = null;
    var warmUpSettler = null;
    var seenIds = {};
    var seenOrder = [];
    var disposed = false;
    var lastError = null;
    var lastResult = null;
    var connectInfo = null;
    var pendingConnect = null;
    var activeOp = null;
    var generation = 0;

    function clearConnectTimer() {
      if (connectTimer !== null) {
        clearTimeout(connectTimer);
        connectTimer = null;
      }
    }

    function warmUp() {
      var spicetify = opts.spicetify || root.Spicetify;
      if (helpersReady(spicetify)) {
        return Promise.resolve();
      }
      var deadlineAt = nowMs() + WARMUP_MS;
      return new Promise(function (resolve, reject) {
        function finish(error) {
          if (warmUpTimer !== null) {
            clearTimeout(warmUpTimer);
            warmUpTimer = null;
          }
          warmUpSettler = null;
          if (error) {
            reject(error);
          } else {
            resolve();
          }
        }
        function tick() {
          warmUpTimer = null;
          if (disposed) {
            finish(new Error('CLIENT_DISCONNECTED'));
            return;
          }
          if (
            helpersReady(opts.spicetify || root.Spicetify) ||
            nowMs() >= deadlineAt
          ) {
            finish();
            return;
          }
          warmUpTimer = setTimeout(
            tick,
            Math.max(1, Math.min(WARMUP_INTERVAL_MS, deadlineAt - nowMs()))
          );
        }
        warmUpSettler = finish;
        tick();
      });
    }

    function send(payload) {
      if (!socket) {
        return;
      }
      socket.send(JSON.stringify(payload));
    }

    function closeSocket(code, reason) {
      if (socket && typeof socket.close === 'function') {
        try {
          socket.close(code || 1000, reason || '');
        } catch (err) {
          /* already closed */
        }
      }
    }

    function settleConnect(error, value) {
      var pending = pendingConnect;
      pendingConnect = null;
      clearConnectTimer();
      if (!pending) {
        return;
      }
      if (error) {
        pending.reject(error);
      } else {
        pending.resolve(value);
      }
    }

    // A cold bridge can refuse the socket or answer CLIENT_BUSY while the old
    // session is being torn down. Those are transport/startup conditions, not a
    // failed authentication: only they are retried, and only inside the startup
    // window. AUTH_FAILED/REPLAY/PROOF/MODE/PROTOCOL errors stay terminal.
    function transientConnectFailure(code) {
      return (
        code === 'CLOSED_BEFORE_READY' ||
        code === 'SOCKET_ERROR' ||
        code === 'AUTH_TIMEOUT' ||
        code === 'CLIENT_BUSY'
      );
    }

    function fail(code) {
      lastError = code;
      clearConnectTimer();
      if (!transientConnectFailure(code)) {
        latched = true;
      }
      if (status === 'connecting') {
        status = 'failed';
        closeSocket(1008, code);
        settleConnect(new Error(code));
        if (transientConnectFailure(code)) {
          scheduleStartupRetry();
        }
      }
    }

    function scheduleStartupRetry() {
      if (!autoReconnect || disposed || !config || reconnectTimer !== null || latched) {
        return;
      }
      var delay = Math.min(
        BACKOFF_MS[Math.min(startupRetries, BACKOFF_MS.length - 1)],
        TRANSPORT_RECONNECT_CEILING_MS
      );
      startupRetries += 1;
      counters.startup_retries += 1;
      status = 'reconnecting';
      reconnectTimer = setTimeout(function () {
        reconnectTimer = null;
        if (disposed) {
          return;
        }
        connectInternal(config.url).catch(function () {
          /* the failure path above already schedules the next transport attempt */
        });
      }, delay);
    }

    function protocolError() {
      counters.protocol_errors += 1;
      var wasConnecting = status === 'connecting';
      latched = true;
      if (wasConnecting) {
        status = 'failed';
      } else if (status === 'authenticated') {
        status = 'closed';
      }
      closeSocket(1008, 'PROTOCOL_ERROR');
      if (wasConnecting) {
        settleConnect(new Error('PROTOCOL_ERROR'));
      }
    }

    function bytesOf(text) {
      return encoder.encode(text).length;
    }

    async function onHello(msg) {
      if (status !== 'connecting' || serverNonce !== null) {
        counters.duplicate_hellos += 1;
        protocolError();
        return;
      }
      if (
        !HEX64.test(msg.server_nonce || '') ||
        !Number.isInteger(msg.epoch) ||
        msg.epoch < 1 ||
        MODES.indexOf(msg.mode) < 0
      ) {
        protocolError();
        return;
      }
      var spicetify = opts.spicetify || root.Spicetify;
      support = probeSupport(spicetify);
      supportText = supportString(support);
      mode = msg.mode;
      scope = deviceScope();
      expectedCaps = capabilitiesFor(support, mode, scope);
      serverNonce = msg.server_nonce;
      epoch = msg.epoch;
      clientNonce = randomHex(32);
      authId = 'auth-' + clientNonce.slice(0, 12);
      var proof = await sign(
        key,
        macInput('client', serverNonce, clientNonce, epoch, mode, scope, supportText)
      );
      send({
        v: VERSION,
        type: 'auth',
        id: authId,
        client_nonce: clientNonce,
        client_proof: proof,
        role: 'client',
        mode: mode,
        device_scope: scope,
        support: supportText
      });
    }

    async function onReady(msg) {
      if (status !== 'connecting' || clientNonce === null) {
        protocolError();
        return;
      }
      if (
        msg.id !== authId ||
        msg.epoch !== epoch ||
        !HEX32.test(msg.session || '') ||
        msg.mode !== mode ||
        msg.device_scope !== scope ||
        msg.origin_filter !== 'kit' ||
        !capsEqual(msg.capabilities, expectedCaps)
      ) {
        protocolError();
        return;
      }
      var ok = await verify(
        key,
        macInput('server', serverNonce, clientNonce, epoch, mode, scope, supportText),
        msg.server_proof
      );
      if (!ok) {
        counters.server_proof_failures += 1;
        latched = true;
        status = 'rejected';
        closeSocket(1008, 'SERVER_PROOF_INVALID');
        settleConnect(new Error('SERVER_PROOF_INVALID'));
        return;
      }
      session = msg.session;
      status = 'authenticated';
      latched = false;
      reconnectAttempts = 0;
      startupRetries = 0;
      clearConnectTimer();
      connectInfo = {
        session: session,
        epoch: epoch,
        mode: mode,
        device_scope: scope,
        capabilities: msg.capabilities,
        support: supportText
      };
      settleConnect(null, connectInfo);
      // A cold Spotify finishes loading after this signature; watch the same two
      // live signals for a bounded window and re-sign if they improve.
      scheduleStartupObservation();
    }

    function nowMs() {
      return Date.now();
    }

    function waitUntil(predicate, deadlineAt) {
      return new Promise(function (resolve) {
        (function tick() {
          var ok = false;
          try {
            ok = predicate() === true;
          } catch (err) {
            ok = false;
          }
          if (ok) {
            resolve(true);
            return;
          }
          if (disposed || nowMs() >= deadlineAt) {
            resolve(false);
            return;
          }
          setTimeout(tick, 25);
        })();
      });
    }

    function player() {
      var spicetify = opts.spicetify || root.Spicetify;
      return spicetify && spicetify.Player ? spicetify.Player : null;
    }

    function stageError(code, stage) {
      var err = new Error(code);
      err.kitCode = code;
      err.stage = stage;
      return err;
    }

    function sleep(ms) {
      return new Promise(function (resolve) {
        setTimeout(resolve, ms);
      });
    }

    // One bounded wait, one absolute deadline, no retry: poll the same read path that
    // will answer the command until it is readable or the command budget is spent.
    async function waitForState(spicetify, deadlineAt, op) {
      var probe = { state: null, stage: 'player' };
      while (true) {
        if (disposed || (op && op.cancelled)) {
          return probe;
        }
        probe = probeState(spicetify);
        if (probe.state || nowMs() >= deadlineAt) {
          return probe;
        }
        await sleep(Math.max(1, Math.min(25, deadlineAt - nowMs())));
      }
    }

    function fixedText(value, limit) {
      if (typeof value !== 'string') return null;
      var text = value.replace(/[\u0000-\u001f\u007f]/g, '').trim();
      if (!text || text.length > limit) return null;
      return text;
    }

    // Exact shape from the addendum (xpui-modules module 77145):
    // tracksV2.items[].item.data {__typename:Track, uri, name,
    // artists.items[].profile.name, albumOfTrack.name, playability.playable}.
    // One unexpected entry fails the whole answer; unplayable tracks are omitted,
    // never invented or promoted. No other shape is accepted.
    function parseSearchItems(payload, limit) {
      var body = payload && payload.data;
      var tracksV2 = body && body.searchV2 && body.searchV2.tracksV2;
      var items = tracksV2 && tracksV2.items;
      if (!Array.isArray(items)) return null;
      var out = [];
      var cap = Math.min(limit, SEARCH_RESULT_MAX);
      for (var i = 0; i < items.length && out.length < cap; i += 1) {
        var entry = items[i];
        var item = entry && entry.item && entry.item.data;
        if (!item || typeof item !== 'object' || item.__typename !== 'Track') {
          return null;
        }
        var uri = fixedText(item.uri, 120);
        if (!uri || !TRACK_URI.test(uri)) return null;
        var title = fixedText(item.name, 300);
        if (!title) return null;
        var artists = item.artists && item.artists.items;
        if (!Array.isArray(artists) || artists.length === 0) return null;
        var profile = artists[0] && artists[0].profile;
        var artist = fixedText(profile && profile.name, 200);
        if (!artist) return null;
        var albumOfTrack = item.albumOfTrack;
        var album = fixedText(albumOfTrack && albumOfTrack.name, 200);
        if (!album) return null;
        var playability = item.playability;
        if (!playability || typeof playability.playable !== 'boolean') return null;
        if (playability.playable !== true) continue;
        out.push({ uri: uri, title: title, artist: artist, album: album });
      }
      return out;
    }

    function settleBy(promise, deadlineAt) {
      return new Promise(function (resolve) {
        var done = false;
        var timer = null;
        function finish(value) {
          if (done) return;
          done = true;
          if (timer !== null) clearTimeout(timer);
          resolve(value);
        }
        if (!promise || typeof promise.then !== 'function') {
          finish({ value: promise, failed: false, timedOut: false });
          return;
        }
        timer = setTimeout(function () {
          finish({ value: null, failed: false, timedOut: true });
        }, Math.max(0, deadlineAt - nowMs()));
        promise.then(
          function (value) {
            finish({ value: value, failed: false, timedOut: false });
          },
          function () {
            finish({ value: null, failed: true, timedOut: false });
          }
        );
      });
    }

    async function searchTracks(spicetify, args, deadlineAt, op) {
      var platform = spicetify && spicetify.Platform;
      var graphql = spicetify && spicetify.GraphQL;
      if (!platform || !platform.History || typeof platform.History.push !== 'function') {
        throw stageError('SEARCH_UNAVAILABLE', 'route');
      }
      if (!graphql || typeof graphql.Request !== 'function') {
        throw stageError('SEARCH_UNAVAILABLE', 'query');
      }
      var definitions = graphql.Definitions || {};
      if (!definitions.searchTracks) {
        try {
          platform.History.push('/search/' + encodeURIComponent(args.query));
        } catch (err) {
          throw stageError('SEARCH_UNAVAILABLE', 'route');
        }
        while (!definitions.searchTracks) {
          if (disposed || (op && op.cancelled)) {
            throw stageError('SEARCH_UNAVAILABLE', 'definition');
          }
          if (nowMs() >= deadlineAt) {
            throw stageError('SEARCH_UNAVAILABLE', 'definition');
          }
          await sleep(Math.max(1, Math.min(25, deadlineAt - nowMs())));
          // The fence is after the suspension point: a disconnect during the
          // sleep must never reach the Query below.
          if (disposed || (op && op.cancelled)) {
            throw stageError('SEARCH_UNAVAILABLE', 'definition');
          }
          definitions = graphql.Definitions || {};
        }
      }
      if (disposed || (op && op.cancelled)) {
        throw stageError('SEARCH_UNAVAILABLE', 'definition');
      }
      var request = null;
      try {
        request = graphql.Request(definitions.searchTracks, {
          searchTerm: args.query,
          offset: 0,
          limit: args.limit,
          includePreReleases: false,
          includeAlbumPreReleases: false,
          numberOfTopResults: 20
        });
      } catch (err) {
        throw stageError('SEARCH_FAILED', 'query');
      }
      var response = await settleBy(request, deadlineAt);
      if (response.timedOut || response.failed) {
        throw stageError('SEARCH_FAILED', 'query');
      }
      var payload = response.value;
      var errors = payload && typeof payload === 'object' ? payload.errors : undefined;
      if (errors !== undefined) {
        // Present but malformed or non-empty: the answer is partial/uncertain even
        // if data looks valid. Never promote it to a list, never echo the messages.
        if (!Array.isArray(errors) || errors.length > 0) {
          throw stageError('SEARCH_FAILED', 'errors');
        }
      }
      var tracks = parseSearchItems(payload, args.limit);
      if (tracks === null) {
        throw stageError('SEARCH_FAILED', 'shape');
      }
      return { tracks: tracks };
    }

    function assertLocalDevice() {
      if (deviceScope() !== 'local') {
        throw stageError('DEVICE_SCOPE_BLOCKED', 'device');
      }
    }

    // Fence evaluated immediately before the one activation call: an accepted
    // command from another socket/session/generation, a cancellation or a lost
    // authentication must never reach pullToLocal.
    function activationAllowed(op) {
      return !(
        disposed ||
        status !== 'authenticated' ||
        !op ||
        op.cancelled ||
        op.socket !== socket ||
        op.session !== session ||
        op.generation !== generation
      );
    }

    // ONE exposed native call, no arguments, no retry. Its Promise is ignored on
    // purpose: a resolved Promise<void> is not proof that this PC became active.
    // Only the same conservative observation used at auth can verify it.
    async function activateLocal(spicetify, deadlineAt, op) {
      if (!activationAllowed(op)) {
        throw stageError('ACTIVATION_ABORTED', 'device');
      }
      var platform = spicetify && spicetify.Platform;
      var connect = platform && platform.ConnectAPI;
      if (!connect || typeof connect.pullToLocal !== 'function') {
        throw stageError('CAPABILITY_UNSUPPORTED', 'action');
      }
      counters.activation_calls += 1;
      var acknowledgement = null;
      try {
        acknowledgement = connect.pullToLocal();
      } catch (err) {
        // The single attempt failed before returning; never repeat it.
        throw stageError('ACTIVATION_FAILED', 'device');
      }
      if (acknowledgement && typeof acknowledgement.then === 'function') {
        // A rejection/timeout here is not a second attempt and not a verdict.
        acknowledgement.then(null, function () {});
      }
      var local = await waitUntil(function () {
        return observedScope(spicetify) === 'local';
      }, deadlineAt);
      if (!local || !activationAllowed(op)) {
        return null;
      }
      var snapshot = deviceSnapshot(spicetify);
      return snapshot.device_scope === 'local' ? snapshot : null;
    }

    async function perform(action, args, deadlineAt, op) {
      var spicetify = opts.spicetify || root.Spicetify;
      if (action === 'search_tracks') {
        return await searchTracks(spicetify, args, deadlineAt, op);
      }
      if (action === 'get_device_scope') {
        return deviceSnapshot(spicetify);
      }
      if (action === 'activate_local') {
        return await activateLocal(spicetify, deadlineAt, op);
      }
      var p = player();
      if (!p) {
        throw stageError('PLAYER_UNAVAILABLE', 'player');
      }
      var probe = await waitForState(spicetify, deadlineAt, op);
      if (!probe.state) {
        // Unreadable at the deadline is a failure with a stage, never a fake state.
        throw stageError('PLAYER_NOT_READY', probe.stage);
      }
      var state = probe.state;
      if (action === 'get_player_state') {
        return state;
      }
      if (action === 'play_uri') {
        assertLocalDevice();
        var targetUri = args.uri;
        try {
          var playing = p.playUri(targetUri);
          counters.mutations_executed += 1;
          if (playing && typeof playing.then === 'function') {
            // The observation decides; a late rejection is not a second attempt.
            playing.then(null, function () {});
          }
        } catch (err) {
          throw stageError('PLAYER_ERROR', 'playing');
        }
        var reached = await waitUntil(function () {
          var next = readState(opts.spicetify || root.Spicetify);
          return !!next && next.track_uri === targetUri && next.is_playing === true;
        }, deadlineAt);
        return reached ? readState(opts.spicetify || root.Spicetify) : null;
      }
      if (action === 'play') {
        assertLocalDevice();
        if (state && state.is_playing) {
          return state;
        }
        p.play();
        counters.mutations_executed += 1;
        var playing = await waitUntil(function () {
          var next = readState(opts.spicetify || root.Spicetify);
          return !!next && next.is_playing === true;
        }, deadlineAt);
        return playing ? readState(opts.spicetify || root.Spicetify) : null;
      }
      if (action === 'pause') {
        assertLocalDevice();
        if (state && state.is_playing === false) {
          return state;
        }
        p.pause();
        counters.mutations_executed += 1;
        var paused = await waitUntil(function () {
          var next = readState(opts.spicetify || root.Spicetify);
          return !!next && next.is_playing === false;
        }, deadlineAt);
        return paused ? readState(opts.spicetify || root.Spicetify) : null;
      }
      if (action === 'set_volume') {
        assertLocalDevice();
        p.setVolume(args.level);
        counters.mutations_executed += 1;
        var reached = await waitUntil(function () {
          var current = clamp01(p.getVolume());
          return current !== null && Math.abs(current - args.level) <= VOLUME_TOLERANCE;
        }, deadlineAt);
        return reached ? readState(opts.spicetify || root.Spicetify) : null;
      }
      if (action === 'next' || action === 'previous') {
        assertLocalDevice();
        var before = state ? state.track_uri : null;
        var beforePosition = state ? state.position_ms : null;
        if (action === 'next') {
          p.next();
        } else {
          p.back();
        }
        counters.mutations_executed += 1;
        var changed = await waitUntil(function () {
          var next = readState(opts.spicetify || root.Spicetify);
          if (!next || next.track_uri === null) {
            return false;
          }
          if (next.track_uri !== before) {
            return true;
          }
          // `previous` may restart the same track. Only `previous` accepts that,
          // and only from the position actually observed before the one `back`;
          // `next` keeps URI-only verification.
          if (action !== 'previous' || before === null || beforePosition === null) {
            return false;
          }
          return (
            beforePosition >= RESTART_BEFORE_MS &&
            next.position_ms <= RESTART_AFTER_MS &&
            beforePosition - next.position_ms >= RESTART_RECUO_MS
          );
        }, deadlineAt);
        return changed ? readState(opts.spicetify || root.Spicetify) : null;
      }
      throw stageError('CAPABILITY_UNSUPPORTED', 'action');
    }

    function sendResult(op, statusValue, data, observed, started, evidenceKind) {
      // A result belongs to the socket/session/generation where it was born.
      if (
        !op ||
        op.cancelled ||
        disposed ||
        op.socket !== socket ||
        op.session !== session ||
        op.generation !== generation ||
        status !== 'authenticated'
      ) {
        counters.results_dropped += 1;
        return;
      }
      var payload = {
        v: VERSION,
        type: 'result',
        id: op.id,
        session: op.session,
        status: statusValue,
        data: data,
        evidence: { kind: evidenceKind || 'player_state', observed: observed },
        duration_ms: Math.max(0, nowMs() - started)
      };
      lastResult = payload;
      counters.results_sent += 1;
      send(payload);
      if (typeof opts.onResult === 'function') {
        opts.onResult(payload);
      }
    }

    function remember(id) {
      seenIds[id] = true;
      seenOrder.push(id);
      while (seenOrder.length > MAX_SEEN_IDS) {
        var old = seenOrder.shift();
        delete seenIds[old];
      }
    }

    async function onCommand(msg) {
      counters.commands_received += 1;
      if (status !== 'authenticated') {
        counters.preauth_commands += 1;
        return;
      }
      if (msg.session !== session || !ID_RE.test(msg.id || '')) {
        protocolError();
        return;
      }
      var action = msg.action;
      if (ACTIONS.indexOf(action) < 0) {
        send({ v: VERSION, type: 'error', id: msg.id, code: 'CAPABILITY_UNSUPPORTED' });
        return;
      }
      if (seenIds[msg.id]) {
        counters.duplicate_commands += 1;
        send({ v: VERSION, type: 'error', id: msg.id, code: 'DUPLICATE_ID' });
        return;
      }
      if (activeOp) {
        counters.busy_commands += 1;
        send({ v: VERSION, type: 'error', id: msg.id, code: 'BUSY' });
        return;
      }
      if (!support[action]) {
        send({ v: VERSION, type: 'error', id: msg.id, code: 'CAPABILITY_UNSUPPORTED' });
        return;
      }
      if (ACTIVATION.indexOf(action) >= 0 && mode !== 'control') {
        counters.blocked_read_only += 1;
        send({ v: VERSION, type: 'error', id: msg.id, code: 'MODE_READ_ONLY' });
        return;
      }
      if (MUTATIONS.indexOf(action) >= 0) {
        if (mode !== 'control') {
          counters.blocked_read_only += 1;
          send({ v: VERSION, type: 'error', id: msg.id, code: 'MODE_READ_ONLY' });
          return;
        }
        if (scope !== 'local') {
          counters.blocked_device_scope += 1;
          send({ v: VERSION, type: 'error', id: msg.id, code: 'DEVICE_SCOPE_BLOCKED' });
          return;
        }
      }
      if (!validArgs(action, msg.args)) {
        send({ v: VERSION, type: 'error', id: msg.id, code: 'BAD_ARGS' });
        return;
      }
      remember(msg.id);
      counters.commands_accepted += 1;
      var started = nowMs();
      var budgetCap =
        action === 'search_tracks'
          ? SEARCH_BUDGET_MS
          : action === 'activate_local'
            ? ACTIVATION_BUDGET_MS
            : EFFECT_BUDGET_MS;
      var budget = Math.max(50, Math.min(budgetCap, msg.deadline_ms - 50));
      var op = {
        id: msg.id,
        session: session,
        epoch: epoch,
        socket: socket,
        generation: generation,
        cancelled: false
      };
      activeOp = op;
      try {
        var state = await perform(action, msg.args, started + budget, op);
        if (state) {
          var evidenceKind =
            action === 'search_tracks'
              ? 'effect'
              : action === 'get_device_scope' || action === 'activate_local'
                ? 'device_scope'
                : 'player_state';
          sendResult(op, 'SUCCESS_VERIFIED', state, true, started, evidenceKind);
        } else {
          // A failed activation still reports the surface it tried to observe.
          sendResult(
            op,
            'NOT_VERIFIED',
            null,
            false,
            started,
            action === 'activate_local' ? 'device_scope' : undefined
          );
        }
      } catch (err) {
        if (op.cancelled || op.socket !== socket || op.session !== session) {
          counters.results_dropped += 1;
          return;
        }
        var code = 'PLAYER_ERROR';
        var detail;
        if (err && err.kitCode === 'PLAYER_UNAVAILABLE') {
          code = 'PLAYER_UNAVAILABLE';
        } else if (err && err.kitCode === 'PLAYER_NOT_READY') {
          code = 'PLAYER_NOT_READY';
          detail = READ_STAGES.indexOf(err.stage) >= 0 ? err.stage : 'player';
        } else if (err && err.kitCode === 'CAPABILITY_UNSUPPORTED') {
          code = 'CAPABILITY_UNSUPPORTED';
        } else if (err && err.kitCode === 'SEARCH_UNAVAILABLE') {
          code = 'SEARCH_UNAVAILABLE';
          detail = SEARCH_STAGES.indexOf(err.stage) >= 0 ? err.stage : 'definition';
        } else if (err && err.kitCode === 'SEARCH_FAILED') {
          code = 'SEARCH_FAILED';
          detail = SEARCH_STAGES.indexOf(err.stage) >= 0 ? err.stage : 'shape';
        } else if (err && err.kitCode === 'DEVICE_SCOPE_BLOCKED') {
          code = 'DEVICE_SCOPE_BLOCKED';
        } else if (err && err.kitCode === 'ACTIVATION_FAILED') {
          code = 'ACTIVATION_FAILED';
        } else if (err && err.kitCode === 'ACTIVATION_ABORTED') {
          code = 'CANCELLED';
        }
        var frame = { v: VERSION, type: 'error', id: op.id, code: code };
        if (detail) {
          frame.detail = detail;
        }
        send(frame);
      } finally {
        if (activeOp === op) {
          activeOp = null;
        }
      }
    }

    async function onFrame(raw) {
      if (typeof raw !== 'string') {
        counters.protocol_errors += 1;
        return;
      }
      if (bytesOf(raw) > MAX_FRAME) {
        protocolError();
        return;
      }
      var msg;
      try {
        msg = parseStrictJson(raw);
      } catch (err) {
        protocolError();
        return;
      }
      var kind = validateIncoming(msg);
      if (kind === null) {
        protocolError();
        return;
      }
      if (kind === 'hello') {
        await onHello(msg);
        return;
      }
      if (kind === 'ready') {
        await onReady(msg);
        return;
      }
      if (kind === 'command') {
        await onCommand(msg);
        return;
      }
      if (msg.code === 'CLIENT_BUSY') {
        // Outro socket ainda ligado ao host: é transporte, tenta de novo espaçado.
        status = 'closed';
        closeSocket(1013, 'CLIENT_BUSY');
        scheduleReconnect();
        return;
      }
      if (
        msg.code === 'AUTH_FAILED' ||
        msg.code === 'AUTH_REPLAY' ||
        msg.code === 'MODE_MISMATCH' ||
        msg.code === 'ROLE_VIOLATION'
      ) {
        latched = true;
        if (status === 'authenticated') {
          status = 'closed';
          closeSocket(1008, msg.code);
        }
        fail(msg.code);
        return;
      }
      lastError = msg.code;
    }

    function scheduleReconnect() {
      if (!autoReconnect || disposed || !config || latched) {
        status = 'closed';
        return;
      }
      var delay = Math.min(
        BACKOFF_MS[Math.min(reconnectAttempts, BACKOFF_MS.length - 1)],
        TRANSPORT_RECONNECT_CEILING_MS
      );
      reconnectAttempts += 1;
      counters.reconnects += 1;
      status = 'reconnecting';
      reconnectTimer = setTimeout(function () {
        reconnectTimer = null;
        if (disposed) {
          return;
        }
        connectInternal(config.url).catch(function () {
          /* the close handler schedules the next transport attempt */
        });
      }, delay);
    }

    // Acorda só o transporte adiado (foco/online/visível). Nunca rearma um erro
    // latched, nunca repete o comando incerto: um novo handshake não tem pendências.
    function wakeTransport() {
      if (disposed || latched || !autoReconnect || !config) {
        return;
      }
      if (status !== 'reconnecting' || reconnectTimer === null) {
        return;
      }
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
      reconnectAttempts = 0;
      startupRetries = 0;
      connectInternal(config.url).catch(function () {
        /* falha volta a agendar; terminal continua latched */
      });
    }

    function installWakeListeners() {
      function listen(target, type, handler) {
        if (!target || typeof target.addEventListener !== 'function') {
          return;
        }
        target.addEventListener(type, handler);
        wakeListeners.push([target, type, handler]);
      }
      listen(root, 'online', function () {
        wakeTransport();
      });
      listen(root, 'focus', function () {
        wakeTransport();
      });
      listen(root.document || null, 'visibilitychange', function () {
        if (root.document && root.document.visibilityState === 'visible') {
          wakeTransport();
        }
      });
    }

    function clearWakeListeners() {
      for (var i = 0; i < wakeListeners.length; i += 1) {
        var entry = wakeListeners[i];
        try {
          entry[0].removeEventListener(entry[1], entry[2]);
        } catch (err) {
          /* target already gone */
        }
      }
      wakeListeners = [];
    }

    installWakeListeners();

    // The signed snapshot is authoritative for its own session and is never
    // mutated. When the local observation catches up with a cold Spotify, the
    // only honest way to use it is a new handshake: new nonces, new epoch, new
    // proof, new signed scope/capabilities. Nothing is promoted by guesswork.
    function signedSnapshotIncomplete() {
      if (mode === 'control' && scope !== 'local') {
        return true;
      }
      for (var i = 0; i < ACTIONS.length; i += 1) {
        if (support[ACTIONS[i]] !== true) {
          return true;
        }
      }
      return false;
    }

    function observationImproved(observedSupport) {
      // Strictly better only: a scope that now proves local in control mode, or a
      // helper surface that now exists. Unknown/lost signals never refresh, so a
      // degraded observation can never rewrite what was signed.
      if (mode === 'control' && scope !== 'local' && deviceScope() === 'local') {
        return true;
      }
      for (var i = 0; i < ACTIONS.length; i += 1) {
        if (observedSupport[ACTIONS[i]] === true && support[ACTIONS[i]] !== true) {
          return true;
        }
      }
      return false;
    }

    function scheduleStartupObservation() {
      if (!autoReconnect || disposed || observationTimer !== null) {
        return;
      }
      if (startupRefreshes >= MAX_SESSION_REFRESHES || !signedSnapshotIncomplete()) {
        return;
      }
      var deadlineAt = nowMs() + STARTUP_OBSERVE_MS;
      observationTimer = setTimeout(function observe() {
        observationTimer = null;
        if (disposed || status !== 'authenticated') {
          return;
        }
        if (startupRefreshes >= MAX_SESSION_REFRESHES || nowMs() >= deadlineAt) {
          return;
        }
        var observed = null;
        try {
          observed = probeSupport(opts.spicetify || root.Spicetify);
        } catch (err) {
          observed = null;
        }
        // Never interrupt an accepted command: its result belongs to this session
        // and a refresh would discard it. The next tick retries the decision.
        if (observed && !activeOp && observationImproved(observed)) {
          refreshSignedSession();
          return;
        }
        observationTimer = setTimeout(observe, STARTUP_OBSERVE_INTERVAL_MS);
      }, STARTUP_OBSERVE_INTERVAL_MS);
    }

    function refreshSignedSession() {
      if (disposed || !autoReconnect || status !== 'authenticated' || activeOp) {
        return false;
      }
      if (reconnectTimer !== null || startupRefreshes >= MAX_SESSION_REFRESHES) {
        return false;
      }
      startupRefreshes += 1;
      counters.session_refreshes += 1;
      clearConnectTimer();
      status = 'reconnecting';
      var previous = socket;
      socket = null;
      if (previous) {
        try {
          previous.close(1000, 'session_refresh');
        } catch (err) {
          /* already closed */
        }
      }
      reconnectTimer = setTimeout(function () {
        reconnectTimer = null;
        if (disposed) {
          return;
        }
        connectInternal(config.url).catch(function () {
          /* the failure path above already scheduled the next bounded attempt */
        });
      }, BACKOFF_MS[0]);
      return true;
    }

    async function connectInternal(url) {
      key = await importSecret(config.secretHex);
      if (disposed) {
        throw new Error('CLIENT_DISCONNECTED');
      }
      generation += 1;
      var thisGeneration = generation;
      status = 'connecting';
      serverNonce = null;
      clientNonce = null;
      epoch = null;
      authId = null;
      session = null;
      support = {};
      supportText = '';
      expectedCaps = null;
      // The server's auth deadline starts at hello, so the bounded warm-up runs
      // before the socket exists. On timeout the socket still opens with the
      // capabilities actually observed; nothing is promoted or invented.
      await warmUp();
      if (disposed) {
        throw new Error('CLIENT_DISCONNECTED');
      }
      return new Promise(function (resolve, reject) {
        pendingConnect = { resolve: resolve, reject: reject };
        socket = createSocket(url);
        connectTimer = setTimeout(function () {
          fail('AUTH_TIMEOUT');
        }, 3000);

        socket.addEventListener('open', function () {
          /* the server speaks first with hello */
        });

        socket.addEventListener('message', function (event) {
          onFrame(event.data).catch(function () {
            protocolError();
          });
        });

        var thisSocket = socket;
        socket.addEventListener('close', function () {
          clearConnectTimer();
          if (activeOp && activeOp.socket === thisSocket) {
            activeOp.cancelled = true;
            activeOp = null;
            counters.results_dropped += 1;
          }
          if (status === 'connecting' && thisGeneration === generation) {
            lastError = 'CLOSED_BEFORE_READY';
            status = 'failed';
            settleConnect(new Error('CLOSED_BEFORE_READY'));
            scheduleStartupRetry();
          } else if (status === 'authenticated' && thisGeneration === generation) {
            if (socket === thisSocket) {
              socket = null;
            }
            scheduleReconnect();
          }
        });

        socket.addEventListener('error', function () {
          if (status === 'connecting') {
            fail('SOCKET_ERROR');
          }
        });
      });
    }

    async function connect(url) {
      if (!config) {
        throw new Error('UNCONFIGURED');
      }
      if (status === 'connecting' || status === 'authenticated' || status === 'reconnecting') {
        throw new Error('ALREADY_CONNECTED');
      }
      // An explicit connect starts a new bounded startup episode; automatic
      // retries/refreshes keep the budget of the episode they belong to. The
      // attempts are reset here only; a previous terminal latch is cleared by an
      // explicit human/startup connect, never by a timer.
      startupRefreshes = 0;
      startupRetries = 0;
      latched = false;
      return connectInternal(url || config.url);
    }

    function start() {
      if (!config) {
        status = 'unconfigured';
        return Promise.reject(new Error('UNCONFIGURED'));
      }
      if (status !== 'idle') {
        return Promise.resolve(connectInfo);
      }
      status = 'waiting';
      return new Promise(function (resolve, reject) {
        var started = nowMs();
        (function poll() {
          var spicetify = opts.spicetify || root.Spicetify;
          if (spicetify && spicetify.Player) {
            connect(config.url).then(resolve, reject);
            return;
          }
          if (disposed || nowMs() - started >= PLAYER_WAIT_MS) {
            status = 'no-spicetify';
            reject(new Error('NO_SPICETIFY'));
            return;
          }
          playerWaitTimer = setTimeout(poll, 100);
        })();
      });
    }

    function disconnect() {
      disposed = true;
      clearWakeListeners();
      if (activeOp) {
        activeOp.cancelled = true;
        activeOp = null;
        counters.results_dropped += 1;
      }
      clearConnectTimer();
      if (observationTimer !== null) {
        clearTimeout(observationTimer);
        observationTimer = null;
      }
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      if (playerWaitTimer !== null) {
        clearTimeout(playerWaitTimer);
        playerWaitTimer = null;
      }
      if (warmUpSettler) {
        warmUpSettler(new Error('CLIENT_DISCONNECTED'));
      }
      if (warmUpTimer !== null) {
        clearTimeout(warmUpTimer);
        warmUpTimer = null;
      }
      if (socket) {
        closeSocket(1000, 'client_disconnect');
      }
      socket = null;
      status = 'closed';
      settleConnect(new Error('CLIENT_DISCONNECTED'));
    }

    return {
      connect: connect,
      start: start,
      disconnect: disconnect,
      dispose: disconnect,
      counters: counters,
      get status() {
        return status;
      },
      get mode() {
        return mode;
      },
      get deviceScope() {
        return scope;
      },
      get capabilities() {
        return expectedCaps;
      },
      get session() {
        return session;
      },
      get lastError() {
        return lastError;
      },
      get lastResult() {
        return lastResult;
      }
    };
  }

  var exported = {
    VERSION: VERSION,
    ACTIONS: ACTIONS,
    MUTATIONS: MUTATIONS,
    ACTIVATION: ACTIVATION,
    MAX_FRAME: MAX_FRAME,
    VOLUME_TOLERANCE: VOLUME_TOLERANCE,
    createClient: createClient,
    probeSupport: probeSupport,
    capabilitiesFor: capabilitiesFor,
    supportString: supportString,
    readState: readState,
    validArgs: validArgs,
    parseStrictJson: parseStrictJson,
    validateIncoming: validateIncoming,
    macInput: macInput,
    importSecret: importSecret,
    sign: sign,
    verify: verify,
    toHex: toHex,
    fromHex: fromHex,
    randomHex: randomHex,
    readConfig: readConfig
  };

  root.LunmiSpotifyKit = exported;

  if (!root.LUNMI_SPOTIFY_KIT_NO_AUTOSTART && readConfig()) {
    var client = createClient({});
    root.LunmiSpotifyKitClient = client;
    client.start().catch(function () {
      /* bounded retries and status are exposed on the client */
    });
  }
})(
  typeof globalThis !== 'undefined'
    ? globalThis
    : typeof window !== 'undefined'
      ? window
      : this
);
