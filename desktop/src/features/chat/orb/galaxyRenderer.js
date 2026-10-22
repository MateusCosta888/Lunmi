

/**
 * Lunmi Galaxy Orb
 * Vidro / efeito óptico adaptado do Siri Sheet, VoiceOrbs (MIT):
 * https://github.com/amunozdev/voiceorbs
 * O efeito interno "sheet" foi REMOVIDO; a galáxia estrelada é procedural e gira no eixo local.
 * WebGL puro, sem dependências de JS.
 */
export function mountGalaxyOrb(canvas, onUnavailable) {

  const CONFIG = {
    rotationBase: 0.02,          // radianos por segundo, equivalente ao giro anterior em 1x
    speedMultiplier: 3.50,      // INICIAR EM 3,50x
    rotationDirection: -1,      // -1 = horário; +1 = anti-horário
    tilt: 1.28,
    diagonal: -0.34,
    parallax: 0.058,
    particleCount: {
      haze: 51000, disc: 34000, interarm: 14500, outskirts: 28500,
      innerfield: 6500,
      arms: 31000, clusters: 7000, bulge: 14500, bright: 1250, background: 900
    },
    maxDpr: 1.75,
    worldExtent: 58
  };

  const gl = canvas.getContext('webgl', {
    alpha: true, antialias: false, depth: false, stencil: false,
    premultipliedAlpha: false, powerPreference: 'high-performance',
    preserveDrawingBuffer: false
  });
  if (!gl) {
    onUnavailable();
    return () => {};
  }

  const shaders = [], programs = [], buffers = [];
  let fbo, texture, physicalSize = 0, frame = null;
  let disposed = false, inView = true, lost = false;
  function releaseGraphics() {
    if (gl.isContextLost()) return;
    // React StrictMode reuses the canvas: release objects without losing its context.
    gl.useProgram(null);
    gl.bindBuffer(gl.ARRAY_BUFFER, null);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.bindTexture(gl.TEXTURE_2D, null);
    shaders.forEach(item => gl.deleteShader(item));
    programs.forEach(item => gl.deleteProgram(item));
    buffers.forEach(item => gl.deleteBuffer(item));
    if (texture) gl.deleteTexture(texture);
    if (fbo) gl.deleteFramebuffer(fbo);
  }

  // -----------------------------------------------------------------------
  // Geração das estrelas: mesma geometria, densidade e paleta da galáxia anterior.
  // -----------------------------------------------------------------------
  let seed = 20261020;
  function random() {
    seed = (seed + 0x6D2B79F5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t ^= t + Math.imul(t ^ (t >>> 7), 61 | t);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  }
  function gaussian() {
    return Math.sqrt(-2 * Math.log(Math.max(1e-8, random()))) * Math.cos(2 * Math.PI * random());
  }
  function mix(a, b, t) { return a + (b - a) * Math.min(1, Math.max(0, t)); }
  function rgb(hex) {
    return [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255);
  }
  const pal = {
    ivory: rgb('#fff5e8'), warm: rgb('#f5dcc6'), cream: rgb('#fffaf3'),
    inner: rgb('#c8c5d2'), frost: rgb('#dce8ff'), blue: rgb('#8fa8e3'),
    azure: rgb('#718dc8'), dark: rgb('#53659a'), pale: rgb('#c1d1fc')
  };
  function colorBetween(a, b, t) { return pal[a].map((c, i) => mix(c, pal[b][i], t)); }
  function palette(r, arm = false) {
    const u = r / 49, q = random();
    if (u < .17) return colorBetween('cream', q < .45 ? 'ivory' : 'warm', u * .9);
    if (u < .37) return colorBetween('warm', q < .40 ? 'inner' : 'frost', (u - .17) / .2);
    if (q < .11) return colorBetween('cream', 'frost', u);
    if (q < .42) return colorBetween('inner', 'blue', Math.min(1, u));
    if (q < .81) return colorBetween(arm ? 'pale' : 'blue', 'azure', u * .7);
    return colorBetween('dark', 'frost', .15 + .55 * random());
  }
  function armPoint() {
    const r = mix(2, 49, Math.pow(random(), .68));
    const base = (random() < .5 ? 0 : Math.PI);
    const a = base + .43 + r / 49 * 5 + Math.sin(r * .23) * .12;
    const offset = gaussian() * (.95 + r * .06) + (random() < .11 ? gaussian() * 2 : 0);
    return {
      x: Math.cos(a) * r - Math.sin(a) * offset,
      y: Math.sin(a) * r + Math.cos(a) * offset,
      z: gaussian() * (.14 + .016 * r), r
    };
  }
  function discPoint() {
    const r = 49 * Math.sqrt(random()), a = random() * Math.PI * 2;
    return { x: Math.cos(a) * r, y: Math.sin(a) * r, z: gaussian() * (.18 + .015 * r), r };
  }
  function interarmPoint() {
    const r = 49 * (.16 + .88 * Math.sqrt(random()));
    const a = .43 + r / 49 * 5 + Math.sin(r * .23) * .12
      + (random() < .5 ? 0 : Math.PI) + Math.PI / 2 + gaussian() * .31;
    return { x: Math.cos(a) * r, y: Math.sin(a) * r, z: gaussian() * (.22 + .017 * r), r };
  }
  function outskirtsPoint() {
    const r = 49 * (.5 + Math.pow(random(), .92) * .87);
    const a = random() * Math.PI * 2, off = gaussian() * (.25 + .01 * r);
    return {
      x: Math.cos(a) * r - Math.sin(a) * off,
      y: Math.sin(a) * r + Math.cos(a) * off,
      z: gaussian() * (.25 + .023 * r), r
    };
  }
  function innerfieldPoint() {
    const r = 54 * Math.sqrt(random());
    const a = random() * Math.PI * 2;
    return {
      x: Math.cos(a) * r,
      y: Math.sin(a) * r,
      z: gaussian() * (.18 + .01 * r),
      r
    };
  }
  const clusters = Array.from({ length: 160 }, () => {
    const r = 49 * (.27 + Math.pow(random(), .8) * .76);
    const a = (random() < .5 ? 0 : Math.PI) + .43 + r / 49 * 5 + Math.sin(r * .23) * .12;
    return { x: Math.cos(a) * r, y: Math.sin(a) * r, r, spread: mix(.35, 1.65, random()) };
  });

  // Vertices intercalados: xyz, rgb, tamanho, opacidade e tipo (0=fundo, 1=galáxia).
  // Nenhum sprite, ponto gigante ou halo artificial no núcleo.
  const N = Object.values(CONFIG.particleCount).reduce((a, b) => a + b, 0);
  const vertexData = new Float32Array(N * 9);
  let pIndex = 0;
  function addPoint(point, c, size, alpha, type = 1) {
    const i = pIndex * 9;
    vertexData[i] = point.x;
    vertexData[i + 1] = point.y;
    vertexData[i + 2] = point.z;
    vertexData[i + 3] = c[0];
    vertexData[i + 4] = c[1];
    vertexData[i + 5] = c[2];
    vertexData[i + 6] = size;
    vertexData[i + 7] = alpha;
    vertexData[i + 8] = type;
    pIndex++;
  }
  function layer(count, make) { for (let i = 0; i < count; i++) make(); }

  layer(CONFIG.particleCount.haze, () => {
    const p = random() < .58 ? armPoint() : discPoint();
    addPoint(p, palette(p.r), mix(.8, 2, random()),
      (1 - .54 * Math.pow(p.r / 49, 2)) * mix(.08, .26, random()));
  });
  layer(CONFIG.particleCount.disc, () => {
    const p = discPoint();
    addPoint(p, palette(p.r), mix(.75, 1.6, random()),
      (1 - .43 * p.r / 49) * mix(.26, .67, random()));
  });
  layer(CONFIG.particleCount.interarm, () => {
    const p = interarmPoint();
    addPoint(p, palette(p.r), mix(.75, 1.45, random()),
      (1 - .38 * p.r / 49) * mix(.12, .43, random()));
  });
  layer(CONFIG.particleCount.outskirts, () => {
    const p = outskirtsPoint(), u = p.r / 49;
    const falloff = Math.max(.045, 1 - Math.pow(Math.max(0, (u - .48) / .95), 1.5));
    const tint = random();
    const col = tint < .08 ? colorBetween('warm', 'ivory', random())
      : tint < .45 ? colorBetween('frost', 'pale', random())
      : colorBetween('dark', 'blue', random());
    addPoint(p, col, mix(.65, 1.55, random()), falloff * mix(.13, .5, random()));
  });
  layer(CONFIG.particleCount.innerfield, () => {
    const p = innerfieldPoint();
    const col = random() < .22 ? colorBetween('frost', 'pale', random()) : colorBetween('dark', 'blue', random());
    addPoint(p, col, mix(.55, 1.15, random()), mix(.06, .22, random()));
  });
  layer(CONFIG.particleCount.arms, () => {
    const p = armPoint();
    addPoint(p, palette(p.r, true), mix(1, 2.1, random()),
      (1 - .25 * p.r / 49) * mix(.4, .95, random()));
  });
  layer(CONFIG.particleCount.clusters, () => {
    const center = clusters[Math.floor(random() * clusters.length)];
    addPoint({ x: center.x + gaussian() * center.spread,
      y: center.y + gaussian() * center.spread * .73,
      z: gaussian() * (.13 + .014 * center.r) },
      random() < .12 ? colorBetween('ivory', 'warm', random())
        : colorBetween('frost', 'pale', random()),
      mix(.85, 1.95, random()), mix(.38, .88, random()));
  });
  layer(CONFIG.particleCount.bulge, () => {
    const r = Math.min(13, Math.abs(gaussian()) * 3.2 + random() * 2.1);
    const a = random() * Math.PI * 2;
    addPoint({ x: Math.cos(a) * r, y: Math.sin(a) * r, z: gaussian() * 1.55 },
      colorBetween('cream', random() < .36 ? 'warm' : 'ivory', Math.min(1, r / 12)),
      mix(1.05, 2.0, random()), mix(.42, .98, random()));
  });
  layer(CONFIG.particleCount.bright, () => {
    const p = random() < .8 ? armPoint() : discPoint();
    addPoint(p, random() < .8 ? colorBetween('frost', 'pale', random()) : pal.ivory,
      mix(1.9, 3.45, random()), mix(.65, 1.0, random()));
  });
  layer(CONFIG.particleCount.background, () => {
    const special = random() < .035;
    addPoint({ x: (random() - .5) * 190, y: (random() - .5) * 115, z: -6 },
      random() < .12 ? colorBetween('warm', 'ivory', random())
        : colorBetween('dark', 'frost', random()),
      special ? mix(1.3, 2.0, random()) : mix(.55, 1.15, random()),
      special ? mix(.28, .52, random()) : mix(.08, .24, random()), 0);
  });
  // Centro = exclusivamente estrelas reais da camada bulge, tal como antes do orb.
  if (pIndex !== N) throw new Error('Quantidade de partículas inconsistente.');

  function compile(type, source) {
    const shader = gl.createShader(type);
    shaders.push(shader);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      throw new Error(gl.getShaderInfoLog(shader) || 'Erro ao compilar shader.');
    }
    return shader;
  }
  function program(v, f) {
    const out = gl.createProgram();
    programs.push(out);
    gl.attachShader(out, compile(gl.VERTEX_SHADER, v));
    gl.attachShader(out, compile(gl.FRAGMENT_SHADER, f));
    gl.linkProgram(out);
    if (!gl.getProgramParameter(out, gl.LINK_STATUS)) {
      throw new Error(gl.getProgramInfoLog(out) || 'Erro ao linkar shader.');
    }
    return out;
  }

  const particleVert = `
    precision highp float;
    attribute vec3 aPos;
    attribute vec3 aCol;
    attribute float aSize;
    attribute float aAlpha;
    attribute float aType;
    uniform float uSpin;
    uniform float uTilt;
    uniform float uDiagonal;
    uniform vec2 uParallax;
    uniform float uExtent;
    uniform float uDpr;
    varying vec3 vColor;
    varying float vAlpha;
    varying float vType;
    void main() {
      vec3 p = aPos;
      if (aType > 0.5) {
        float c = cos(uSpin), s = sin(uSpin);
        p.xy = vec2(c*p.x-s*p.y, s*p.x+c*p.y);
        float ct = cos(uTilt), st = sin(uTilt);
        p.yz = vec2(ct*p.y-st*p.z, st*p.y+ct*p.z);
        float cy = cos(uParallax.x), sy = sin(uParallax.x);
        p.xz = vec2(cy*p.x+sy*p.z, -sy*p.x+cy*p.z);
        float cd = cos(uDiagonal + uParallax.y), sd = sin(uDiagonal + uParallax.y);
        p.xy = vec2(cd*p.x-sd*p.y, sd*p.x+cd*p.y);
      }
      gl_Position = vec4(p.xy/uExtent, 0.0, 1.0);
      gl_PointSize = max(1.0, aSize * uDpr);
      vColor = aCol;
      vAlpha = aAlpha;
      vType = aType;
    }
  `;
  const particleFrag = `
    precision mediump float;
    varying vec3 vColor;
    varying float vAlpha;
    varying float vType;
    void main() {
      float r = length(gl_PointCoord - vec2(0.5));
      float soft = 1.0 - smoothstep(0.03, 0.5, r);
      float center = 1.0 - smoothstep(0.0, 0.15, r);
      float a = vAlpha * (0.58 * soft * soft + 0.42 * center);
      gl_FragColor = vec4(vColor, a);
    }
  `;

  // O vidro e a refração seguem a construção óptica do Siri Sheet (MIT),
  // mas sem a função "sheet": o shader lê a galáxia do framebuffer em seu lugar.
  const orbVert = `
    attribute vec2 aPos;
    varying vec2 vUv;
    void main() {
      vUv = aPos * 0.5 + 0.5;
      gl_Position = vec4(aPos, 0.0, 1.0);
    }
  `;
  const orbFrag = `
    precision highp float;
    varying vec2 vUv;
    uniform sampler2D uGalaxy;
    uniform float uTime;
    const float RAD = 0.875;
    const float SOFT = 0.006;
    const float INNER_ZOOM = 0.40;

    float hash21(vec2 p) {
      p = fract(p * vec2(123.34, 456.21));
      p += dot(p, p + 78.233);
      return fract(p.x * p.y);
    }
    vec3 over(vec3 dst, vec3 src, float a) {
      float k = clamp(a, 0.0, 1.0);
      return src * k + dst * (1.0 - k);
    }
    float lobe(vec3 n, vec3 dir, float power) {
      return pow(max(dot(n, dir), 0.0), power);
    }

    void main() {
      vec2 uv = (vUv - 0.5) * 2.0;
      float r = length(uv);
      if (r > 0.995) discard;

      float ang = atan(uv.y, uv.x);
      vec3 cool = vec3(0.48, 0.77, 1.0);
      vec3 warm = vec3(0.72, 0.56, 1.0);

      if (r > RAD + SOFT) {
        float halo = exp(-max(r - RAD, 0.0) * 18.0) * (1.0 - smoothstep(RAD, 0.995, r));
        vec3 haloCol = mix(cool, warm, 0.5 + 0.5 * sin(ang + uTime * 0.19));
        gl_FragColor = vec4(haloCol * halo * 0.18, halo * 0.18);
        return;
      }

      vec2 p = uv / RAD;
      float pd = length(p);
      float sphere = sqrt(max(1.0 - pd * pd, 0.0));
      vec3 n3 = normalize(vec3(p, sphere));
      vec2 n = pd > 0.0001 ? p / pd : vec2(0.0);
      float fresnel = pow(1.0 - max(n3.z, 0.0), 2.2);

      // Vidro só na BORDA: a parte central nunca sofre refração radial.
      // Antes era "n * 0.10" até em p≈0, criando uma descontinuidade circular
      // que multiplicava o núcleo em anéis coloridos (o "sol" da captura).
      float edgeOptics = smoothstep(0.76, 0.985, pd);
      vec2 refr = p * (0.075 * (1.0 - sphere)) * edgeOptics;
      vec2 texUv = vec2(0.5) + (p - refr) * INNER_ZOOM;
      // Aberração cromática também limitada à borda, nunca no núcleo.
      vec2 chrom = p * (0.006 * fresnel) * edgeOptics;

      vec3 col = vec3(
        texture2D(uGalaxy, texUv - chrom).r,
        texture2D(uGalaxy, texUv).g,
        texture2D(uGalaxy, texUv + chrom).b
      );

      // preenche o interior com um fundo cósmico suave + micro-estrelas procedurais
      vec3 deepTint = mix(vec3(0.014, 0.020, 0.045), vec3(0.030, 0.025, 0.065), smoothstep(-0.7, 0.9, p.y));
      col += deepTint * 0.10; // sem halo luminoso circular no centro

      vec2 starUv = texUv * 36.0 + vec2(uTime * 0.02, 0.0);
      vec2 cell = floor(starUv);
      vec2 f = fract(starUv) - 0.5;
      float rnd = hash21(cell);
      float star = smoothstep(0.06, 0.0, length(f)) * step(0.986, rnd);
      float star2 = smoothstep(0.035, 0.0, length(fract(texUv * 54.0) - 0.5)) * step(0.9935, hash21(floor(texUv * 54.0)));
      vec3 starCol = mix(vec3(0.72, 0.82, 1.0), vec3(1.0, 0.96, 0.90), step(0.85, rnd));
      col += starCol * (star * 0.18 + star2 * 0.12);

      // iluminação 3D mais suave
      vec3 keyDir = normalize(vec3(-0.55, 0.68, 0.48));
      vec3 fillDir = normalize(vec3(0.72, -0.44, 0.36));
      float keyLight = max(dot(n3, keyDir), 0.0);
      float fillLight = max(dot(n3, fillDir), 0.0);
      col *= 0.97 + keyLight * 0.08 + fillLight * 0.03;

      float bottomShade = smoothstep(-0.16, -0.92, p.y) * (0.18 + 0.18 * (1.0 - sphere));
      col *= 1.0 - bottomShade * 0.18;

      // vidro/reflexo mais contido
      vec3 rimCol = mix(cool, warm, 0.5 + 0.5 * sin(ang + uTime * 0.12));
      col = over(col, rimCol, fresnel * 0.14);
      col += rimCol * pow(smoothstep(0.72, 1.0, pd), 2.8) * 0.04;

      float spec1 = lobe(n3, normalize(vec3(-0.50, 0.62, 0.60)), 60.0);
      float spec2 = lobe(n3, normalize(vec3(-0.15, 0.90, 0.40)), 135.0);
      float spec3 = lobe(n3, normalize(vec3(0.76, -0.18, 0.42)), 80.0);
      col += vec3(1.0) * spec1 * 0.12;
      col += vec3(0.92, 0.97, 1.0) * spec2 * 0.18;
      col += vec3(0.58, 0.74, 1.0) * spec3 * 0.035;

      float ring = exp(-pow((pd - 0.94) * 18.0, 2.0));
      float innerRing = exp(-pow((pd - 0.80) * 10.5, 2.0));
      col += mix(vec3(0.80, 0.92, 1.0), vec3(0.76, 0.60, 1.0), smoothstep(-0.8, 0.8, p.y)) * ring * 0.14;
      col += vec3(0.18, 0.24, 0.40) * innerRing * 0.07;

      float topGlint = exp(-dot(p - vec2(-0.30, 0.38), p - vec2(-0.30, 0.38)) * 24.0);
      col += vec3(0.96, 0.98, 1.0) * topGlint * 0.10;

      col *= 1.0 - smoothstep(0.93, 1.0, pd) * 0.06;

      float alpha = 1.0 - smoothstep(0.992 - SOFT, 1.008 + SOFT, pd);
      gl_FragColor = vec4(clamp(col, 0.0, 1.0), alpha);
    }
  `;

  let particleProgram, glassProgram;
  try {
    particleProgram = program(particleVert, particleFrag);
    glassProgram = program(orbVert, orbFrag);
  } catch (err) {
    releaseGraphics();
    onUnavailable();
    return () => {};
  }

  const vertexBuffer = gl.createBuffer();
  buffers.push(vertexBuffer);
  gl.bindBuffer(gl.ARRAY_BUFFER, vertexBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, vertexData, gl.STATIC_DRAW);
  const quadBuffer = gl.createBuffer();
  buffers.push(quadBuffer);
  gl.bindBuffer(gl.ARRAY_BUFFER, quadBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,1,-1,-1,1,-1,1,1,-1,1,1]), gl.STATIC_DRAW);

  const pAttr = ['aPos','aCol','aSize','aAlpha','aType'].map(k => gl.getAttribLocation(particleProgram,k));
  const pUniform = Object.fromEntries(['uSpin','uTilt','uDiagonal','uParallax','uExtent','uDpr']
    .map(k => [k,gl.getUniformLocation(particleProgram,k)]));
  const orbAttr = gl.getAttribLocation(glassProgram,'aPos');
  const orbUniform = Object.fromEntries(['uGalaxy','uTime']
    .map(k => [k,gl.getUniformLocation(glassProgram,k)]));

  function resize() {
    if (disposed || lost) return;
    const width = Math.max(1, canvas.getBoundingClientRect().width);
    const dpr = Math.min(window.devicePixelRatio || 1, CONFIG.maxDpr);
    const side = Math.max(4, Math.round(width * dpr));
    if (side === physicalSize) return;
    physicalSize = side;
    canvas.width = side;
    canvas.height = side;
    if (texture) gl.deleteTexture(texture);
    if (fbo) gl.deleteFramebuffer(fbo);
    texture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,side,side,0,gl.RGBA,gl.UNSIGNED_BYTE,null);
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);
    fbo = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER,fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER,gl.COLOR_ATTACHMENT0,gl.TEXTURE_2D,texture,0);
    if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE)
      console.error('Framebuffer da galáxia incompleto.');
    gl.bindFramebuffer(gl.FRAMEBUFFER,null);
  }
  const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => {
    resize();
    if (motion.matches && !document.hidden && inView && !disposed && !lost) animate(performance.now());
  }) : null;
  if (observer) observer.observe(canvas);
  function onResize() { resize(); if (motion.matches) refreshMotion(); }
  window.addEventListener('resize',onResize,{passive:true});
  resize();

  const pointer = { x:0, y:0, sx:0, sy:0 };
  function onPointerMove(e) {
    const rect = canvas.getBoundingClientRect();
    pointer.x = ((e.clientX - rect.left)/rect.width - .5)*2;
    pointer.y = ((e.clientY - rect.top)/rect.height - .5)*2;
  }
  function onPointerLeave() { pointer.x=0; pointer.y=0; }
  canvas.addEventListener('pointermove', onPointerMove, {passive:true});
  canvas.addEventListener('pointerleave', onPointerLeave);

  let spinAngle = 0;
  let startTime = performance.now();
  let last = startTime;
  function animate(now) {
    if (disposed || lost || !inView || document.hidden) { frame = null; return; }
    frame = motion.matches ? null : requestAnimationFrame(animate);
    resize();
    const delta = motion.matches ? 0 : Math.min((now - last)/1000,0.05);
    last = now;
    spinAngle += CONFIG.rotationBase*CONFIG.speedMultiplier*CONFIG.rotationDirection*delta;
    pointer.sx += (pointer.x - pointer.sx)*Math.min(1,delta*2.4);
    pointer.sy += (pointer.y - pointer.sy)*Math.min(1,delta*2.4);

    // Passagem 1: estrelas e glow da galáxia em textura RGBA interna.
    gl.bindFramebuffer(gl.FRAMEBUFFER,fbo);
    gl.viewport(0,0,physicalSize,physicalSize);
    gl.clearColor(0.010,0.014,0.032,1);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.useProgram(particleProgram);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA,gl.ONE);
    gl.bindBuffer(gl.ARRAY_BUFFER,vertexBuffer);
    const stride=9*4;
    [[pAttr[0],3,0],[pAttr[1],3,3*4],[pAttr[2],1,6*4],[pAttr[3],1,7*4],[pAttr[4],1,8*4]].forEach(([loc,sz,offset])=>{
      if (loc >= 0) {
        gl.enableVertexAttribArray(loc);
        gl.vertexAttribPointer(loc,sz,gl.FLOAT,false,stride,offset);
      }
    });
    gl.uniform1f(pUniform.uSpin,spinAngle);
    gl.uniform1f(pUniform.uTilt,CONFIG.tilt + pointer.sy*CONFIG.parallax*.95);
    gl.uniform1f(pUniform.uDiagonal,CONFIG.diagonal);
    gl.uniform2f(pUniform.uParallax,pointer.sx*CONFIG.parallax*.55,pointer.sx*CONFIG.parallax*.14);
    gl.uniform1f(pUniform.uExtent,CONFIG.worldExtent);
    gl.uniform1f(pUniform.uDpr,physicalSize/Math.max(1,canvas.getBoundingClientRect().width));
    gl.drawArrays(gl.POINTS,0,pIndex);
    pAttr.forEach(loc=>{if(loc>=0)gl.disableVertexAttribArray(loc);});

    // Passagem 2: somente a ESFERA DE VIDRO, usando estrelas como interior.
    gl.bindFramebuffer(gl.FRAMEBUFFER,null);
    gl.viewport(0,0,physicalSize,physicalSize);
    gl.disable(gl.BLEND);
    gl.clearColor(0,0,0,0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.useProgram(glassProgram);
    gl.bindBuffer(gl.ARRAY_BUFFER,quadBuffer);
    gl.enableVertexAttribArray(orbAttr);
    gl.vertexAttribPointer(orbAttr,2,gl.FLOAT,false,0,0);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D,texture);
    gl.uniform1i(orbUniform.uGalaxy,0);
    gl.uniform1f(orbUniform.uTime,motion.matches ? 0 : (now-startTime)/1000);
    gl.drawArrays(gl.TRIANGLES,0,6);
    gl.disableVertexAttribArray(orbAttr);
  }
  function refreshMotion() {
    if (frame != null) cancelAnimationFrame(frame);
    frame = null;
    last = performance.now();
    if (!document.hidden && inView && !disposed && !lost) animate(last);
  }
  motion.addEventListener('change', refreshMotion);
  refreshMotion();

  const intersection = typeof IntersectionObserver !== 'undefined'
    ? new IntersectionObserver(entries => {
      inView = entries[0]?.isIntersecting ?? false;
      refreshMotion();
    }) : null;
  intersection?.observe(canvas);
  function onContextLost(event) {
    event.preventDefault();
    lost = true;
    refreshMotion();
    onUnavailable();
  }
  canvas.addEventListener('webglcontextlost', onContextLost);
  document.addEventListener('visibilitychange', refreshMotion);
  return () => {
    disposed = true;
    if (frame != null) cancelAnimationFrame(frame);
    observer?.disconnect();
    intersection?.disconnect();
    motion.removeEventListener('change', refreshMotion);
    document.removeEventListener('visibilitychange', refreshMotion);
    window.removeEventListener('resize', onResize);
    canvas.removeEventListener('pointermove', onPointerMove);
    canvas.removeEventListener('pointerleave', onPointerLeave);
    canvas.removeEventListener('webglcontextlost', onContextLost);
    releaseGraphics();
  };
}
