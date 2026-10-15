# Contribuir com a Lunmi

Siga o [README](README.md#começar), com Python 3.11 e Node.js 24. Instale ferramentas de desenvolvimento com `.venv/Scripts/python.exe -m pip install -r requirements-dev.txt`.

## Alterações

- Procure componentes, contratos e consumidores existentes antes de criar outro caminho.
- Mantenha apresentação, Brain, bridge, memória e ferramentas Windows em suas camadas.
- Não crie outro planner, shell genérico no renderer ou nova persistência de credenciais para resolver uma mudança visual.
- Verifique falha, carregamento, cancelamento e confirmação, além de sucesso.
- Para UI, confira os dois temas, teclado, movimento reduzido, janela pequena e rascunhos durante tarefas.
- Preserve lockfiles e revise as licenças de código incorporado.

## Verificações

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m ruff check app tests

cd desktop
npm ci
npm run typecheck
npm test
npm run build
```

Use diretórios temporários, credenciais fictícias e serviços locais controlados. Nunca use banco pessoal como fixture. Um mock não certifica tarefa executada em aplicativo real. Testes com inferência ou controle do desktop exigem ambiente e efeitos definidos; não entram automaticamente na CI.

## Publicação limpa

O `.gitignore` seleciona a superfície pública. Relatórios internos, pesquisas, agentes, `.local/`, `logs/`, `output/`, `.env`, dependências e vendor permanecem locais.

Antes de commitar:

```powershell
git status --short
git diff --cached --stat
git diff --cached --check
```

Revise o conteúdo dos arquivos novos. Ignore não remove arquivos já rastreados. Não publique tokens, chaves, cookies, caminhos pessoais, exportações ou logs brutos.

Descreva problema, alteração observável, validação e limites nas issues/PRs. Vulnerabilidades com detalhes sensíveis usam um canal privado; consulte [SECURITY.md](SECURITY.md).
