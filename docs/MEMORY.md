# Memória local

## O que é guardado

A memória usa SQLite e busca textual FTS5, sem embeddings ou banco vetorial. As categorias têm papéis diferentes:

- **Perfil V2:** um documento de texto livre com preferências, factos, hábitos e decisões. A caixa mostra o documento canónico, não uma segunda cópia.
- **Trabalho:** contexto curto da tarefa em andamento.
- **Episódios:** resumo de tarefas, conforme a opção habilitada.
- **Histórico legado da memória:** excertos de até 400 caracteres, apenas quando a opção correspondente estiver ativada. Começa desligado.

O banco pertence à instalação local, não ao provedor de IA. Trocar de Brain não troca o banco. Não há sincronização automática com nuvem ou outro sistema de notas.

## Guardar e revisar

Um pedido explícito como `Lembra que prefiro respostas curtas.` pode registrar uma preferência sem uma inferência adicional só para esse registro. O registro continua sujeito à política de conteúdo.

No painel **Memória**, edite **O que a Lunmi deve saber sobre ti** e escolha **Guardar**. Enter insere texto; não existe sintaxe de itens ou IDs. **Desfazer** repõe a última alteração elegível, num só nível. O teto é 8.000 caracteres Unicode. Conteúdo legado maior abre integralmente e pode encolher, sem truncamento silencioso.

Se a Lunmi atualizar a memória durante uma edição, o rascunho não é substituído. A revisão do documento impede guardar por cima de uma versão mais recente sem uma escolha explícita na interface. A gestão detalhada recolhida mantém busca, episódios, propostas legadas, importação, exportação e limpeza. Propostas legadas continuam pendentes até aceite.

Busca e filtros permitem consultar o que existe. A lista tem limite; ela não representa uma promessa de recuperação integral de conversas antigas.

## Opções

- **Usar memória:** controla a utilização do perfil; desligada, a caixa fica só de leitura.
- **Aprender com as conversas:** permite aprender declarações claras do utilizador em Conversar, na resposta já pedida ao Brain, sem uma inferência adicional. Desligada, não cria propostas automáticas novas.
- **Episódios:** controla a captura episódica.
- **Propostas:** opção legada na gestão detalhada; não substitui o controlo de aprendizagem V2.
- **Excertos legados da memória (400 caracteres):** autoriza o registo minimizado anterior; é opt-in e mantém a preferência existente.

Memória desligada não significa que o banco foi apagado. Para remover registros, use exclusão ou limpeza explícita. Apagar um item não deve apagar conversas independentes.

Pedidos explícitos para lembrar algo continuam disponíveis no chat, sujeitos à política de conteúdo. Aprendizagem automática não deve substituir texto manual. Anexos e conteúdo de email não são fontes de aprendizagem; esses turnos também não alimentam o histórico automático. Uma janela limitada de turnos em RAM permite continuidade da sessão sem ativar histórico persistente.

## Histórico de conversas

As novas conversas são guardadas automaticamente neste computador, separadas da memória
pessoal, em `<diretório de dados da Lunmi>/.local/chat-history.sqlite3`. A sandbox mantém
o arquivo dentro da sua própria pasta. Não se convertem excertos antigos nem se reconstroem
mensagens a partir de logs.

Na barra lateral, selecionar **Conversa** abre o histórico como sublista com ramos animados.
A sublista reúne **Nova conversa**, conversas temporárias e as conversas guardadas. Permite
abrir, renomear e apagar; não tem campo de pesquisa. Uma única área de rolagem contém a lista,
com título e data, por última atividade, carregada em páginas de 50.
O título inicial usa a primeira mensagem, até 80 caracteres, sem chamar o Brain. Em janelas
estreitas, selecionar **Conversa** abre uma gaveta com suporte de teclado e Escape.

Em **Configurações → Histórico de conversas**, **Guardar conversas neste computador**
afeta as próximas mensagens e não apaga o arquivo existente. O arranque pode **Retomar a
última conversa** (padrão) ou **Começar uma nova**. **Conversa temporária** não grava mensagens
nesse arquivo e nunca é retomada no arranque. Os rascunhos sobrevivem à troca de conversa
durante a execução da aplicação e ficam apenas em RAM.

O mini chat acompanha a conversa e o rascunho selecionados na principal. A gestão fica na
janela principal. Uma tarefa ou confirmação pendente impede mudar, criar ou apagar
conversas até concluir ou parar a tarefa.

Apagar uma conversa e **Limpar histórico** exigem confirmações próprias. Removem mensagens,
fontes e índices deste arquivo; os factos e opções da memória pessoal mantêm-se. Arquivar
não cria aprendizagem adicional. Guardam-se texto público, IDs, datas, Brain e fontes
com os metadados originais. Anexos guardam só nomes e tamanho; respostas derivadas de
anexos recebem um marcador de proteção. Segredos e conteúdo privado identificáveis são
substituídos por um marcador, segundo a política existente.

Ao continuar, restaura-se apenas o contexto elegível dessa conversa: até seis pares e
12 mil caracteres. Usa-se o Brain atualmente selecionado. Reabrir não repete pesquisas,
ações, confirmações ou fala. Fontes antigas conservam as datas originais. Após uma
interrupção, envios incompletos ficam marcados como interrompidos. Uma falha de gravação
mantém o chat em RAM e mostra um aviso nas duas janelas.

A extensão IPC aditiva anuncia `chat_history` e publica `chat.history.get`, `.list`,
`.read`, `.create`, `.select`, `.rename`, `.delete`, `.clear`, `.options.get` e `.options.set`,
além de `chat.history.changed`. O backend controla a conversa ativa; `conversation_id`,
`session_id` e `task_id` identificam conversa, ligação e execução. Os contratos estão no
[protocolo Python](../app/bridge/protocol.py), no
[protocolo TypeScript](../desktop/src/shared/protocol.ts) e nos
[DTOs de histórico](../desktop/src/shared/chatHistory.ts).

Cloud, exportação do chat, pastas e favoritos ficam fora desta versão.

## Contexto e estado atual

A recuperação seleciona poucos itens sob orçamento de caracteres. Uma tarefa de continuidade pode usar um episódio para localizar o contexto, mas precisa observar o estado atual: memória não comprova que um arquivo, janela ou conteúdo permaneça igual.

Trechos recuperados podem ser enviados ao Brain escolhido. **Armazenamento local não significa inferência privada.** Para reduzir envio a serviços externos, avalie um endpoint local e as configurações do seu provedor; não presuma isso só por ver a palavra local na interface.

## Exportar, importar e apagar

Exportação produz JSON versionado e pode conter dados pessoais. Importação valida tamanho, estrutura e política; não executa SQL do arquivo. O seletor é nativo do Windows.

Apagar, importar e limpar têm confirmação explícita. Cancelar um diálogo não é uma falha de tarefa. Exclusão não garante apagamento forense de SSD, backups ou exportações guardadas fora do app.

Esquecer, limpar e importar invalidam o estado de Desfazer aplicável. A migração de uma base V1 cria uma cópia de segurança validada antes de alterar o esquema; limpar a memória não elimina essa cópia nem ficheiros externos. A importação valida o documento e reconstrói o índice derivado, sem confiar num índice fornecido pelo ficheiro.

Não publique o banco em `.local/` nem exportações. Leia [SECURITY.md](../SECURITY.md).
