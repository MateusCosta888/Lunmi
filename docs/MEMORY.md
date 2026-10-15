# Memória local

## O que é guardado

A memória usa SQLite e busca textual FTS5, sem embeddings ou banco vetorial. As categorias têm papéis diferentes:

- **Perfil:** preferências, fatos, hábitos e decisões explícitos ou aceitos.
- **Trabalho:** contexto curto da tarefa em andamento.
- **Episódios:** resumo de tarefas, conforme a opção habilitada.
- **Histórico:** conversas, apenas quando a opção correspondente estiver ativada. Começa desligado.

O banco pertence à instalação local, não ao provedor de IA. Trocar de Brain não troca o banco. Não há sincronização automática com nuvem ou outro sistema de notas.

## Guardar e revisar

Um pedido explícito como `Lembra que prefiro respostas curtas.` pode registrar uma preferência sem uma inferência adicional só para esse registro. O registro continua sujeito à política de conteúdo.

No painel **Memória**, uma proposta nova permanece pendente até ser aceita. Rejeitar não a transforma em perfil. Atualizar altera um registro existente; não use edição para simular a aceitação de uma proposta.

Busca e filtros permitem consultar o que existe. A lista tem limite; ela não representa uma promessa de recuperação integral de conversas antigas.

## Opções

- **Ativada:** controla o uso da memória.
- **Episódios:** controla a captura episódica.
- **Propostas:** controla propostas de memória.
- **Histórico:** autoriza guardar conversa; é opt-in.

Memória desligada não significa que o banco foi apagado. Para remover registros, use exclusão ou limpeza explícita. Apagar um item não deve apagar conversas independentes.

## Contexto e estado atual

A recuperação seleciona poucos itens sob orçamento de caracteres. Uma tarefa de continuidade pode usar um episódio para localizar o contexto, mas precisa observar o estado atual: memória não comprova que um arquivo, janela ou conteúdo permaneça igual.

Trechos recuperados podem ser enviados ao Brain escolhido. **Armazenamento local não significa inferência privada.** Para reduzir envio a serviços externos, avalie um endpoint local e as configurações do seu provedor; não presuma isso só por ver a palavra local na interface.

## Exportar, importar e apagar

Exportação produz JSON versionado e pode conter dados pessoais. Importação valida tamanho, estrutura e política; não executa SQL do arquivo. O seletor é nativo do Windows.

Apagar, importar e limpar têm confirmação explícita. Cancelar um diálogo não é uma falha de tarefa. Exclusão não garante apagamento forense de SSD, backups ou exportações guardadas fora do app.

Não publique o banco em `.local/` nem exportações. Leia [SECURITY.md](../SECURITY.md).
