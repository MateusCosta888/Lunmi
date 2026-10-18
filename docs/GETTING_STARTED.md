# Instalação e primeiros passos

## Ambiente

Use Windows 10/11, Python 3.11, Node.js 24 LTS e Git. Siga os comandos do README a partir de um clone do projeto. Os exemplos usam o caminho explícito do Python da `.venv`; não é necessário ativá-la.

`requirements.lock` fixa o ambiente Python; `requirements.txt` contém requisitos diretos e `requirements-dev.txt` acrescenta ferramentas de desenvolvimento. O frontend usa `desktop/package-lock.json` e `npm ci`.

O Electron baixa seu binário durante a instalação npm. Não desabilite os scripts de instalação sem preparar separadamente os binários exigidos pelo Electron e pelo build.

## Abrir

Depois da instalação e do build, execute `run.bat`. O launcher verifica o checkout UFO fixado. A abertura celestial aguarda os dados de inicialização e sua sequência visual antes de mostrar a interface; erros oferecem recuperação explícita.

O frontend legado é opcional: `run.bat --tk` ou `run-tk.bat`.

## Configurar um Brain

1. Abra **Brain** na lateral.
2. Escolha um provedor suportado e preencha os campos solicitados.
3. Para um CLI, autentique-se antes na ferramenta correspondente. A Lunmi não recupera sua senha.
4. Abra o catálogo e escolha um modelo disponível. É possível pesquisar na lista; não é necessário escrever um ID de modelo. Verificar e testar um modelo são operações distintas; o teste de inferência exige consentimento e pode consumir cota.
5. Aplique. O catálogo aparece na barra de conversa quando o provedor oferece listagem.

Credenciais lembradas usam DPAPI. A chave salva não volta ao renderer como texto. Variáveis explícitas do ambiente têm prioridade; se um valor parece fixo, confira `.env` e o ambiente do processo. Aguarde ou pare a tarefa antes de trocar a configuração do Brain.

## Usar com previsibilidade

O chat atual decide entre responder e utilizar ferramentas. Comece com uma pergunta simples. Para pedir uma operação, descreva um objetivo pequeno e identifique o aplicativo/arquivo. Acompanhe confirmações e resultados. Comece com dados próprios de teste.

Atalhos do painel apenas preenchem o rascunho. Enter envia; Shift+Enter insere uma linha. A lateral recolhida mantém os ícones. Temas ficam em **Configurações → Aparência**; a escala segue Windows/Chromium, sem ajuste manual de zoom.

## Dados locais

No checkout, o aplicativo cria dados em `.local/`, `logs/` e `output/`. A instalação empacotada usa a raiz de dados do utilizador em `%LOCALAPPDATA%\Lunmi`; os recursos instalados não são o destino dos dados pessoais. Não compartilhe essas pastas. Exportações podem conter preferências e conversas autorizadas; trate-as como dados privados. Leia [Memória](MEMORY.md) e [Segurança](../SECURITY.md).

## Ligar Gmail

1. Abra **Apps** e escolha Gmail.
2. Inicie a ligação. No navegador, autentique a sua própria conta Composio e autorize a conta Gmail de testes que pretende utilizar.
3. Se houver várias contas, selecione explicitamente a pretendida. Aguarde a indicação de ligação concluída.
4. Comece com uma pesquisa ou leitura. Para enviar, indique destinatário, assunto e mensagem; confira a prévia da aplicação e escolha **Aprovar** ou **Negar**.

A ligação não dá autorização para enviar qualquer mensagem. A Autoaprovação não elimina a confirmação manual de email. Não repita um envio com resultado incerto sem consultar primeiro a pasta Enviados. Os limites e a disponibilidade dependem também do Composio e do Gmail. O estado da ligação e as credenciais são locais; não os anexe a issues.

## Solução de problemas

| Sintoma | Confira |
| --- | --- |
| Launcher pede o Python | Crie `.venv` na raiz e instale `requirements.lock`. |
| Electron/build ausente | Execute `npm ci` e `npm run build` dentro de `desktop/`. |
| Revisão/hash do UFO divergente | Preserve o checkout existente e prepare uma cópia limpa da revisão em `ufo.lock.json`. O instalador não sobrescreve vendor modificado. |
| Catálogo vazio | Verifique provedor, endpoint, credencial ou autenticação do CLI. Nem todo provedor oferece listagem. |
| Configuração da tela não prevalece | Confira as variáveis explícitas do ambiente e `.env`. |
| Sessão com falha | Leia o erro e use reiniciar backend; a instrução anterior não é repetida automaticamente. |
| Aplicativo não responde à automação | Confira sessão desbloqueada, identidade da janela e suporte de acessibilidade. Não trate uma falha como sucesso. |

Ao relatar um problema, envie descrição mínima e versões relevantes. Não anexe `.env`, `.local`, bancos ou logs brutos.
