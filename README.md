<div align="center">

<img src="docs/assets/lunmi-logo.svg" width="180" height="180" alt="Logo Lunmi" />

# Lunmi

**Converse, organize suas ideias e execute tarefas no Windows.**

Um assistente de desktop com interface Electron, backend Python e automação assistida pelo Microsoft UFO².

[![Windows](https://img.shields.io/badge/Windows-10%20%2F%2011-5161D8?style=flat-square)](#requisitos)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=flat-square&logo=python&logoColor=white)](#começar)
[![Electron](https://img.shields.io/badge/Electron-React%20%2B%20TypeScript-242938?style=flat-square&logo=electron&logoColor=white)](desktop/)
[![Checks](https://github.com/MateusCosta888/Lunmi/actions/workflows/checks.yml/badge.svg)](https://github.com/MateusCosta888/Lunmi/actions/workflows/checks.yml)

[**Baixar Lunmi Alpha 6 · Windows x64**](https://github.com/MateusCosta888/Lunmi/releases/download/v0.1.0-alpha.6/Lunmi-0.1.0-alpha.6-Setup.exe)

[Instalar](#instalar-o-alpha-6) · [Executar pelo código](#começar) · [Funcionalidades](#o-que-a-lunmi-faz) · [Arquitetura](docs/ARCHITECTURE.md) · [Privacidade](SECURITY.md) · [Contribuir](CONTRIBUTING.md)

</div>

<p align="center">
  <img src="docs/assets/lunmi-dark.png" width="1100" alt="Lunmi instalada no tema Celestial, com o orb e o ecrã inicial da conversa" />
</p>

> **Alpha 6 disponível para testes.** Inclui introdução com Groq ou Kiro, pesquisa web que abre páginas, voz e ditado offline por CPU e atualizações pelo GitHub Releases, com verificação Ed25519 e confirmação antes de reiniciar para instalar. O setup ainda não tem certificado de editor Windows. A automação depende do aplicativo e da verificação disponível. Consulte [a Release](https://github.com/MateusCosta888/Lunmi/releases/tag/v0.1.0-alpha.6) e [os limites de segurança](SECURITY.md).

## Instalar o Alpha 6

1. Baixe o [instalador para Windows 10/11 x64](https://github.com/MateusCosta888/Lunmi/releases/download/v0.1.0-alpha.6/Lunmi-0.1.0-alpha.6-Setup.exe), com cerca de 2,08 GB. Inclui os modelos de voz; a instalação pode demorar vários minutos.
2. Execute o setup e abra a Lunmi. O Python necessário acompanha o instalador; não precisa instalar Python, Node.js ou Git para usar esta versão.
3. Na primeira abertura, a introdução ajuda a ligar o Groq (gratuito) ou o Kiro CLI. Em **Brain**, pode escolher outra API, endpoint ou CLI suportado com o seu próprio acesso. Créditos e chaves de IA não estão incluídos. O setup inclui voz e ditado por CPU, sem exigir NVIDIA; a ativação continua opcional.

Quem usa o Alpha 3 precisa instalar o Alpha 6 manualmente uma vez. Quem tem o Alpha 4 ou o Alpha 5 recebe esta versão pelo atualizador; a primeira atualização para o Alpha 6 substitui todos os ficheiros e demora mais. Depois, a Lunmi procura novas versões e baixa em segundo plano, aproveitando blocos existentes quando possível. Em **Configurações → Atualizações**, pode procurar manualmente e escolher **Reiniciar e atualizar** quando estiver pronta. Tarefas e operações de voz ativas bloqueiam a instalação; histórico, memória e configurações continuam fora da pasta instalada.

As atualizações chegam quando publicamos uma **Release**. Um envio de código a `main` executa testes; a distribuição exige preparar e publicar uma versão. O modo `run.bat` continua sem auto-update. [Funcionamento, publicação e recuperação →](docs/UPDATES.md)

## O que a Lunmi faz

| Recurso | Comportamento |
| --- | --- |
| **Chat único** | Responde, pede esclarecimento ou utiliza ferramentas conforme o pedido, mantendo autorização e verificação no backend. |
| **Resposta progressiva** | Mostra texto incremental nos transportes que oferecem streaming real. Transportes sem essa capacidade entregam a resposta completa. |
| **Conversas guardadas** | Histórico local separado da memória pessoal; retomar, renomear, apagar e conversar temporariamente. |
| **Pesquisa web** | Pesquisa configurável com fontes e metadados; disponibilidade e profundidade dependem do motor e do Brain. |
| **Brain configurável** | Usa APIs, endpoints compatíveis e CLIs locais suportados. O catálogo depende do provedor conectado. |
| **Memória local V2** | Documento editável com Guardar/Desfazer, aprendizagem opcional e gestão detalhada de episódios, propostas legadas e importação/exportação. |
| **Gmail via Composio** | Liga a conta Composio do próprio utilizador para pesquisar, ler e preparar emails. Envio exige uma prévia com aprovação manual. Integração experimental. |
| **Perfil local** | Nome de tratamento e foto, editáveis na própria janela. A foto não vai ao Brain. |
| **Anexos de texto** | Até 4 arquivos UTF-8, 32 KiB por arquivo e 64 KiB no total; o turno com anexos não autoriza ações. |
| **Controle do usuário** | Confirmações, perguntas, cancelamento e indicação de falhas fazem parte da execução. |
| **Interface adaptável** | Temas Celestial, Claro suave e Red Velvet, laterais com vidro, navegação recolhível e compositor multilinha com seleção de modelo. |
| **Lunmi Node** | Ao fechar a principal, pode continuar na bandeja com um Node num canto superior. Abre um mini chat sincronizado e mostra atividade/atenção. Fica passivo durante tarefas para não intercetar cliques. |
| **Voz e ditado locais** | Recursos de voz e ditado por CPU incluídos no setup completo. Ativação opcional e microfone acionado pelo utilizador; sem escuta contínua. |
| **GalaxyOrb** | Presença visual persistente na conversa principal, preservada quando chegam mensagens. |
| **Atualizações pelo GitHub** | A partir do Alpha 4 instalado: download automático, verificação de assinatura e instalação após confirmação de reinício. |

A execução combina caminhos determinísticos para tarefas reconhecidas e agentes UFO² para as demais. Um despacho de ação não prova sucesso: confira o resultado e a evidência apresentados.

## Requisitos

Os requisitos abaixo são para executar pelo código-fonte. O instalador alpha inclui o runtime Python e as dependências; cada testador configura o seu próprio Brain.

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

## Um chat, autorização explícita

Na interface atual não é preciso escolher Conversar ou Executar. Uma pergunta como "Como apago uma pasta?" deve produzir uma explicação; um pedido direto de operação pode utilizar ferramentas. Pedidos ambíguos exigem esclarecimento. Os modos legados continuam no protocolo para compatibilidade.

- Comece com uma tarefa pequena e arquivos próprios de teste.
- Leia os pedidos de confirmação antes de aceitar.
- **Parar** solicita o cancelamento da tarefa atual.
- Reiniciar um backend falhado não repete automaticamente a instrução anterior.
- A Autoaprovação mantém o comportamento existente: a alteração se destina à próxima tarefa, sem trocar a política de uma tarefa em andamento.
- O envio de email exige aprovação manual da prévia, mesmo com Autoaprovação ligada. Um resultado incerto não é repetido automaticamente; aceitação pelo Gmail não comprova entrega ao destinatário.

O painel de tarefas recentes mostra a sessão atual. O histórico sob **Conversa** guarda as
novas conversas neste computador, permite continuá-las e oferece conversas temporárias.
Em Configurações, pode desativar a gravação e escolher entre retomar a última conversa ou
começar uma nova. Apagar histórico não apaga factos da memória pessoal.

Em **Configurações → Segundo plano**, escolha se fechar a janela mantém a Lunmi em execução. O Node tem opções de canto, visibilidade, recolhimento, atividade e atalho. **Sair** na bandeja termina a aplicação. O mini chat partilha a conversa atual; confirmações sensíveis são abertas na principal. Durante uma tarefa em execução, o Node não aceita interação nem rouba o foco; use a bandeja para restaurar a principal se precisar intervir.

A saída de voz e o ditado são opcionais e experimentais. O checkout não inclui os modelos
nem instala um runtime de áudio automaticamente. Sem esses recursos, a interface indica
indisponibilidade e o chat continua a funcionar. O microfone é acionado pelo utilizador;
não há escuta contínua. Veja [os limites da voz e do modo residente](docs/GETTING_STARTED.md#node-e-segundo-plano).

## Memória e privacidade

As memórias ficam em SQLite local. O painel apresenta um documento de texto livre: **O que a Lunmi deve saber sobre ti**. É possível editar e guardar, pedir para lembrar algo no chat e permitir aprendizagem de declarações claras em conversas. As opções **Usar memória** e **Aprender com as conversas** controlam esse comportamento. Propostas legadas continuam sujeitas a aceite; episódios e o histórico legado da memória têm opções próprias. O histórico do chat é separado e começa ativado para novas conversas.

**Memória local não significa inferência privada.** Instruções, contexto recuperado e observações necessárias à execução podem ser enviados ao provedor escolhido. Um endpoint local e uma API externa têm fronteiras de privacidade diferentes.

Chaves lembradas pela interface usam DPAPI do Windows. O repositório não contém banco pessoal, credenciais, logs de execução ou exportações de memória.

[Memória: uso e limites →](docs/MEMORY.md) · [Segurança e dados locais →](SECURITY.md)

## A interface atual

### Brain no Red Velvet

Provedores aparecem em botões com as suas marcas, separados entre APIs, endpoints e CLIs. O campo de chave aparece apenas quando o provedor selecionado precisa dele. Disponibilidade e autenticação dependem da instalação de cada utilizador.

<p align="center">
  <img src="docs/assets/lunmi-brain.png" width="1100" alt="Seletor de provedores do Brain no tema Red Velvet, com botões por API, endpoint e CLI e configuração de chave" />
</p>

### Memória no Claro suave

O documento de memória pode ser editado e guardado no próprio painel. Troque o tema em **Configurações → Aparência**, sem reiniciar ou apagar o rascunho; a escolha é lembrada na próxima abertura.

<p align="center">
  <img src="docs/assets/lunmi-light.png" width="1100" alt="Editor de memória da Lunmi Alpha 4 no tema Claro suave, com perfil fictício de demonstração" />
</p>

As capturas mostram a interface real do Alpha 4, com conversas e perfil fictícios preparados para demonstração, sem dados pessoais ou credenciais. O texto do chat não é prova de inferência ou de ações executadas. Modelos, CLIs e integrações não são fornecidos nem autenticados por estas imagens.

## Estrutura

```text
app/
  brain/        Provedores, descoberta e transportes de inferência
  bridge/       Protocolo JSONL entre Electron e Python
  computer/     Ferramentas Windows e integração UFO²
  core/         Controle da tarefa e política de execução
  integrations/ Composio OAuth e ferramentas restritas de Gmail
  memory/       Persistência e recuperação de memória
  ui/           Frontend Tkinter legado
desktop/
  electron/     Processo principal, preload e supervisão do backend
  src/          Interface React, temas e componentes
  scripts/      Montagem, auditoria e aceite dos pacotes
  tests/        Testes do frontend e do protocolo
  resources/    Metadados do runtime; binários locais fora do Git
  release/      Instaladores gerados localmente, fora do Git
docs/           Guias públicos e recursos de apresentação
tests/          Regressões do backend
benchmarks/     Ferramentas locais de medição
```

[Índice dos guias →](docs/README.md)

Na cópia completa de trabalho, `docs/LOCAL-INDEX.md` organiza os relatórios por assunto, planos, investigação e evidências. `docs/reports/`, `docs/plans/`, `logs/`, `output/`, `research/` e `Relatorio Final/` conservam o histórico local; esses materiais não são publicados em bloco no repositório. `docs/MAPA-DE-CAMINHOS.tsv` regista a localização anterior e atual dos ficheiros reorganizados.

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
- Anexos aceitam texto/código UTF-8. Imagens, PDF e binários são recusados; anexos e respostas desse turno não viram memória automática.
- Streaming depende da capacidade do transporte. Voz e ditado requerem runtimes externos preparados; pesquisa web não equivale a controlo completo de um navegador.
- Gmail é experimental: a ligação e a leitura reais foram exercitadas numa conta de testes; o diálogo e as confirmações de envio foram testados com simulações. Envio/entrega reais, todos os formatos MIME e todas as combinações de modelos não estão certificados. Cada utilizador usa a sua conta Composio e Gmail; os limites desses serviços continuam a aplicar-se.
- A distribuição está em alpha Windows x64, com atualizador desde o Alpha 4 e sem certificado Authenticode. As atualizações exigem assinatura Ed25519 válida. Outra máquina/VM e todas as combinações de provedores ainda precisam de validação; não há suporte a macOS/Linux ou certificação geral de múltiplos monitores e DPIs.
- Há dependências upstream legadas; avisos conhecidos e triagem estão em [Segurança](SECURITY.md#dependências-upstream).

## Créditos e licenças

A Lunmi integra [Microsoft UFO²](https://github.com/microsoft/UFO), Electron, React e outros projetos. Avisos de fontes, ícones e referências visuais: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Uma licença geral para o código próprio da Lunmi ainda não foi definida. Não presuma que permissões de uma dependência se estendam ao projeto inteiro; os avisos e restrições de terceiros continuam aplicáveis.
