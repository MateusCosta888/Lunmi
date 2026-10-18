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

- `desktop/src` contém componentes, navegação, formulários, temas e estado de apresentação.
- O renderer não recebe Node.js, shell, filesystem ou IPC bruto. O preload oferece uma API limitada.
- `desktop/electron` supervisiona o Python, valida a origem de comandos e controla diálogos nativos.
- `app/bridge` valida envelopes/payloads e publica DTOs. Sessões e tarefas têm identidade própria.
- Reiniciar uma sessão não repete uma tarefa. Eventos antigos não completam uma tarefa nova.

## Inferência e execução

`app/brain` concentra provedores e transportes. A configuração distingue Brain principal e Brain do AppAgent, quando configurado.

`app/core/controller.py` mantém tarefa, política, cancelamento, recuperação de contexto e resultado. `app/computer` integra ferramentas verificadas e o worker UFO². `install_ufo.py` obtém a fonte upstream na revisão/hashes de `ufo.lock.json`; `vendor/` não é versionado.

O app exclui sua própria árvore de processos da seleção de alvos. Autorização, despacho e verificação são conceitos separados. Uma operação incerta não deve virar repetição silenciosa.

Streaming usa os eventos públicos do IPC existente, com identidades de sessão, tarefa e resposta. Texto incremental, texto estável e resultados verificados são conceitos separados; raciocínio interno não é publicado. O suporte pertence a cada transporte, sem simular streaming nos restantes. Não há sistema de voz neste lote.

`app/integrations` mantém OAuth e ferramentas Gmail limitadas. O modelo não escolhe arbitrariamente a conta ligada. Conteúdo de email é dado externo não confiável, não autorização. Rascunhos pendentes ficam em RAM; enviar exige confirmação manual e não é repetido automaticamente após resultado incerto.

## Memória e dados

`app/memory` mantém o documento canónico de perfil em SQLite, um índice FTS derivado, revisão para edição concorrente e um nível de Desfazer. Propostas legadas, episódios e histórico opt-in permanecem na gestão detalhada. Perfil, janela de turnos em RAM e episódios têm papéis diferentes. O contexto é limitado; conteúdo recuperado pode acompanhar chamadas ao provedor de IA.

O frontend usa essa API, sem abrir SQLite. Importação e operações destrutivas mantêm confirmação e validação no backend.

Código e lockfiles são públicos. Configurações de máquina, credenciais, bancos, exportações e evidências de execução são locais e ignorados pelo Git. O checkout não é um instalador empacotado; prepare dependências conforme o README.
