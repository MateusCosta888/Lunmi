# Motion references incorporated into Lunmi

## React Bits — David Haz

Adaptations: `src/shared/RubberNavigation.tsx` (RubberSegment two-edge dilation, vertical navigation and native Web Animations instead of Motion); `src/features/chat/ThoughtLine.tsx` and `src/shared/motion.css` (ThoughtLine shimmer, breathing, timer and folding public-state trace, using Lunmi icons). These are intentionally scoped adaptations, not unchanged copies or a claim of pixel identity.

Sources, verified2026-10-15, commit43591e8b3fa327d45633b4e8239ff6742ef706ef:
- https://reactbits.dev/micro/thought-line
- https://reactbits.dev/micro/rubber-segment?radius=8
- https://github.com/DavidHDev/react-bits/tree/43591e8b3fa327d45633b4e8239ff6742ef706ef/src/ts-default/Micro

License: MIT + Commons Clause. Full notice: [ReactBits-LICENSE.md](licenses/ReactBits-LICENSE.md). Incorporated into this application only, not offered as a component pack. Native button semantics, reduced motion, wrapping, task events and cleanup were adapted.

## Aceternity UI / Vijay Verma

`src/shared/Starfield.tsx` adapts the star density/twinkle and shooting-star gradient/travel from the free components StarsBackground and ShootingStars, in one bounded Canvas. Changes: Lunmi palette, southwest trajectory, time-based speed, single occasional meteor, DPR1.5 cap, up to180stars, paint cap30fps, no per-frame React state, visibility/reduced-motion/pause and lifecycle cleanup.

Sources:
- https://ui.aceternity.com/components/shooting-stars-and-stars-background
- https://ui.aceternity.com/registry/shooting-stars.json
- https://ui.aceternity.com/registry/stars-background.json
- https://ui.aceternity.com/licence

The site permits use/modification in end products. Its source/template redistribution restrictions remain applicable. No Pro template was downloaded; this is not an MIT attribution for Aceternity.

## Lucide icons — 2026-10-15

The shared icon primitive now uses selected named imports from lucide-react1.57.0. License: ISC with Feather-derived icons under their retained MIT notice; [full packaged notice](licenses/Lucide-LICENSE.txt). References: https://lucide.dev/guide/react and https://lucide.dev/license. No icon webfont, external font request or full icon catalog is loaded at runtime. Lunmi BrandMark remains its own authored mark.

## liquid-glass-react — 2026-10-15

`src/shared/GlassSurface.tsx` uses liquid-glass-react1.1.1 (MIT, © 2025 Max Rovensky / rdev) for the real side-panel glass: SVG displacement refraction plus backdrop-filter over the global cosmos. License notice: [licenses/liquid-glass-react-MIT.txt](licenses/liquid-glass-react-MIT.txt). Source: https://github.com/rdev/liquid-glass-react. Adaptations in this app only: the package returns a Fragment whose container plus four utility siblings carry inline `top/left:50%` and `translate(-50%)`; all of them are confined inside a dedicated absolute, pointer-events-none, aria-hidden `.lunmi-glass-layer` (position/top/left "0px" passed through props; residual transform neutralized by scoped CSS), while app content lives in a sibling plane with its own height and real scrolling — nothing in the layout depends on the package's internal tree. The layer is used as a decorative panel background (radius 18, `padding:0`, tint from theme tokens), mouse tracking and elasticity are disabled by passing static `globalMousePos`/`mouseOffset` (the package then attaches no mousemove listener), mode is `standard` with discrete refraction/aberration, and the experimental shader mode is not used. Installed with `npm install --ignore-scripts` by the coordinator; npm audit reported zero vulnerabilities.

## React Bits Particles — 2026-10-16

The main shell now uses the current official TypeScript/CSS [Particles source](https://github.com/DavidHDev/react-bits/tree/main/src/ts-default/Backgrounds/Particles), vendored in `src/shared/Particles.tsx` and `Particles.css`. It replaces the Aceternity star/meteor background in the shell; the separately authored startup animation remains intact. The registry JSON still served an older OGL version when inspected, so that installation artifact was not used. This current source implements all requested properties, uses WebGL2 directly, and adds no package dependency. Copyright David Haz, 2026, **MIT + Commons Clause**, full notice in [licenses/react-bits-Particles.txt](licenses/react-bits-Particles.txt); component resale is restricted by that notice.

Lunmi integration: one full-shell canvas, the requested 460-particle preset, theme colors, global passive pointer events with no hit interception, live reduced-motion/visibility pause. The upstream renderer has a paused `wake()` path that paints a static frame without scheduling RAF. The optical-glass refinement also calibrates light-mode ink luminance from 0.06 to 0.09 and its glow multiplier from 0.3 to 0.4, making theme-colored particles more visible. `Starfield.tsx` keeps its existing caller/test entry point and delegates to Particles. Floating glass uses a smoked navy Celestial material and refraction blended beneath the content; the app's buttons and navigation stay in a separate interactive plane.

## Magic UI Kinetic Text — 2026-10-16

A marca central do topo (`Lunmi`) usa o [Kinetic Text](https://magicui.design/docs/components/kinetic-text) do Magic UI, registry `@magicui/kinetic-text`, portado para TypeScript em `src/shared/KineticText.tsx` + `KineticText.css`. Cada caractere é um `span` com peso base300; ao passar o ponteiro, a letra atinge900 com contorno de texto, as vizinhas imediatas vão para600 e as duas casas afastadas para400, numa transição de0,4s. Adaptações deste projeto (nada além disto): o arquivo oficial é Tailwind com variantes arbitrárias e o helper `cn` — aqui o mesmo efeito foi escrito como CSS autoral (este projeto não usa Tailwind), com tipos TypeScript; um rótulo oculto com o texto completo (`sr-only`) mantém a palavra legível para leitores de tela e as letras ficam `aria-hidden`, para não duplicar a leitura. Nenhuma dependência de pacote é adicionada. Licença: MIT, © Magic UI; aviso completo em [licenses/magicui-KineticText.txt](licenses/magicui-KineticText.txt). Redistribuição do componente — inclusive uma versão portada — continua restrita; ele é incorporado a esta aplicação, não oferecido como pacote de componentes. O WarpText abaixo permanece no projeto, porém não é mais a marca central.

## Geist Sans — fonte da marca central (2026-10-16)

A marca central usa a **Geist Sans** variável (`desktop/src/assets/fonts/Geist-Variable.ttf`, eixo `wght` 100–900), declarada em `@font-face` local no `src/styles.css` — nenhuma requisição externa de fonte em tempo de execução. É a mesma família do lettering do símbolo Lunmi; o resto da interface continua em Plus Jakarta Sans. O eixo até 900 é o que permite ao Kinetic Text atingir o peso 900 do hover (o Plus Jakarta Sans, 200–800, o limitava a 800). Arquivo obtido do catálogo oficial Google Fonts (`ofl/geist/Geist[wght].ttf`). Licença: SIL Open Font License 1.1, © 2024 The Geist Project Authors; aviso completo em [licenses/Geist-OFL.txt](licenses/Geist-OFL.txt).

## React Bits WarpText — 2026-10-16 (revisado 2026-10-16 contra `main`)

O [WarpText](https://reactbits.dev/text-animations/warp-text) oficial, registry `@react-bits/WarpText-JS-CSS`, está portado para TypeScript em `src/shared/WarpText.tsx` + `WarpText.css`. Ele foi a marca central do topo até 2026-10-16; agora a marca usa Kinetic Text e este componente permanece no projeto (testado), porém não é mais exibido no shell. Fonte conferida nesta tarefa contra o `main` do repositório: [WarpText.jsx](https://github.com/DavidHDev/react-bits/blob/main/src/content/TextAnimations/WarpText/WarpText.jsx) e o mesmo `WarpText.css`. O arquivo oficial importa `Renderer, Program, Mesh, Triangle, Texture` de `ogl` e desenha o texto por um shader WebGL2 (`#version 300 es`) com fbm ambiente, lente de ponteiro, ondulação, dispersão cromática e onda de clique — este projeto usa o mesmo shader, com os mesmos nomes de uniformes. **Declaração honesta das props:** a revisão oficial atual DECLARA `warpStrength`, `warpScale`, `speed`, `pointerInfluence`, `pointerStrength`, `refraction`, `chromatic`, `ripple`, `clickRipple`, `idleDrift`, `lens`, `intro`, `fontSize`, `fontWeight`, `fontFamily`, `letterSpacing` e `lineHeight`; todas são repassadas pelo cabeçalho da Lunmi e nenhuma é decorativa. Uma nota anterior desta tarefa dizia que a fonte não declarava `lens`/`chromatic`/`idleDrift`/`clickRipple`/`intro`; a leitura direta do `main` em 2026-10-16 mostrou o contrário e a afirmação foi corrigida aqui. Desvio declarado do padrão do upstream: `chromatic` upstream é `0` e a Lunmi pede `0,35` (mais dispersão).

Adaptações deste projeto (nada além disto): tipos TypeScript e superfície explícita de props (o arquivo oficial é JSX sem tipos); rótulo DOM real "Lunmi" sempre presente para acessibilidade e exibido como fallback quando o WebGL não existe, o contexto se perde ou a preferência de transparência reduzida está ativa (o oficial só caía para texto em falha e usava `role="img"`); canvas `aria-hidden` para não duplicar a leitura; contexto nulo tratado explicitamente; efeito que só reexecuta quando a preferência de transparência muda, para troca de tema não multiplicar o laço de RAF; movimento reduzido totalmente estático (sem deriva, sem warpa de ponteiro e sem onda de clique — o oficial ainda ondulava); e **piso de ~1,2 px físicos** no deslocamento do ponteiro (`pointerWarp`, `dir * bulge * uPointerStrength * pixel * 3.2`), porque em uma cápsula de ~30 px de altura o termo em UV do upstream fica abaixo de um pixel e o efeito do ponteiro desaparece; no tamanho de vitrine do upstream esse piso é menor que o termo original. Licença: MIT + Commons Clause, © 2026 David Haz; aviso completo em [licenses/react-bits-WarpText.txt](licenses/react-bits-WarpText.txt) (texto de licença do repositório React Bits, o mesmo que cobre Particles e MagicBento). Redistribuição do componente — inclusive uma versão portada — continua restrita; ele é incorporado a esta aplicação, não oferecido como pacote.

## ogl — 1.0.11 (dependência exigida pela fonte oficial)

`ogl@1.0.11` é `npm install --save-exact` porque é a única coisa que o WarpText oficial importa (`Renderer`, `Program`, `Mesh`, `Triangle`, `Texture`); nenhum outro pacote entrou para o texto. Licença verificada no próprio pacote instalado: o campo `license` do `package.json` é `Unlicense` e a seção "Unlicense" do `README.md` traz o texto de domínio público ("free and unencumbered software released into the public domain"). O pacote **não** traz arquivo `LICENSE` dentro do `node_modules`; o arquivo fica no repositório, https://github.com/oframe/ogl/blob/master/LICENSE. Uso aqui: apenas o caminho de renderização do WarpText, sem fork nem alteração do pacote.

## React Bits MagicBento — 2026-10-16

The bounded pointer-light mechanism ("border glow", spotlight and a finite click wave) is adapted from the official MagicBento component into `src/shared/bento.tsx` plus CSS variables (`--bento-*`) consumed by the composer surface and the two side glass shells. Only the mechanism is reused: exponential light follow (τ≈0.05 s), faster in/out fades for border proximity, a 1.1 s wave whose radius eases as `1-(1−p)^2.2` and whose alpha decays as `(1−p)^1.3`, and a single RAF loop that only lives while the pointer moves — parked, it stops with every variable settled. Sources, verified 2026-10-16 against the main branch: https://github.com/DavidHDev/react-bits/blob/main/src/content/Components/MagicBento/MagicBento.jsx and `.css`. License: MIT + Commons Clause (© 2026 David Haz); full notice in [licenses/react-bits-MagicBento.txt](licenses/react-bits-MagicBento.txt) — resale, sublicensing or redistribution of the component itself (including a ported version) stays restricted.

Declared inert parts: `enableStars`/`particleCount` are not implemented (no canvas, no stars); tilt and magnetism are off (no card transforms); `textAutoHide` stays off (labels never hide); the intro is a single short wave per surface. Reduced motion and hidden windows are honored at runtime — the engine clears its variables and refuses new frames — and unmount removes listeners, timers and variables. Decorative wave/spotlight layers are `pointer-events: none`; they never capture click, drag or selection.


## Galaxy Orb — 2026-10-21

HTML visual fornecido pelo autor da Lunmi, extraído para `src/features/chat/orb/galaxyRenderer.js` com quatro shaders preservados. O comentário de origem atribui o vidro/efeito óptico Siri Sheet ao [VoiceOrbs](https://github.com/amunozdev/voiceorbs). Licença MIT oficial, copyright 2026 Alexis Munoz, arquivada em `licenses/voiceorbs-MIT.txt` (origem https://raw.githubusercontent.com/amunozdev/voiceorbs/main/LICENSE). Alterações Lunmi: ciclo React, limpeza GL/listeners/RAF, visibilidade/movimento reduzido, fallback e apresentação persistente no chat. Sem pacote ou código remoto em runtime.
