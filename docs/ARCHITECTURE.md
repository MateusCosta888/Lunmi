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
    ├── Conversar → Brain → resposta sem ferramentas
    │
    └── Executar → caminho determinístico ou UFO²
                       │ HostAgent / AppAgent
                       │ UIA / Win32 / filesystem
                       └ resultado + verificação
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

## Memória e dados

`app/memory` fornece SQLite, busca FTS5, propostas, episódios e histórico opt-in. Perfil, contexto de trabalho e episódios têm papéis diferentes. O contexto é limitado; conteúdo recuperado pode acompanhar chamadas ao provedor de IA.

O frontend usa essa API, sem abrir SQLite. Importação e operações destrutivas mantêm confirmação e validação no backend.

Código e lockfiles são públicos. Configurações de máquina, credenciais, bancos, exportações e evidências de execução são locais e ignorados pelo Git. O checkout não é um instalador empacotado; prepare dependências conforme o README.
