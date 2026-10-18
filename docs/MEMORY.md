# Memória local

## O que é guardado

A memória usa SQLite e busca textual FTS5, sem embeddings ou banco vetorial. As categorias têm papéis diferentes:

- **Perfil V2:** um documento de texto livre com preferências, factos, hábitos e decisões. A caixa mostra o documento canónico, não uma segunda cópia.
- **Trabalho:** contexto curto da tarefa em andamento.
- **Episódios:** resumo de tarefas, conforme a opção habilitada.
- **Histórico:** conversas, apenas quando a opção correspondente estiver ativada. Começa desligado.

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
- **Histórico:** autoriza guardar conversa; é opt-in.

Memória desligada não significa que o banco foi apagado. Para remover registros, use exclusão ou limpeza explícita. Apagar um item não deve apagar conversas independentes.

Pedidos explícitos para lembrar algo continuam disponíveis no chat, sujeitos à política de conteúdo. Aprendizagem automática não deve substituir texto manual. Anexos e conteúdo de email não são fontes de aprendizagem; esses turnos também não alimentam o histórico automático. Uma janela limitada de turnos em RAM permite continuidade da sessão sem ativar histórico persistente.

## Contexto e estado atual

A recuperação seleciona poucos itens sob orçamento de caracteres. Uma tarefa de continuidade pode usar um episódio para localizar o contexto, mas precisa observar o estado atual: memória não comprova que um arquivo, janela ou conteúdo permaneça igual.

Trechos recuperados podem ser enviados ao Brain escolhido. **Armazenamento local não significa inferência privada.** Para reduzir envio a serviços externos, avalie um endpoint local e as configurações do seu provedor; não presuma isso só por ver a palavra local na interface.

## Exportar, importar e apagar

Exportação produz JSON versionado e pode conter dados pessoais. Importação valida tamanho, estrutura e política; não executa SQL do arquivo. O seletor é nativo do Windows.

Apagar, importar e limpar têm confirmação explícita. Cancelar um diálogo não é uma falha de tarefa. Exclusão não garante apagamento forense de SSD, backups ou exportações guardadas fora do app.

Esquecer, limpar e importar invalidam o estado de Desfazer aplicável. A migração de uma base V1 cria uma cópia de segurança validada antes de alterar o esquema; limpar a memória não elimina essa cópia nem ficheiros externos. A importação valida o documento e reconstrói o índice derivado, sem confiar num índice fornecido pelo ficheiro.

Não publique o banco em `.local/` nem exportações. Leia [SECURITY.md](../SECURITY.md).
