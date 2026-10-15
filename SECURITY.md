# Segurança e privacidade

## Relatar um problema

Não publique chaves, bancos, logs brutos ou explorações com dados reais em issues. Use o [relato privado de vulnerabilidade](https://github.com/MateusCosta888/Lunmi/security/advisories/new) se habilitado. Caso contrário, abra uma issue apenas pedindo um canal privado, sem detalhes sensíveis. Não há prazo de resposta, recompensa ou versão estável com suporte de longo prazo anunciado.

## Fronteiras importantes

- **Desktop:** Executar pode interagir com aplicativos e arquivos. Use sessão controlada e acompanhe confirmações/resultados.
- **Inferência:** instruções, observações e contexto de memória podem ser enviados ao provedor. Armazenamento local não equivale a inferência privada.
- **Memória:** SQLite fica em `.local/`; episódios/histórico seguem opções e propostas exigem aceite. Exportações podem conter dados pessoais.
- **Credenciais:** chaves lembradas usam DPAPI. `.env`, se usado, é texto local; proteja-o e não o publique.
- **Electron:** isolamento de contexto, sandbox, CSP local e preload limitado fazem parte da configuração. Sem shell/filesystem genéricos no renderer.
- **Recuperação:** reiniciar backend não reenvia automaticamente a tarefa anterior.

Exclusão pelo aplicativo não garante apagamento forense de SSD, backups ou exportações externas.

## Publicação

Código e documentação são selecionados explicitamente. Bancos, dados da máquina, credenciais, agentes, logs, relatórios, resultados de teste, builds e dependências baixadas ficam fora. Strings de chave nos testes são sentinelas fictícias para testar bloqueio e não vazamento.

Se uma credencial real for publicada, revogue/rotacione primeiro. Excluir em um commit posterior não a remove do histórico.

## Dependências upstream

Auditoria de 2026-10-15: `npm audit` não apontou advisories. `pip-audit -r requirements.lock --no-deps --disable-pip` apontou seis avisos; o ambiente Python **não é anunciado como livre de vulnerabilidades**.

| Pacote | Aviso | Alcance analisado nesta versão |
| --- | --- | --- |
| Flask 3.1.2 | [GHSA-68rp-wp8r-4726](https://github.com/advisories/GHSA-68rp-wp8r-4726) | Middleware upstream importa `flask.json`; a Lunmi não inicia servidor WSGI Flask nem usa suas sessões HTTP. |
| langchain 0.3.27 | [GHSA-gr75-jv2w-4656](https://github.com/advisories/GHSA-gr75-jv2w-4656) | Loaders/configurações e middleware de busca afetados não são expostos pelo bridge. |
| langchain 0.3.27 | [GHSA-3644-q5cj-c5c7](https://github.com/advisories/GHSA-3644-q5cj-c5c7) | Não há rota que puxe prompts de terceiros do LangSmith Hub. |
| langchain-core 0.3.86 | [GHSA-qh6h-p6c9-ff54](https://github.com/advisories/GHSA-qh6h-p6c9-ff54) | Não são aceitas configurações de prompt para os loaders LangChain afetados. |
| langchain-core 0.3.86 | [GHSA-2g6r-c272-w58r](https://github.com/advisories/GHSA-2g6r-c272-w58r) | A contagem de tokens com URLs de imagem de ChatOpenAI não é chamada. |
| langchain-text-splitters 0.3.11 | [GHSA-fv5p-p927-qmxr](https://github.com/advisories/GHSA-fv5p-p927-qmxr) | `split_text_from_url` não é chamado. Worker desativa RAG online/offline, experiência e demonstração. |

Triagem estática do caminho atual, não prova de ausência de toda exploração. Versões continuam fixadas por compatibilidade com a revisão UFO²; sem upgrade forçado de major. Reavaliar antes de habilitar novas rotas upstream e até **2026-11-15**. Esta triagem não autoriza habilitar Hub, RAG ou loaders adicionais.
