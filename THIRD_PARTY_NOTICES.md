# Componentes e avisos de terceiros

As licenças originais continuam aplicáveis. Este documento não concede uma licença geral ao código próprio da Lunmi.

| Projeto | Uso | Licença / aviso |
| --- | --- | --- |
| [Microsoft UFO²](https://github.com/microsoft/UFO) | HostAgent/AppAgent e infraestrutura obtidos na revisão fixada | MIT; checkout conserva o LICENSE upstream. Vendor não versionado. |
| [Electron](https://www.electronjs.org/) | Shell desktop | MIT e avisos dos componentes Electron/Chromium. |
| [React](https://react.dev/) | Interface | MIT. |
| [Lucide](https://lucide.dev/) | Ícones selecionados | [ISC e avisos Feather MIT](desktop/licenses/Lucide-LICENSE.txt). |
| [Plus Jakarta Sans](https://github.com/tokotype/PlusJakartaSans) | Fonte local | [SIL Open Font License 1.1](desktop/src/assets/fonts/OFL.txt). |
| [Geist](https://github.com/vercel/geist-font) | Fonte da marca central embarcada (`desktop/src/assets/fonts/Geist-Variable.ttf`) e lettering da logo pública convertido em curvas | [SIL Open Font License 1.1](desktop/licenses/Geist-OFL.txt). |
| [resedit](https://github.com/jet2jet/resedit-js) | Ferramenta de build para ícone e identidade do executável Windows | MIT; dependência de desenvolvimento, sem código no renderer. |
| [React Bits](https://reactbits.dev/) | Adaptações ThoughtLine/RubberSegment/WarpText/MagicBento e Particles integrados ao app | [MIT + Commons Clause](desktop/licenses/ReactBits-LICENSE.md); restrições dos componentes continuam válidas. Avisos por componente: [WarpText](desktop/licenses/react-bits-WarpText.txt), [Particles](desktop/licenses/react-bits-Particles.txt), [MagicBento](desktop/licenses/react-bits-MagicBento.txt). WarpText permanece no projeto, mas a marca central usa Kinetic Text. |
| [Magic UI](https://magicui.design/) | Kinetic Text adaptado para a marca central do topo | [MIT](desktop/licenses/magicui-KineticText.txt); restrições do componente continuam válidas. |
| [ogl](https://github.com/oframe/ogl) | Renderizador WebGL exigido pela fonte oficial do WarpText | [Unlicense](https://github.com/oframe/ogl/blob/master/LICENSE) (domínio público); versão fixada `1.0.11`, sem fork. |
| [Aceternity UI](https://ui.aceternity.com/) | Referências/adaptação do fundo estrelado integrado | [Aceternity License](https://ui.aceternity.com/licence); não atribuir MIT nem redistribuir itens como biblioteca/template/pacote de componentes. |

Fontes e detalhes visuais: [desktop/THIRD-PARTY-MOTION.md](desktop/THIRD-PARTY-MOTION.md). O produto é a aplicação Lunmi, não um catálogo de componentes extraídos. Um aviso não autoriza usos proibidos pela licença original.

Demais dependências e versões estão nos lockfiles. Instalações mantêm seus avisos. Antes de distribuir binários, revise os avisos completos do Electron/Chromium e de todos os pacotes incluídos.
