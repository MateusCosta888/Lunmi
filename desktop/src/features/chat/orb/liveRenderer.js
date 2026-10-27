/**
 * Reactive Lunmi orb adapted from Live Orb by radiumcoders (Jay Sharma).
 * Source: https://23rd.dev/r/live-orb.json
 * MIT declaration: https://preview.21st.dev/@radiumcoders/components/live-orb
 * Full notice: desktop/licenses/radiumcoders-LiveOrb-MIT.txt.
 * The supplied visual base is combined with Lunmi's procedural particle material.
 * Integration changes:
 * - sem controlos/slider/meter/botões/burst/textos de demonstração nem `window.LunmiOrbDemo`;
 * - o modo vem de fora (`setMode`) — estados reais da aplicação; os envelopes por modo são os
 *   procedurais do ficheiro; nenhum valor de áudio real é medido;
 * - olhar a ≤60 FPS durante interação; em repouso chat ≤30 e Node idle ≤15/ativo ≤30;
 *   upload de textura ≤30; no Node pequeno, partículas/DPR/tamanho de textura adaptados mantendo a
 *   identidade;
 * - para o loop (sem quadros novos) quando o documento está hidden, quando o container sai do
 *   viewport (IntersectionObserver), quando `setActive(false)` (camada do Node oculta ou chat)
 *   e com `prefers-reduced-motion` (um quadro estático; redraw apenas em mudanças);
 * - limpeza completa: listeners, observers, RAF, programs/shaders/buffers/texture WebGL;
 *   perda/restauro de contexto tratado; sem canvas → `onUnavailable` (fallback honesto).
 */

/* attribution: original visual foundation @radiumcoders / Live Orb (21st.dev, MIT),
   adapted from the user-supplied file; only the body material and lifecycle differ. */

const VERT = `
attribute vec2 a_position;
void main() { gl_Position = vec4(a_position, 0.0, 1.0); }
`;

const FRAG = `
precision highp float;
uniform vec2 u_resolution;
uniform float u_time;
uniform vec2 u_look;
uniform float u_blink;
uniform float u_energy;
uniform float u_mode;
uniform sampler2D u_particles;
vec3 orient(vec3 p, vec2 look) {
  float yaw = look.x * 0.92;
  float pitch = -look.y * 0.78;
  float cy = cos(yaw);
  float sy = sin(yaw);
  float cp = cos(pitch);
  float sp = sin(pitch);
  vec3 q = vec3(p.x, p.y * cp - p.z * sp, p.y * sp + p.z * cp);
  return vec3(q.x * cy + q.z * sy, q.y, -q.x * sy + q.z * cy);
}
float eyeMask(vec3 n, vec3 e, vec3 right, vec3 up, float halfH, float rad) {
  float facing = dot(n, e);
  float x = dot(n, right) - dot(e, right);
  float y = dot(n, up) - dot(e, up);
  y -= clamp(y, -halfH, halfH);
  float d = length(vec2(x, y)) - rad;
  float fill = 1.0 - smoothstep(-0.01, 0.01, d);
  return fill * smoothstep(0.12, 0.32, facing);
}
void main() {
  vec2 uv = (gl_FragCoord.xy / u_resolution.xy) * 2.0 - 1.0;
  float aspect = u_resolution.x / max(u_resolution.y, 1.0);
  uv.x *= aspect;
  float radius = 0.94;
  vec2 p = uv / radius;
  float r2 = dot(p, p);
  float edge = 1.0 - smoothstep(0.985, 1.012, sqrt(max(r2, 0.0)));
  if (edge <= 0.001) { gl_FragColor = vec4(0.0); return; }
  float z = sqrt(max(1.0 - r2, 0.0));
  vec3 n = normalize(vec3(p, z));
  vec2 look = u_look;
  float lm = length(look);
  if (lm > 1.0) look /= lm;
  vec3 right = orient(vec3(1.0, 0.0, 0.0), look);
  vec3 up = orient(vec3(0.0, 1.0, 0.0), look);
  vec3 eL = orient(normalize(vec3(-0.32, 0.08, 1.0)), look);
  vec3 eR = orient(normalize(vec3(0.32, 0.08, 1.0)), look);
  float halfH = mix(0.128, 0.012, u_blink);
  float rad = mix(0.054, 0.062, u_blink);
  float eyes = max(
    eyeMask(n, eL, right, up, halfH, rad),
    eyeMask(n, eR, right, up, halfH, rad)
  );
  vec2 st=gl_FragCoord.xy/u_resolution.xy;
  vec3 stars=texture2D(u_particles,st).rgb;
  float rim=pow(1.0-z,3.7);
  vec3 body=stars*(0.90+0.08*z);
  body+=vec3(0.015,0.018,0.029)*(0.5+0.4*z);
  body+=vec3(0.12,0.15,0.20)*rim*(0.18+u_energy*.15);
  vec3 eyeCol=vec3(0.96,0.97,1.0);
  vec3 col=mix(body,eyeCol,clamp(eyes,0.0,1.0));
  float eyeHalo=smoothstep(0.15,0.95,eyes)*(0.08+u_energy*.08);
  col+=vec3(eyeHalo);
  gl_FragColor=vec4(clamp(col,0.0,1.0),edge);
}
`;

const MODES = ["idle", "listening", "thinking", "speaking"];

export function mountLiveOrb(canvas, options = {}) {
  const { compact = false, onUnavailable = () => undefined, onModeChange = null } = options;
  const host = canvas.parentElement || canvas;
  const P = compact
    ? { particleCount: 150, particleSpread: 18, cameraDistance: 17, connectionDistance: 58, tex: 256, dprCap: 1.5, sizeScale: 14.7 }
    : { particleCount: 450, particleSpread: 18, cameraDistance: 17, connectionDistance: 110, tex: 512, dprCap: 2, sizeScale: 29.4 };
  const INTENSITY = 0.8;
  const MIN_FRAME_MS = 1000 / 30;
  const IDLE_FRAME_MS = 1000 / 15;
  const GAZE_FRAME_MS = 1000 / 60;
  const clamp = (n, a, b) => Math.max(a, Math.min(b, n));
  const smooth = (a, b, t) => a + (b - a) * t;
  const rnd = (a, b) => a + Math.random() * (b - a);

  // Ambientes sem canvas (testes/jsdom) podem devolver null ou levantar; o fallback honesto é
  // a marca real via onUnavailable, nunca uma orb falsa por CSS.
  let gl = null, fallback2d = null;
  try { gl = canvas.getContext("webgl", {
    alpha: true, antialias: true, depth: false, stencil: false,
    premultipliedAlpha: false, powerPreference: "high-performance",
  }); } catch { gl = null; }
  try { fallback2d = gl ? null : canvas.getContext("2d"); } catch { fallback2d = null; }
  if (!gl && !fallback2d) { onUnavailable(); return { setMode() {}, setActive() {}, destroy() {} }; }

  const S = P.tex;
  const texCanvas = document.createElement("canvas");
  texCanvas.width = S; texCanvas.height = S;
  const pctx = texCanvas.getContext("2d", { alpha: false });
  if (!pctx) { onUnavailable(); return { setMode() {}, setActive() {}, destroy() {} }; }

  let prog = null, texture = null, quadBuffer = null, attrib = -1, uniforms = {};
  let anim = 0, ioActive = true, uiActive = true, lost = false, destroyed = false, dead = false;
  let mode = "idle", clock = 0, last = 0, lastFrameAt = 0, lastPointer = -Infinity, lastTextureAt = -Infinity;
  let speakingLevel = 0, speakingTarget = 0, listeningLevel = 0, listeningTarget = 0;
  let nextSyllable = 0, nextListeningImpulse = 0, nextGlance = 1200, nextBlink = 2800, blinkAt = -1e6;
  let currentEnergy = 0;
  const look = { x: 0, y: 0.08 }, targetLook = { x: 0, y: 0.08 }, glance = { x: 0, y: 0.08 };
  const blend = { idle: 1, listening: 0, thinking: 0, speaking: 0 };

  const reduced = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : { matches: false, addEventListener: () => undefined, removeEventListener: () => undefined };

  const points = [];
  for (let i = 0; i < P.particleCount; i++) {
    const u = rnd(-1, 1), a = rnd(0, Math.PI * 2), r = Math.cbrt(Math.random()) * (P.particleSpread * 0.455), rr = Math.sqrt(1 - u * u) * r;
    points.push({ x: rr * Math.cos(a), y: rr * Math.sin(a), z: u * r, size: rnd(0.6, 1.45), alpha: rnd(0.4, 0.95), phase: rnd(0, Math.PI * 2) });
  }
  const edges = [];
  for (let i = 0; i < points.length; i++) {
    const a = points[i], ns = [];
    for (let j = 0; j < points.length; j++) {
      if (j === i) continue;
      const b = points[j], dx = a.x - b.x, dy = a.y - b.y, dz = (a.z - b.z) * 0.75, dd = dx * dx + dy * dy + dz * dz;
      if (dd < 11.8) ns.push([dd, j]);
    }
    ns.sort((x, y) => x[0] - y[0]);
    for (const [, j] of ns.slice(0, 3)) { if (i < j) edges.push([i, j]); }
  }

  // Recursos rastreados para libertação SEMPRE idempotente: um shader que falha a compilação
  // já existia e tem de ser deletado; nada pode ser libertado duas vezes.
  const owned = { shaders: [], buffers: [], textures: [], program: null };
  function compile(type, src) {
    const s = gl.createShader(type);
    owned.shaders.push(s);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
    return s;
  }
  function disposeGl() {
    if (gl) {
      try {
        const lostContext = typeof gl.isContextLost === "function" && gl.isContextLost();
        if (!lostContext) {
          for (const s of owned.shaders) gl.deleteShader(s);
          for (const b of owned.buffers) gl.deleteBuffer(b);
          for (const t of owned.textures) gl.deleteTexture(t);
          if (owned.program) gl.deleteProgram(owned.program);
        }
      } catch { /* contexto morto: nothing further to release */ }
    }
    owned.shaders = []; owned.buffers = []; owned.textures = []; owned.program = null;
    prog = null; texture = null; quadBuffer = null; uniforms = {};
  }
  function glSetup() {
    const vs = compile(gl.VERTEX_SHADER, VERT), fs = compile(gl.FRAGMENT_SHADER, FRAG);
    prog = gl.createProgram();
    owned.program = prog;
    gl.attachShader(prog, vs); gl.attachShader(prog, fs); gl.linkProgram(prog);
    gl.deleteShader(vs); gl.deleteShader(fs);
    owned.shaders = owned.shaders.filter(s => s !== vs && s !== fs);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
    gl.useProgram(prog);
    quadBuffer = gl.createBuffer();
    owned.buffers.push(quadBuffer);
    gl.bindBuffer(gl.ARRAY_BUFFER, quadBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), gl.STATIC_DRAW);
    attrib = gl.getAttribLocation(prog, "a_position");
    gl.enableVertexAttribArray(attrib);
    gl.vertexAttribPointer(attrib, 2, gl.FLOAT, false, 0, 0);
    ["resolution", "time", "look", "blink", "energy", "mode", "particles"].forEach(k => { uniforms[k] = gl.getUniformLocation(prog, "u_" + k); });
    texture = gl.createTexture();
    owned.textures.push(texture);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
    gl.uniform1i(uniforms.particles, 0);
  }
  function resize() {
    const rect = host.getBoundingClientRect();
    const dpr = Math.min(P.dprCap, window.devicePixelRatio || 1);
    const size = Math.max(1, Math.round((rect.width || 52) * dpr));
    if (canvas.width !== size || canvas.height !== size) {
      canvas.width = size; canvas.height = size;
      if (gl) gl.viewport(0, 0, size, size);
    }
  }
  function updateReactivity(t, dt) {
    const k = reduced.matches ? 1 : 1 - Math.exp(-dt / 340);
    for (const state of MODES) blend[state] = smooth(blend[state], state === mode ? 1 : 0, k);
    if (reduced.matches) { speakingLevel = 0; listeningLevel = 0; return; }
    if (t >= nextSyllable) { speakingTarget = Math.random() < 0.14 ? 0.05 : rnd(0.35, 1); nextSyllable = t + rnd(105, 245); }
    if (t >= nextListeningImpulse) { listeningTarget = Math.random() < 0.16 ? 0.08 : rnd(0.3, 0.9); nextListeningImpulse = t + rnd(240, 540); }
    speakingLevel = smooth(speakingLevel, speakingTarget, 1 - Math.exp(-dt / (speakingTarget > speakingLevel ? 65 : 175)));
    listeningLevel = smooth(listeningLevel, listeningTarget, 1 - Math.exp(-dt / (listeningTarget > listeningLevel ? 120 : 310)));
  }
  function drawParticles(t, dt) {
    const ctx = pctx, seconds = t * 0.001;
    const unit = S / 512;
    const bi = blend.idle, bl = blend.listening, bt = blend.thinking, bs = blend.speaking;
    const voice = clamp(speakingLevel * bs * INTENSITY, 0, 1);
    const hearing = clamp(listeningLevel * bl * INTENSITY, 0, 1);
    const respond = INTENSITY;
    void bi;
    ctx.fillStyle = "#05070d"; ctx.fillRect(0, 0, S, S);
    const rotation = reduced.matches ? 0 : seconds * (0.027 + 0.085 * bt + 0.009 * bs);
    const yaw = Math.sin(seconds * 0.36) * 0.075 + look.x * 0.12;
    const pitch = Math.sin(seconds * 0.21) * 0.035 + look.y * 0.10;
    const cy = Math.cos(rotation + yaw), sy = Math.sin(rotation + yaw);
    const cp = Math.cos(pitch), sp = Math.sin(pitch);
    const render = new Array(points.length);
    for (let i = 0; i < points.length; i++) {
      const p = points[i];
      let x = p.x, y = p.y, z = p.z;
      const radial = Math.sqrt(x * x + y * y + z * z);
      const orbital = Math.atan2(y, x);
      const micro = reduced.matches ? 0 : 0.040 * respond;
      x += Math.sin(seconds * 0.47 + p.phase) * micro;
      y += Math.cos(seconds * 0.41 + p.phase * 1.27) * micro;
      z += Math.sin(seconds * 0.31 + p.phase * 0.76) * micro;
      if (bl > 0.001) {
        const wave = Math.sin(seconds * 5.4 + radial * 1.28);
        const contraction = 1 - bl * respond * (0.047 + hearing * 0.048) + bl * respond * 0.037 * wave;
        x *= contraction; y *= contraction; z *= contraction;
      }
      if (bt > 0.001) {
        const swirl = bt * respond * (0.23 * Math.sin(seconds * 0.82 + radial * 0.64) + seconds * 0.19);
        const cs = Math.cos(swirl), sn = Math.sin(swirl);
        const px = x * cs - y * sn, py = x * sn + y * cs; x = px; y = py;
        z += Math.sin(seconds * 1.1 + orbital * 2.1 + p.phase) * bt * respond * 0.15;
      }
      if (bs > 0.001) {
        const wave = Math.sin(seconds * 12.2 - radial * 1.15 + p.phase * 0.14);
        const pulse = 1 + bs * respond * (0.018 + speakingLevel * (0.055 + 0.090 * wave));
        x *= pulse; y *= pulse; z *= pulse;
      }
      const rotatedX = x * cy - z * sy;
      const rotatedZ = x * sy + z * cy;
      const rotatedY = y * cp - rotatedZ * sp;
      z = y * sp + rotatedZ;
      const perspective = P.cameraDistance / (P.cameraDistance - z * 0.33);
      const scale = P.sizeScale * perspective;
      render[i] = {
        x: S / 2 + rotatedX * scale * unit,
        y: S / 2 - rotatedY * scale * unit,
        z, scale, a: p.alpha, phase: p.phase, size: p.size, radial, orbital,
      };
    }
    const clipR = S * 0.466;
    ctx.lineWidth = (0.55 + voice * 0.28) * unit;
    if (edges.length) for (let e = 0; e < edges.length; e++) {
      const [i, j] = edges[e], a = render[i], b = render[j];
      const len = Math.hypot(a.x - b.x, a.y - b.y);
      if (len > P.connectionDistance * unit) continue;
      if (Math.hypot(a.x - S / 2, a.y - S / 2) > clipR || Math.hypot(b.x - S / 2, b.y - S / 2) > clipR) continue;
      const shimmer = bt * respond * (0.5 + 0.5 * Math.sin(seconds * 3.5 + e * 0.71));
      const alpha = (1 - len / (P.connectionDistance * unit)) * (0.098 + 0.075 * hearing + 0.12 * voice + 0.046 * shimmer);
      ctx.strokeStyle = `rgba(211,223,248,${Math.min(0.42, alpha).toFixed(3)})`;
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      if (bt * respond > 0.02 && e % 6 === 0) {
        const travel = (seconds * 0.55 + e * 0.173) % 1;
        const tx = smooth(a.x, b.x, travel), ty = smooth(a.y, b.y, travel);
        ctx.fillStyle = `rgba(233,241,255,${(0.08 + 0.48 * bt * respond).toFixed(3)})`;
        ctx.beginPath(); ctx.arc(tx, ty, (0.65 + bt * 0.48) * unit, 0, Math.PI * 2); ctx.fill();
      }
    }
    for (let i = 0; i < render.length; i++) {
      const p = render[i];
      if (Math.hypot(p.x - S / 2, p.y - S / 2) > clipR) continue;
      const sparkle = reduced.matches ? 1 : 1 + Math.sin(seconds * (1.7 + bt * 0.7) + p.phase) * 0.3;
      const speakWave = 0.5 + 0.5 * Math.sin(seconds * 12.2 - p.radial * 1.15 + p.phase * 0.14);
      const linkBoost = 1 + 0.25 * hearing + 0.58 * voice * speakWave + 0.20 * bt * respond;
      const size = (0.53 + p.size * 0.73) * Math.max(0.72, p.scale / 29.4) * sparkle * (1 + 0.28 * voice * speakWave + 0.13 * hearing) * unit;
      const alpha = clamp((0.28 + p.a * 0.52) * sparkle * 0.84 * linkBoost, 0.05, 0.99);
      ctx.fillStyle = `rgba(243,246,255,${alpha.toFixed(3)})`;
      ctx.beginPath(); ctx.arc(p.x, p.y, Math.max(0.36 * unit, size), 0, Math.PI * 2); ctx.fill();
      if (p.size > 1.15) {
        const halo = 0.052 + 0.07 * voice + 0.02 * hearing;
        ctx.fillStyle = `rgba(226,236,255,${(alpha * halo).toFixed(3)})`;
        ctx.beginPath(); ctx.arc(p.x, p.y, size * (3.2 + voice * 1.4), 0, Math.PI * 2); ctx.fill();
      }
    }
    if (bs > 0.02 && voice > 0.05) {
      for (let j = 0; j < 2; j++) {
        const phase = (seconds * 1.75 + j * 0.5) % 1;
        const radius = (36 + phase * 182) * unit;
        const alpha = bs * voice * (1 - phase) * 0.095;
        ctx.strokeStyle = `rgba(212,228,255,${alpha.toFixed(3)})`;
        ctx.lineWidth = (0.65 + voice * 0.35) * unit;
        ctx.beginPath(); ctx.arc(S / 2, S / 2, radius, 0, Math.PI * 2); ctx.stroke();
      }
    } else if (bl > 0.02 && hearing > 0.05) {
      const phase = (seconds * 0.72) % 1;
      const radius = (209 - phase * 151) * unit;
      ctx.strokeStyle = `rgba(209,223,255,${(bl * hearing * (1 - phase) * 0.065).toFixed(3)})`;
      ctx.lineWidth = 0.7 * unit;
      ctx.beginPath(); ctx.arc(S / 2, S / 2, radius, 0, Math.PI * 2); ctx.stroke();
    }
    if (gl) {
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, texCanvas);
    }
    void dt;
  }
  function paint(now) {
    anim = 0;
    if (destroyed || dead || lost) return;
    const dt = Math.min(44, Math.max(0, now - last || 16));
    const steering = now - lastPointer < 3000;
    const budget = steering ? GAZE_FRAME_MS : compact && mode === "idle" ? IDLE_FRAME_MS : MIN_FRAME_MS;
    const elapsed = now - lastFrameAt;
    if (elapsed + 0.001 < budget && lastFrameAt !== 0 && !reduced.matches) {
      anim = requestAnimationFrame(paint);
      return;
    }
    // Preserve fractional frame time so 60/120/144 Hz displays do not quantize the cap down.
    lastFrameAt = lastFrameAt === 0 ? now : now - ((elapsed + 0.001) % budget);
    last = now;
    if (!reduced.matches) clock += dt;
    if (!steering && clock > nextGlance) { glance.x = Math.random() < 0.25 ? 0 : rnd(-0.7, 0.7); glance.y = rnd(-0.35, 0.38); nextGlance = clock + rnd(900, 2600); }
    const goal = steering ? targetLook : reduced.matches ? { x: 0, y: 0.08 } : glance;
    const gazeBlend = reduced.matches ? 1 : 1 - Math.exp(-dt / (steering ? 95 : 400));
    look.x += (goal.x - look.x) * gazeBlend;
    look.y += (goal.y - look.y) * gazeBlend;
    if (!reduced.matches && clock > nextBlink) { blinkAt = clock; nextBlink = clock + rnd(2500, 5200); }
    const blinkT = (clock - blinkAt) / 1000;
    let blink = 0;
    if (!reduced.matches && blinkT >= 0 && blinkT < 0.18) {
      if (blinkT < 0.055) blink = blinkT / 0.055;
      else if (blinkT < 0.10) blink = 1;
      else blink = 1 - (blinkT - 0.10) / 0.08;
    }
    updateReactivity(clock, dt);
    const modulation = blend.speaking * speakingLevel * INTENSITY * 0.72 + blend.listening * listeningLevel * INTENSITY * 0.26 + blend.thinking * 0.09;
    currentEnergy += (modulation - currentEnergy) * (reduced.matches ? 1 : 1 - Math.exp(-dt / 120));
    resize();
    if (reduced.matches || now - lastTextureAt >= 30 || lastTextureAt < 0) {
      drawParticles(clock, dt);
      lastTextureAt = now;
    }
    if (!gl) {
      const ctx = fallback2d, Sc = canvas.width, R = Sc * 0.47;
      ctx.clearRect(0, 0, Sc, Sc); ctx.save();
      ctx.beginPath(); ctx.arc(Sc / 2, Sc / 2, R, 0, Math.PI * 2); ctx.clip();
      ctx.drawImage(texCanvas, Sc * 0.03, Sc * 0.03, Sc * 0.94, Sc * 0.94);
      const edge = ctx.createRadialGradient(Sc / 2, Sc / 2, R * 0.25, Sc / 2, Sc / 2, R);
      edge.addColorStop(0, "rgba(10,13,20,0)");
      edge.addColorStop(0.75, "rgba(9,12,18,.05)");
      edge.addColorStop(1, "rgba(88,101,135,.18)");
      ctx.fillStyle = edge; ctx.fillRect(0, 0, Sc, Sc);
      const ex = Sc * 0.154, ey = Sc * 0.038, hw = Sc * 0.049, hh = Sc * 0.133 * (1 - blink * 0.90);
      ctx.fillStyle = "#f5f7ff"; ctx.shadowColor = "rgba(223,231,255,.23)"; ctx.shadowBlur = Sc * 0.015;
      for (const side of [-1, 1]) {
        const ox = Sc * 0.5 + side * ex + look.x * Sc * 0.068, oy = Sc * 0.49 - look.y * Sc * 0.065;
        const radius = Sc * 0.052;
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(ox - hw, oy - hh - radius, hw * 2, (hh + radius) * 2, [radius]);
        else ctx.rect(ox - hw, oy - hh - radius, hw * 2, (hh + radius) * 2);
        ctx.fill();
      }
      ctx.restore();
      void ey;
    } else {
      gl.useProgram(prog);
      gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
      gl.uniform2f(uniforms.resolution, canvas.width, canvas.height);
      gl.uniform1f(uniforms.time, clock * 0.001);
      gl.uniform2f(uniforms.look, look.x, look.y);
      gl.uniform1f(uniforms.blink, blink);
      gl.uniform1f(uniforms.energy, currentEnergy);
      gl.uniform1f(uniforms.mode, MODES.indexOf(mode));
      gl.drawArrays(gl.TRIANGLES, 0, 6);
    }
    if (reduced.matches) return; // um quadro estático; redraw só por mudanças/wake
    if (document.visibilityState === "visible" && ioActive && uiActive) anim = requestAnimationFrame(paint);
  }
  function wake() {
    if (destroyed || dead || lost) return;
    if (reduced.matches) { paint(performance.now()); return; }
    if (!anim && ioActive && uiActive && document.visibilityState === "visible") anim = requestAnimationFrame(paint);
  }
  function setMode(next) {
    if (!MODES.includes(next) || next === mode) return;
    mode = next;
    lastTextureAt = -Infinity;
    if (next === "speaking") nextSyllable = 0;
    if (next === "listening") nextListeningImpulse = 0;
    wake();
  }
  function setActive(active) {
    uiActive = Boolean(active);
    if (!uiActive) { if (anim) cancelAnimationFrame(anim); anim = 0; }
    else wake();
  }
  function onPointer(e) {
    // Gaze só se persegue com a orb visível/ativa e um retângulo finito: uma camada oculta
    // (rect 0×0) ou um `hidden` não pode envenenar `look` com NaN/Infinity para o reveal.
    if (destroyed || dead || !uiActive || !ioActive || document.visibilityState !== "visible") return;
    const rect = host.getBoundingClientRect();
    if (!Number.isFinite(rect.width) || !Number.isFinite(rect.height) || rect.width < 2 || rect.height < 2) return;
    if (!Number.isFinite(e.clientX) || !Number.isFinite(e.clientY)) return;
    targetLook.x = clamp((e.clientX - rect.left - rect.width * 0.5) / (rect.width * 0.5), -1, 1);
    targetLook.y = clamp((rect.top + rect.height * 0.5 - e.clientY) / (rect.height * 0.5), -1, 1);
    lastPointer = performance.now();
  }
  const onVisibility = () => {
    if (document.visibilityState === "hidden") { if (anim) cancelAnimationFrame(anim); anim = 0; }
    else wake();
  };
  const onResize = () => wake();
  const onLost = e => { e.preventDefault(); lost = true; if (anim) cancelAnimationFrame(anim); anim = 0; };
  const onRestored = () => {
    lost = false;
    if (gl) {
      try { glSetup(); }
      catch {
        // Restauração com compilação falhada: o motor morre com limpeza completa — nem
        // listeners globais, nem observer, nem shaders parciais podem sobreviver.
        dead = true;
        disposeGl();
        detachGlobals();
        onUnavailable();
        return;
      }
    }
    wake();
  };
  const onReducedChange = () => wake();

  let observer = null;
  if (window.IntersectionObserver) {
    observer = new IntersectionObserver(entries => {
      ioActive = entries.some(entry => entry.isIntersecting);
      if (ioActive) wake();
      else { if (anim) cancelAnimationFrame(anim); anim = 0; }
    });
    observer.observe(host);
  }
  document.addEventListener("pointermove", onPointer, { passive: true }); // olhar segue o cursor tambem no Node (passivo, sem interacao nova)
  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("resize", onResize);
  if (reduced.addEventListener) reduced.addEventListener("change", onReducedChange);
  canvas.addEventListener("webglcontextlost", onLost);
  canvas.addEventListener("webglcontextrestored", onRestored);
  let globalsAttached = true;
  function detachGlobals() {
    if (!globalsAttached) return;
    globalsAttached = false;
    if (anim) cancelAnimationFrame(anim);
    anim = 0;
    if (observer) observer.disconnect();
    document.removeEventListener("pointermove", onPointer);
    document.removeEventListener("visibilitychange", onVisibility);
    window.removeEventListener("resize", onResize);
    if (reduced.removeEventListener) reduced.removeEventListener("change", onReducedChange);
    canvas.removeEventListener("webglcontextlost", onLost);
    canvas.removeEventListener("webglcontextrestored", onRestored);
  }
  if (gl) {
    try { glSetup(); }
    catch {
      // Compilação/link falhada: libertar TUDO (shaders parciais incluídos) e devolver um
      // handle inerte; `destroy` posterior não volta a libertar nada (idempotente).
      dead = true;
      disposeGl();
      detachGlobals();
      onUnavailable();
      return { setMode() {}, setActive() {}, destroy() {} };
    }
  }
  resize();
  wake();

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    detachGlobals();
    disposeGl();
  }
  void onModeChange;
  return { setMode, setActive, destroy };
}
