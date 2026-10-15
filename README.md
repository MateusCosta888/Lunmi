<div align="center">

<img src="docs/assets/lunmi-logo.svg" width="180" height="180" alt="Logo Lunmi" />

# Lunmi

**Converse, organize suas ideias e execute tarefas no Windows.**

Um assistente de desktop com interface Electron, backend Python e automação assistida pelo Microsoft UFO².

[![Windows](https://img.shields.io/badge/Windows-10%20%2F%2011-5161D8?style=flat-square)](#requisitos)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=flat-square&logo=python&logoColor=white)](#começar)
[![Electron](https://img.shields.io/badge/Electron-React%20%2B%20TypeScript-242938?style=flat-square&logo=electron&logoColor=white)](desktop/)
[![Checks](https://github.com/MateusCosta888/Lunmi/actions/workflows/checks.yml/badge.svg)](https://github.com/MateusCosta888/Lunmi/actions/workflows/checks.yml)

[Começar](#começar) · [Funcionalidades](#o-que-a-lunmi-faz) · [Arquitetura](docs/ARCHITECTURE.md) · [Privacidade](SECURITY.md) · [Contribuir](CONTRIBUTING.md)

</div>

<p align="center">
  <img src="docs/assets/lunmi-dark.png" width="1100" alt="Lunmi no tema Celestial, com navegação recolhida e painel de memória" />
</p>

> **Em desenvolvimento.** A Lunmi funciona a partir do código-fonte em uma sessão interativa do Windows. Ainda não há instalador assinado, atualização automática ou garantia de automação universal.

## O que a Lunmi faz

| Recurso | Comportamento |
| --- | --- |
| **Conversar** | Responde com o Brain escolhido, sem executar ferramentas de desktop. |
| **Executar** | Encaminha tarefas ao fluxo de automação e apresenta o resultado com seu estado de verificação. |
| **Brain configurável** | Usa APIs, endpoints compatíveis e CLIs locais suportados. O catálogo depende do provedor conectado. |
| **Memória local** | Permite consultar, editar, propor, aceitar, excluir, importar e exportar registros. |
| **Perfil local** | Nome de tratamento e foto, editáveis na própria janela. A foto não vai ao Brain. |
| **Anexos de texto** | No modo Conversar: até 4 arquivos UTF-8, 32 KiB por arquivo e 64 KiB no total. |
| **Controle do usuário** | Confirmações, perguntas, cancelamento e indicação de falhas fazem parte da execução. |
| **Interface adaptável** | Temas Celestial e Claro suave, lateral recolhível e compositor multilinha com seleção de modelo. |

A execução combina caminhos determinísticos para tarefas reconhecidas e agentes UFO² para as demais. Um despacho de ação não prova sucesso: confira o resultado e a evidência apresentados.

## Requisitos

- **Windows 10 ou 11**, com a sessão de desktop aberta e desbloqueada.
- **Python 3.11**, com Tkinter para o frontend legado opcional.
- **Node.js 24 LTS**, npm e Git disponíveis no terminal.
- Internet na instalação para baixar dependências e a revisão fixada do UFO².
- Um provedor de IA para os fluxos com inferência. Endpoints locais e CLIs precisam estar instalados/configurados separadamente.

Custos, limites e disponibilidade pertencem ao provedor escolhido. A Lunmi não inclui créditos nem chaves de API.

## Começar

No PowerShell:

```powershell
git clone https://github.com/MateusCosta888/Lunmi.git
cd Lunmi

py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe install_ufo.py

cd desktop
npm ci
npm run build
cd ..

.\run.bat
```

No Windows, o build prepara um runtime local `desktop/.runtime/Lunmi.exe`, com nome e ícone da Lunmi. Ele é iniciado por `run.bat` e não modifica o Electron instalado. Esse runtime depende deste checkout; não é um instalador ou executável portátil independente.

`install_ufo.py` baixa e confere a revisão registrada em [`ufo.lock.json`](ufo.lock.json). `vendor/` é local e não faz parte deste repositório. O launcher também verifica esse checkout antes de abrir o app.

Em **Brain**, escolha o provedor e configure credenciais ou endpoint. Quando disponível, o catálogo de modelos aparece no seletor da própria barra de conversa.

Para configuração por ambiente, copie [`.env.example`](.env.example) para `.env`. O arquivo real é local e ignorado pelo Git. Valores explícitos do ambiente têm prioridade sobre os da interface.

[Guia de instalação e solução de problemas →](docs/GETTING_STARTED.md)

## Dois modos, uma distinção importante

**Conversar** não executa ações no computador. **Executar** pode interagir com aplicativos e arquivos conforme a tarefa, as capacidades disponíveis e a política de confirmação.

- Comece com uma tarefa pequena e arquivos próprios de teste.
- Leia os pedidos de confirmação antes de aceitar.
- **Parar** solicita o cancelamento da tarefa atual.
- Reiniciar um backend falhado não repete automaticamente a instrução anterior.
- A Autoaprovação mantém o comportamento existente: a alteração se destina à próxima tarefa, sem trocar a política de uma tarefa em andamento.

O painel de tarefas recentes mostra a sessão atual; não promete recuperar integralmente conversas anteriores.

## Memória e privacidade

As memórias ficam em SQLite local. Propostas precisam ser aceitas; episódios e histórico seguem as opções habilitadas. O histórico de conversa começa desligado.

**Memória local não significa inferência privada.** Instruções, contexto recuperado e observações necessárias à execução podem ser enviados ao provedor escolhido. Um endpoint local e uma API externa têm fronteiras de privacidade diferentes.

Chaves lembradas pela interface usam DPAPI do Windows. O repositório não contém banco pessoal, credenciais, logs de execução ou exportações de memória.

[Memória: uso e limites →](docs/MEMORY.md) · [Segurança e dados locais →](SECURITY.md)

## Claro suave

Troque o tema em **Configurações → Aparência**, sem reiniciar ou apagar o rascunho. A escolha é lembrada na próxima abertura.

<p align="center">
  <img src="docs/assets/lunmi-light.png" width="1100" alt="Interface real da Lunmi no tema Claro suave" />
</p>

As imagens mostram o aplicativo real em ambiente de demonstração, sem dados pessoais. Não representam modelos ou integrações adicionais disponíveis por padrão.

## Estrutura

```text
app/
  brain/        Provedores, descoberta e transportes de inferência
  bridge/       Protocolo JSONL entre Electron e Python
  computer/     Ferramentas Windows e integração UFO²
  core/         Controle da tarefa e política de execução
  memory/       Persistência e recuperação de memória
  ui/           Frontend Tkinter legado
desktop/
  electron/     Processo principal, preload e supervisão do backend
  src/          Interface React, temas e componentes
  tests/        Testes do frontend e do protocolo
docs/           Guias públicos e imagens selecionadas
tests/          Regressões do backend
benchmarks/     Ferramentas locais de medição
```

[Como as partes se conectam →](docs/ARCHITECTURE.md)

## Desenvolvimento

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m ruff check app tests

cd desktop
npm run typecheck
npm test
npm run build
```

Os testes usam fixtures e sandboxes; não certificam qualquer aplicativo externo. Inferência real e controle do desktop exigem ambiente próprio e consentimento para seus efeitos. O frontend Tkinter continua disponível com `run.bat --tk` ou `run-tk.bat`.

## Limites atuais

- Automação depende do que os aplicativos expõem por UIA/Win32 e da verificação disponível.
- Anexos funcionam somente em Conversar com texto/código UTF-8. Imagens, PDF e binários são recusados; anexos e respostas desse turno não viram memória automática.
- Voz, streaming e navegação web completa não são anunciados como recursos prontos.
- Não há distribuição empacotada, suporte a macOS/Linux ou certificação geral de múltiplos monitores e DPIs.
- Há dependências upstream legadas; avisos conhecidos e triagem estão em [Segurança](SECURITY.md#dependências-upstream).

## Créditos e licenças

A Lunmi integra [Microsoft UFO²](https://github.com/microsoft/UFO), Electron, React e outros projetos. Avisos de fontes, ícones e referências visuais: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Uma licença geral para o código próprio da Lunmi ainda não foi definida. Não presuma que permissões de uma dependência se estendam ao projeto inteiro; os avisos e restrições de terceiros continuam aplicáveis.
