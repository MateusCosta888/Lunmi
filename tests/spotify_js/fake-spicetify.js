/*
 * Test-only fake Spicetify. It models the pinned v2.46.2 Player surface used by
 * the real extension: Player.data is the origin._state object (camelCase), and
 * the public methods are the ones the extension allowlists.
 *
 * This file never ships in the bundle and is not loaded by Spotify.
 */
(function (root) {
  'use strict';

  var TRACKS = [
    {
      uri: 'spotify:track:qa-s1-0001',
      name: 'QA Kit Track',
      artists: [{ name: 'QA Fixture Artist' }],
      album: { name: 'QA Fixture Album' }
    },
    {
      uri: 'spotify:track:qa-s1-0002',
      name: 'QA Kit Track Two',
      artists: [{ name: 'QA Fixture Artist' }],
      album: { name: 'QA Fixture Album' }
    }
  ];

  function createFakeSpicetify(options) {
    var opts = options || {};
    var calls = {
      play: 0,
      pause: 0,
      next: 0,
      back: 0,
      setVolume: 0,
      playUri: 0,
      pullToLocal: 0,
      graphql: 0
    };
    var index = typeof opts.index === 'number' ? opts.index : 0;
    var state = {
      isPaused: opts.isPaused === true,
      positionAsOfTimestamp: typeof opts.position === 'number' ? opts.position : 12000,
      timestamp: Date.now(),
      volume: typeof opts.volume === 'number' ? opts.volume : 0.42,
      shuffle: opts.shuffle === true,
      repeat: typeof opts.repeat === 'number' ? opts.repeat : 0
    };
    var nextChanges = opts.nextChanges !== false;
    var backRestarts = opts.backRestarts === true;
    var backPosition = typeof opts.backPosition === 'number' ? opts.backPosition : 0;
    var volumeSticks = opts.volumeSticks !== false;
    // Mirrors the real surface: the methods exist from load (core.js) but they
    // throw while origin/_state/PlaybackAPI do not exist yet. A defined method is
    // not readiness.
    var readyAt =
      opts.neverReady === true
        ? Number.POSITIVE_INFINITY
        : Date.now() + (typeof opts.readyAfterMs === 'number' ? opts.readyAfterMs : 0);

    function assertReadable(name) {
      if (opts.failGetter === name) {
        throw new TypeError('fixture getter ' + name + ' unavailable');
      }
      if (Date.now() < readyAt) {
        throw new TypeError('fixture player not ready');
      }
    }

    function currentItem() {
      if (opts.noTrack === true) {
        return null;
      }
      return TRACKS[index % TRACKS.length];
    }

    function position() {
      var base = state.isPaused ? 0 : Date.now() - state.timestamp;
      return base + state.positionAsOfTimestamp;
    }

    function trackUri(number) {
      return 'spotify:track:' + String(number).padStart(22, '0');
    }

    function searchItem(uri, title, artist, album, playable) {
      return {
        item: {
          data: {
            __typename: 'Track',
            uri: uri,
            name: title,
            artists: { items: [{ profile: { name: artist } }] },
            albumOfTrack: { name: album },
            playability: { playable: playable !== false }
          }
        }
      };
    }

    var defaultSearchPayload = {
      data: {
        searchV2: {
          tracksV2: {
            items: [
              searchItem(trackUri(1), 'QA Search One', 'QA Fixture Artist', 'QA Fixture Album'),
              searchItem(trackUri(2), 'QA Search Two', 'QA Fixture Artist', 'QA Fixture Album')
            ]
          }
        }
      }
    };

    var pushes = [];
    var lastSearch = null;
    var device = null;
    var graphql = {
      Definitions: {},
      Request: function (definition, variables) {
        calls.graphql += 1;
        lastSearch = { definition: definition, variables: variables };
        if (opts.searchRejects === true) {
          return Promise.reject(new Error('fixture search failed'));
        }
        if (typeof opts.searchDelayMs === 'number') {
          return new Promise(function (resolve) {
            setTimeout(function () {
              resolve(opts.searchPayload || defaultSearchPayload);
            }, opts.searchDelayMs);
          });
        }
        return Promise.resolve(opts.searchPayload || defaultSearchPayload);
      }
    };
    var platform = {
      History: {
        push: function (path) {
          pushes.push(path);
          if (opts.searchDefinition === 'never') return;
          if (!graphql.Definitions.searchTracks) {
            var delay = typeof opts.definitionDelayMs === 'number' ? opts.definitionDelayMs : 0;
            setTimeout(function () {
              graphql.Definitions.searchTracks = { kind: 'SearchTracks' };
            }, delay);
          }
        }
      },
      ConnectAPI: {
        getState: function () {
          return { activeDevice: device };
        },
        // The exposed pinned surface: one fixed-local pull, no device argument.
        // `activate` selects the fixture behaviour; none of it proves real Spotify.
        pullToLocal: function () {
          calls.pullToLocal += 1;
          var mode = opts.activate || 'local';
          if (mode === 'throw') {
            throw new Error('fixture pullToLocal threw');
          }
          if (mode === 'reject') {
            return Promise.reject(new Error('fixture pullToLocal rejected'));
          }
          if (mode === 'hold') {
            return new Promise(function () {});
          }
          if (mode === 'none') {
            return Promise.resolve();
          }
          setScope('local');
          return Promise.resolve();
        }
      },
      PlaybackAPI: { _isLocal: false }
    };

    function setScope(scope) {
      if (scope === 'local') {
        device = { isActive: true, isLocal: true };
        platform.PlaybackAPI._isLocal = true;
      } else if (scope === 'remote') {
        device = { isActive: true, isLocal: false };
        platform.PlaybackAPI._isLocal = false;
      } else if (scope === 'discordant') {
        device = { isActive: true, isLocal: true };
        platform.PlaybackAPI._isLocal = false;
      } else {
        device = null;
        platform.PlaybackAPI._isLocal = false;
      }
    }

    setScope(opts.scope || 'local');
    if (opts.searchDefinition !== 'never' && opts.searchDefinition !== 'onPush') {
      graphql.Definitions.searchTracks = { kind: 'SearchTracks' };
    }

    var player = {
      data: {
        item: currentItem(),
        isPaused: state.isPaused,
        positionAsOfTimestamp: state.positionAsOfTimestamp,
        timestamp: state.timestamp
      },
      isPlaying: function () {
        assertReadable('isPlaying');
        return state.isPaused === false;
      },
      getProgress: function () {
        assertReadable('getProgress');
        return position();
      },
      getVolume: function () {
        assertReadable('getVolume');
        return state.volume;
      },
      getShuffle: function () {
        assertReadable('getShuffle');
        return state.shuffle;
      },
      getRepeat: function () {
        assertReadable('getRepeat');
        return state.repeat;
      },
      play: function () {
        calls.play += 1;
        if (state.isPaused) {
          state.isPaused = false;
          state.timestamp = Date.now();
        }
      },
      pause: function () {
        calls.pause += 1;
        if (!state.isPaused) {
          state.positionAsOfTimestamp = position();
          state.timestamp = Date.now();
          state.isPaused = true;
        }
      },
      next: function () {
        calls.next += 1;
        if (nextChanges) {
          index += 1;
          state.positionAsOfTimestamp = 0;
          state.timestamp = Date.now();
          player.data.item = currentItem();
        }
      },
      back: function () {
        calls.back += 1;
        if (backRestarts) {
          state.positionAsOfTimestamp = backPosition;
          state.timestamp = Date.now();
          return;
        }
        index = (index - 1 + TRACKS.length) % TRACKS.length;
        state.positionAsOfTimestamp = 0;
        state.timestamp = Date.now();
        player.data.item = currentItem();
      },
      setVolume: function (value) {
        calls.setVolume += 1;
        if (volumeSticks) {
          state.volume = value;
        }
      },
      playUri: function (uri) {
        calls.playUri += 1;
        if (opts.playUriIgnores === true) {
          return Promise.resolve();
        }
        player.data.item = {
          uri: opts.playUriTarget || uri,
          name: 'QA Played Track',
          artists: [{ name: 'QA Fixture Artist' }],
          album: { name: 'QA Fixture Album' }
        };
        state.isPaused = false;
        state.positionAsOfTimestamp = 0;
        state.timestamp = Date.now();
        return Promise.resolve();
      }
    };

    return {
      Player: player,
      Platform: platform,
      GraphQL: graphql,
      calls: calls,
      historyPaths: pushes,
      searchRequest: function () {
        return lastSearch;
      },
      setScope: setScope,
      writes: function () {
        return (
          calls.play + calls.pause + calls.next + calls.back +
          calls.setVolume + calls.playUri
        );
      }
    };
  }

  root.FakeSpicetify = { create: createFakeSpicetify };
})(
  typeof globalThis !== 'undefined'
    ? globalThis
    : typeof window !== 'undefined'
      ? window
      : this
);
