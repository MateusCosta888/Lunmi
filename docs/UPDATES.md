# Atualizações da Lunmi

Disponível no pacote instalado Windows x64 com o atualizador. No checkout está desligado. O Alpha 3 precisa de instalar o Alpha 4 manualmente uma vez.

A Lunmi procura atualizações 30 segundos após abrir e a cada seis horas. Baixa em segundo plano e apresenta **Reiniciar e atualizar** nas configurações. É possível procurar manualmente. Fechar a janela ou sair normalmente não instala nada. Tarefas, confirmações, comandos pendentes, ligações de conta e voz bloqueiam o reinício. Guarda rascunhos e alterações nos formulários antes de confirmar.

## Distribuição e confiança

Cada publicação produz um setup completo. O NSIS tenta baixar apenas blocos alterados com os blockmaps; sem cache ou download parcial disponível, baixa o setup completo. O tamanho depende das alterações, especialmente no Python. A instalação substitui o pacote completo e pode demorar vários minutos com a Lunmi fechada. Não copia ficheiros diretamente de `main`.

A origem de produção é `MateusCosta888/Lunmi`, em GitHub Releases, sem token no aplicativo. Alpha pode avançar para beta e estável; estável não recebe pré-lançamentos. Não há downgrade ou rollback automático.

`lunmi-update.json` contém versão, Windows/x64, nome, tamanho e SHA-512 do setup. `lunmi-update.sig` é uma assinatura Ed25519 binária de 64 bytes sobre o descritor exato. O cliente verifica a assinatura antes de baixar, confere os metadados NSIS e verifica o ficheiro novamente antes de instalar. Falta de assinatura ou adulteração impede a instalação.

Só a chave pública em `desktop/electron/update-public-key.json` é distribuída. A chave privada fica fora do checkout e de pastas sincronizadas, com acesso restrito e cópia de segurança protegida. Não a substituas: as instalações existentes confiam na chave que receberam. Perder a chave pode exigir novo setup manual; rotação não faz parte desta versão.

Ed25519 não é um certificado Authenticode e não elimina avisos de editor desconhecido no Windows. A verificação Authenticode do empacotador fica desativada; a assinatura Ed25519 e o hash são obrigatórios no único caminho de instalação da aplicação.

## Publicar

1. Configura o environment GitHub **release**, limitado a `main`. O acionamento do workflow é manual e o resultado fica em rascunho. Guarda nele **LUNMI_UPDATE_PRIVATE_KEY**, com o PEM privado codificado em base64. Nunca guardes esse valor no projeto, artefactos, relatórios ou logs.
2. Faz uma cópia de segurança protegida da chave e confirma que corresponde à chave pública. O assinador recusa outra identidade.
3. Conclui o ensaio de instalação e reinício isolados. Não publiques versões sintéticas de QA.
4. Atualiza `desktop/package.json` e lock para a mesma versão e envia o código revisto a `main`.
5. Executa manualmente **Prepare Windows release**, indicando a versão sem `v`.
6. O workflow testa, prepara Python e UFO pinados, audita, assina e cria uma Release em rascunho. Não executa em cada push nem publica automaticamente.
7. Confirma versão, commit, testes e cinco artefactos: setup `.exe`, `.exe.blockmap`, `latest.yml`, `lunmi-update.json` e `lunmi-update.sig`. O provider GitHub usa a marca de pré-lançamento da Release; o empacotador gera `latest.yml` também para alpha. Só então publica o rascunho. Não alteres os artefactos de uma versão publicada.

Conserva setups e blockmaps antigos. Corrige problemas com uma versão superior. A revisão das seis exceções Python em `SECURITY.md` expira em 2026-11-15; novas publicações ficam bloqueadas após essa data até revisão explícita.

## Build local e testes

Num checkout de release novo: instala as dependências de desenvolvimento, executa `install_ufo.py`, `npm ci` em `desktop`, `desktop/scripts/fetch_runtime.py`, `desktop/scripts/fetch_voice.py` e `desktop/scripts/assemble_resources.py`. Executa os testes, `npm run dist -- --publish never`, `verify_installer_defaults.py`, `verify_package.py` e `audit_payload.py` antes de assinar. O setup completo desde o Alpha 5 inclui os recursos de voz CPU; os modelos são obtidos de revisões públicas fixas e conferidos por SHA-256. Nunca copies o ambiente de desenvolvimento ou dados pessoais para o pacote.

Define **LUNMI_UPDATE_PRIVATE_KEY_FILE** com o caminho externo do PEM e executa `node desktop/scripts/update-release.mjs`. Apenas descritor e assinatura são escritos em `desktop/release`. O PEM nunca deve entrar nessa pasta.

`npm run test:release` testa o assinador com chaves efémeras. O ensaio `Lunmi.exe desktop/scripts/updater-acceptance.cjs <setup-existente> <pasta-nova>` usa o NSIS updater real e servidor local. Altera uma cópia sintética, mede downloads diferencial e completo, verifica a assinatura e **não executa o instalador**. Não substitui o ensaio de instalação e reinício reais.

## Dados e recuperação

Dados continuam fora da pasta instalada, em `%LOCALAPPDATA%/Lunmi/.local`. Atualizar não apaga histórico, memória, configurações, credenciais ou modelos. O Python e o mini chat encerram antes do instalador; reabrir não repete tarefas.

Se a rede ou o download falharem, continua na versão atual e tenta novamente. Se a instalação falhar, reinstala com um setup publicado e verificado, preservando os dados. Interrupção de energia durante a instalação não tem rollback garantido. Faz backup dos dados antes de ensaios de recuperação.
