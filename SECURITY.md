# Segurança e privacidade

## Relatar um problema

Não publique chaves, bancos, logs brutos ou explorações com dados reais em issues. Use o [relato privado de vulnerabilidade](https://github.com/MateusCosta888/Lunmi/security/advisories/new) se habilitado. Caso contrário, abra uma issue apenas pedindo um canal privado, sem detalhes sensíveis. Não há prazo de resposta, recompensa ou versão estável com suporte de longo prazo anunciado.

## Fronteiras importantes

- **Desktop:** o chat automático pode interagir com aplicativos e arquivos quando o pedido autoriza uma operação. Use sessão controlada e acompanhe confirmações/resultados.
- **Inferência:** instruções, observações e contexto de memória podem ser enviados ao provedor. Armazenamento local não equivale a inferência privada.
- **Memória:** SQLite fica na raiz de dados local; o documento de perfil pode ser editado e atualizado por aprendizagem quando ativada. Propostas legadas exigem aceite; episódios/histórico seguem opções próprias. Exportações e cópias de segurança podem conter dados pessoais.
- **Gmail:** utiliza a conta Composio do próprio utilizador. Credenciais lembradas são cifradas com DPAPI; não são incluídas no repositório. Emails são dados externos não confiáveis. Enviar exige prévia e aprovação manual, mesmo com Autoaprovação. Resultado incerto não autoriza repetição, e aceitação pelo serviço não garante entrega.
- **Credenciais:** chaves lembradas usam DPAPI. `.env`, se usado, é texto local; proteja-o e não o publique.
- **Electron:** isolamento de contexto, sandbox, CSP local e preload limitado fazem parte da configuração. Sem shell/filesystem genéricos no renderer.
- **Recuperação:** reiniciar backend não reenvia automaticamente a tarefa anterior.

Exclusão pelo aplicativo não garante apagamento forense de SSD, backups ou exportações externas.

## Publicação

Código e documentação são selecionados explicitamente. Bancos, dados da máquina, credenciais, agentes, logs, relatórios, resultados de teste, builds e dependências baixadas ficam fora. Strings de chave nos testes são sentinelas fictícias para testar bloqueio e não vazamento.

Se uma credencial real for publicada, revogue/rotacione primeiro. Excluir em um commit posterior não a remove do histórico.

## Dependências upstream

Atualização de 2026-10-18: `npm audit` apontou **oito entradas moderadas**, nenhuma alta ou crítica, na árvore de ferramentas de build/empacotamento. A cadeia envolve `electron-builder`, `@electron/get`, `global-agent`, `roarr` e dependentes; não se trata de oito falhas independentes do renderer. A correção automática sugerida muda a versão fixada do empacotador e não foi aplicada neste envio de código. Reavaliar até **2026-11-15** ou antes de gerar nova distribuição. Não usar proxies de build ou dependências não confiáveis.

A auditoria Python (`pip-audit -r requirements.lock --no-deps --disable-pip`), repetida em 2026-10-18, confirmou seis avisos em quatro pacotes; o ambiente Python **não é anunciado como livre de vulnerabilidades**. A tabela abaixo preserva a triagem de alcance registada em 2026-10-15.

| Pacote | Aviso | Alcance analisado nesta versão |
| --- | --- | --- |
| Flask 3.1.2 | [GHSA-68rp-wp8r-4726](https://github.com/advisories/GHSA-68rp-wp8r-4726) | Middleware upstream importa `flask.json`; a Lunmi não inicia servidor WSGI Flask nem usa suas sessões HTTP. |
| langchain 0.3.27 | [GHSA-gr75-jv2w-4656](https://github.com/advisories/GHSA-gr75-jv2w-4656) | Loaders/configurações e middleware de busca afetados não são expostos pelo bridge. |
| langchain 0.3.27 | [GHSA-3644-q5cj-c5c7](https://github.com/advisories/GHSA-3644-q5cj-c5c7) | Não há rota que puxe prompts de terceiros do LangSmith Hub. |
| langchain-core 0.3.86 | [GHSA-qh6h-p6c9-ff54](https://github.com/advisories/GHSA-qh6h-p6c9-ff54) | Não são aceitas configurações de prompt para os loaders LangChain afetados. |
| langchain-core 0.3.86 | [GHSA-2g6r-c272-w58r](https://github.com/advisories/GHSA-2g6r-c272-w58r) | A contagem de tokens com URLs de imagem de ChatOpenAI não é chamada. |
| langchain-text-splitters 0.3.11 | [GHSA-fv5p-p927-qmxr](https://github.com/advisories/GHSA-fv5p-p927-qmxr) | `split_text_from_url` não é chamado. Worker desativa RAG online/offline, experiência e demonstração. |

Triagem estática do caminho atual, não prova de ausência de toda exploração. Versões continuam fixadas por compatibilidade com a revisão UFO²; sem upgrade forçado de major. Reavaliar antes de habilitar novas rotas upstream e até **2026-11-15**. Esta triagem não autoriza habilitar Hub, RAG ou loaders adicionais.

### Nova consulta dos avisos — 2026-10-27

Na atualização do código-fonte, `npm audit` reportou três entradas: duas altas (`electron`
e `undici`) e uma moderada (`fast-uri`), sem críticas. As versões destes pacotes não foram
alteradas neste envio; há correções indicadas pelo audit que exigem nova validação do runtime
e da cadeia de build. Não aplicar `audit fix --force` sem essa validação.

`pip-audit -r requirements.lock --no-deps --disable-pip` reportou 57 avisos em 11 pacotes:
authlib, certifi, dnspython, fastmcp, filelock, Flask, langchain, langchain-core,
langchain-text-splitters, opentelemetry-api e Pillow. O lock Python não mudou neste envio.
A triagem anterior não cobre automaticamente estes avisos novos. A análise de alcance e as
atualizações compatíveis ficam pendentes antes de uma nova distribuição binária.

Esta publicação atualiza código-fonte e testes, sem gerar ou anunciar um instalador seguro.
Não constitui certificação de ausência de vulnerabilidades nas dependências.

### Preparação do atualizador — 2026-10-27

Electron passa a 44.10.0; `undici` e `fast-uri` transitivos foram atualizados sem `--force`. A auditoria npm passa sem avisos na árvore resultante. Python e runtime fixam Pillow 13.0.0, certifi 2026.10.19, filelock 4.2.1 e Flask 3.1.3. A auditoria do lock de distribuição fica com seis avisos, revistos até **2026-11-15**:

- FastMCP `GHSA-7xgj-8392-5q4r`: bypass de políticas após transforms de servidor. O servidor local regista ferramentas diretamente, sem transforms de autorização e sem exposição de um servidor HTTP autenticado. Não habilitar essas superfícies com esta versão.
- LangChain `PYSEC-2026-2192` e `PYSEC-2026-2555`, core `PYSEC-2026-2193` e `PYSEC-2026-2562`, text-splitters `PYSEC-2026-77`: caminhos de loaders/configurações, Hub, contagem de tokens de imagens por URL e splitter por URL já delimitados acima. Essas rotas não são expostas pela Lunmi; não fazer upgrade de major à margem da compatibilidade UFO.

`desktop/scripts/audit_runtime.py` ignora apenas esses seis IDs revistos e falha para avisos adicionais ou revisão expirada. É uma análise estática e temporária de alcance, não ausência de vulnerabilidades. Fonte FastMCP: [aviso do mantenedor](https://github.com/PrefectHQ/fastmcp/security/advisories/GHSA-7xgj-8392-5q4r).

O lock de runtime usa pins PyPI para pip e setuptools, em vez de URLs locais da máquina que construiu o Python. Setuptools passa a 84.1.0 para corrigir `GHSA-xmj3-9gf7-h52w`, identificado quando deixou de ser ignorado pela auditoria como URL local.

O atualizador usa descritores assinados com Ed25519 e hash do instalador. Só aceita a origem fixa e uma versão superior; não instala ao sair normalmente. Chave privada e tokens GitHub nunca entram no cliente ou nos artefactos. Ver [Atualizações](docs/UPDATES.md).
