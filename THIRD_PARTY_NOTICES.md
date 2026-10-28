# Componentes e avisos de terceiros

As licenças originais continuam aplicáveis. Este documento não concede uma licença geral ao código próprio da Lunmi.

| Projeto | Uso | Licença / aviso |
| --- | --- | --- |
| [Microsoft UFO²](https://github.com/microsoft/UFO) | HostAgent/AppAgent e infraestrutura obtidos na revisão fixada | MIT; checkout conserva o LICENSE upstream. Vendor não versionado. |
| [Electron](https://www.electronjs.org/) | Shell desktop | MIT e avisos dos componentes Electron/Chromium. |
| [electron-updater](https://github.com/electron-userland/electron-builder/tree/master/packages/electron-updater) | Downloads diferenciais e atualizações NSIS | MIT; licença incluída no pacote npm. A verificação Ed25519 usa `node:crypto`. |
| [React](https://react.dev/) | Interface | MIT. |
| [Lucide](https://lucide.dev/) | Ícones selecionados | [ISC e avisos Feather MIT](desktop/licenses/Lucide-LICENSE.txt). |
| [Plus Jakarta Sans](https://github.com/tokotype/PlusJakartaSans) | Fonte local | [SIL Open Font License 1.1](desktop/src/assets/fonts/OFL.txt). |
| [Geist](https://github.com/vercel/geist-font) | Fonte da marca central embarcada (`desktop/src/assets/fonts/Geist-Variable.ttf`) e lettering da logo pública convertido em curvas | [SIL Open Font License 1.1](desktop/licenses/Geist-OFL.txt). |
| [resedit](https://github.com/jet2jet/resedit-js) | Ferramenta de build para ícone e identidade do executável Windows | MIT; dependência de desenvolvimento, sem código no renderer. |
| [React Bits](https://reactbits.dev/) | Adaptações ThoughtLine/RubberSegment/WarpText/MagicBento/BranchedMenu e Particles integrados ao app | [MIT + Commons Clause](desktop/licenses/ReactBits-LICENSE.md); restrições dos componentes continuam válidas. Avisos por componente: [WarpText](desktop/licenses/react-bits-WarpText.txt), [Particles](desktop/licenses/react-bits-Particles.txt), [MagicBento](desktop/licenses/react-bits-MagicBento.txt), [BranchedMenu](desktop/licenses/react-bits-BranchedMenu.txt). WarpText permanece no projeto, mas a marca central usa Kinetic Text. |
| [Magic UI](https://magicui.design/) | Kinetic Text adaptado para a marca central do topo | [MIT](desktop/licenses/magicui-KineticText.txt); restrições do componente continuam válidas. |
| [ogl](https://github.com/oframe/ogl) | Renderizador WebGL exigido pela fonte oficial do WarpText | [Unlicense](https://github.com/oframe/ogl/blob/master/LICENSE) (domínio público); versão fixada `1.0.11`, sem fork. |
| [Aceternity UI](https://ui.aceternity.com/) | Referências/adaptação do fundo estrelado integrado | [Aceternity License](https://ui.aceternity.com/licence); não atribuir MIT nem redistribuir itens como biblioteca/template/pacote de componentes. |
| [Supertonic](https://github.com/supertone-inc/supertonic-py) | Síntese de voz opcional (`supertonic==1.3.1`), executada num runtime local separado fora do payload (`<state_root>/.local/voice/runtime`); modelos pinados em `.local/voice/models`, com download automático desligado | MIT; o SDK e os modelos não são embarcados no instalador nesta entrega. |
| [python-sounddevice](https://python-sounddevice.readthedocs.io/) / PortAudio | Saída de áudio opcional no mesmo runtime local separado (`sounddevice==0.5.6`); nenhuma captura de microfone é usada | MIT (sounddevice) e licença do PortAudio; não embarcados no payload do produto. |
| [websockets](https://github.com/python-websockets/websockets) | Transporte local do host Spotify (`websockets==15.0.1`) no runtime Python | BSD-3-Clause; aviso integral em [desktop/licenses/websockets-LICENSE.txt](desktop/licenses/websockets-LICENSE.txt). |
| [Spicetify CLI](https://github.com/spicetify/cli) | Binário pinado 2.46.2 obtido no setup para o perfil privado do Spotify; não é embarcado no instalador nesta entrega | LGPL-2.1; a distribuição pública do binário exige cumprir os termos da licença e do projeto upstream. |
| [NumPy](https://numpy.org/) | Arrays de áudio exigidos pelo runtime de voz separado; a bridge do produto não o carrega para voz | BSD-3-Clause e avisos dos componentes (0BSD, MIT, Zlib, CC0-1.0); não embarcado como dependência de voz do produto. |

Fontes e detalhes visuais: [desktop/THIRD-PARTY-MOTION.md](desktop/THIRD-PARTY-MOTION.md). O produto é a aplicação Lunmi, não um catálogo de componentes extraídos. Um aviso não autoriza usos proibidos pela licença original.

Demais dependências e versões estão nos lockfiles. Instalações mantêm seus avisos. Antes de distribuir binários, revise os avisos completos do Electron/Chromium e de todos os pacotes incluídos.


### Galaxy Orb / VoiceOrbs (integração anterior)

O orb fornecido para a Lunmi atribui o vidro e o efeito óptico Siri Sheet ao [VoiceOrbs](https://github.com/amunozdev/voiceorbs), copyright (c) 2026 Alexis Munoz. Código adaptado em `desktop/src/features/chat/orb/galaxyRenderer.js`; licença MIT integral em `desktop/licenses/voiceorbs-MIT.txt`, incluída pela pasta de licenças do packaging existente. Galáxia e apresentação fornecidas pelo autor; alterações de integração Lunmi descritas em `desktop/THIRD-PARTY-MOTION.md`.

Esta implementação foi substituída pelo Live Orb; o aviso anterior permanece por atribuição histórica.

### Live Orb (implementação atual)

O motor atual em `desktop/src/features/chat/orb/liveRenderer.js` adapta o Live Orb de
radiumcoders (Jay Sharma), com material procedural de partículas e ciclo de aplicação Lunmi.
A [página pública do componente](https://preview.21st.dev/@radiumcoders/components/live-orb)
declara MIT; a fonte é https://23rd.dev/r/live-orb.json. Texto da licença e atribuição em
[desktop/licenses/radiumcoders-LiveOrb-MIT.txt](desktop/licenses/radiumcoders-LiveOrb-MIT.txt).
# App and provider marks used in Lunmi settings (2026-10-27)

Provider marks are bundled from [LobeHub lobe-icons](https://github.com/lobehub/lobe-icons),
commit `8b7bbc433d38437fa2b0cca58ae896917162b812`, under the MIT license
(`desktop/licenses/LobeHub-icons-MIT.txt`). Source URLs and asset hashes are recorded
with the provider assets. Company and product marks remain their owners' trademarks.

The Gmail icon is the unmodified Google product icon from
https://knowledge.workspace.google.com/static/images/icons/product/gmail.svg.
Gmail and its icon are trademarks of Google LLC. It identifies the Gmail integration;
use follows [Google product co-branding guidance](https://about.google/brand-resource-center/guidance/apis/).

The Spotify icon is the unmodified green icon from the official
[Spotify media kit](https://newsroom.spotify.com/media-kit/logo-and-brand-assets/).
Spotify and its icon are trademarks of Spotify AB. It identifies the Spotify integration;
use follows [Spotify design guidance](https://developer.spotify.com/documentation/design).
These brand assets are not offered under the project's open-source license.
