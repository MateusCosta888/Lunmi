# Arquitetura

A apresentação é separada dos mecanismos de inferência e execução. Mudar a interface não exige reconstruir Brain, memória ou ferramentas Windows.

```text
Renderer React
    │ API limitada do preload
Electron main + supervisor
    │ JSONL versionado por stdio
Bridge Python
    │
TaskController ─── MemoryManager / SQLite
    │
    ├── Chat automático → Brain → texto ou decisão de ferramenta validada
    │
    ├── Operação Windows → caminho determinístico ou UFO²
                       │ HostAgent / AppAgent
                       │ UIA / Win32 / filesystem
    │                  └ resultado + verificação
    └── Email → integração Gmail restrita → Composio da própria conta
                       └ leitura não confiável / prévia manual de envio
```

## Interface e comunicação

O modo residente reutiliza a bridge e a sessão da janela principal. `desktop/electron/chat-projection.ts` mantém a projeção canónica em RAM, incluindo o parser de streaming; principal e companion recebem snapshots/deltas por sessão e cursor. O companion não monta outro App/useSession nem cria outra conversa. Rascunho e mensagens são partilhados, sem persistência adicional de conversa pelo Node.

`desktop/electron/companion.ts` e `desktop/src/shared/node.ts` implementam a janela única do Node: arco/círculo → cápsula → mini chat. A shape Win32 é a autoridade de desenho e input; o renderer só desenha o conteúdo da superfície assentada. O main deriva estados reais, gere foco, passividade, atalho, migração/preferências e timer de conclusão. A allowlist do mini mantém `chat.send`, `task.cancel`, `voice.get` e `voice.stop`; confirmações pertencem à principal.

`app/voice/manager.py` supervisiona um worker externo com protocolo JSONL, gerações e cancelamento. A bridge observa resultados elegíveis sem alterar o TaskController. `eligibility.py`/`filter.py` limitam o texto falado; `worker_main.py` carrega o runtime de áudio separado e sintetiza/reproduz localmente. A fala não instala modelos e não é um segundo Brain. Preferências próprias, último texto apenas em RAM, sem captura de microfone. Indisponibilidade do runtime não impede a execução normal do chat.

- `desktop/src` contém componentes, navegação, formulários, temas e estado de apresentação.
- O renderer não recebe Node.js, shell, filesystem ou IPC bruto. O preload oferece uma API limitada.
- `desktop/electron` supervisiona o Python, valida a origem de comandos e controla diálogos nativos.
- `app/bridge` valida envelopes/payloads e publica DTOs. Sessões e tarefas têm identidade própria.
- Reiniciar uma sessão não repete uma tarefa. Eventos antigos não completam uma tarefa nova.

## Inferência e execução

`app/brain` concentra provedores e transportes. A configuração distingue Brain principal e Brain do AppAgent, quando configurado.

`app/core/controller.py` mantém tarefa, política, cancelamento, recuperação de contexto e resultado. `app/computer` integra ferramentas verificadas e o worker UFO². `install_ufo.py` obtém a fonte upstream na revisão/hashes de `ufo.lock.json`; `vendor/` não é versionado.

O app exclui sua própria árvore de processos da seleção de alvos. Autorização, despacho e verificação são conceitos separados. Uma operação incerta não deve virar repetição silenciosa.

Streaming usa os eventos públicos do IPC existente, com identidades de conversa, sessão, tarefa e resposta. Texto incremental, texto estável e resultados verificados são conceitos separados; raciocínio interno não é publicado. O suporte pertence a cada transporte, sem simular streaming nos restantes. Os gestores de voz e ditado comunicam com workers em runtimes separados; não partilham raciocínio interno ou credenciais com o renderer.

`app/integrations` mantém OAuth e ferramentas Gmail limitadas. O modelo não escolhe arbitrariamente a conta ligada. Conteúdo de email é dado externo não confiável, não autorização. Rascunhos pendentes ficam em RAM; enviar exige confirmação manual e não é repetido automaticamente após resultado incerto.

## Memória e dados

`app/memory` mantém o documento canónico de perfil em SQLite, um índice FTS derivado, revisão para edição concorrente e um nível de Desfazer. Propostas legadas, episódios e histórico opt-in permanecem na gestão detalhada. Perfil, janela de turnos em RAM e episódios têm papéis diferentes. O contexto é limitado; conteúdo recuperado pode acompanhar chamadas ao provedor de IA.

O frontend usa essa API, sem abrir SQLite. Importação e operações destrutivas mantêm confirmação e validação no backend.

`app/chat_history.py` guarda conversas e fontes públicas num SQLite próprio. O backend controla
a conversa ativa e restaura apenas o seu contexto elegível. O histórico é separado da memória
pessoal e não repete ferramentas, confirmações ou fala durante a recuperação. As duas janelas
recebem a mesma projeção pública. Pesquisa web mantém conteúdo de trabalho privado em RAM;
apenas as fontes públicas elegíveis acompanham a resposta e o histórico.

Código e lockfiles são públicos. Configurações de máquina, credenciais, bancos, exportações e evidências de execução são locais e ignorados pelo Git. O checkout não é um instalador empacotado; prepare dependências conforme o README. No pacote instalado, o runtime Python e a voz ficam como componentes em `<pasta de instalação>-runtime\<nome>-<hash>`, resolvidos por `componentDir` em `desktop/electron/backend.ts` a partir de `resources/lunmi-components.json`; ver `docs/UPDATES.md`.
