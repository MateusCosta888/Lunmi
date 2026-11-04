# Sistema de instalação e atualização (1.0.0-beta)

Contrato entre o lançador Rust (`desktop/installer`), a app Electron (`desktop/electron`) e o CI
(`.github/workflows/release.yml`). Substitui o NSIS do electron-builder, `electron-updater`,
`desktop/build/installer.nsh` e `desktop/scripts/components.py`. Não há migração automática das
versões 0.1.0-alpha.*: quem as tem desinstala-as e instala a beta (os dados ficam).

## Porquê
As alphas falhavam por três causas estruturais: pasta escolhida pelo utilizador (Documentos,
proteção de pastas, indexador, caminhos longos), ficheiros substituídos no mesmo sítio enquanto
estavam em uso, e o desinstalador da versão antiga a correr dentro da atualização. Cada regra
abaixo elimina uma delas.

## Regras
1. Programa sempre em `%LOCALAPPDATA%\Programs\Lunmi` (`ROOT`). Dados sempre em
   `%LOCALAPPDATA%\Lunmi` (`DATA`, = `LUNMI_USER_DATA`), nunca tocados pelo instalador.
2. Pacotes imutáveis, endereçados pelo hash do conteúdo. Uma versão nova vai para pastas novas;
   nada que esteja em uso é apagado ou reescrito.
3. Ativar uma versão = substituir `ROOT\current.json` atomicamente (escrever `current.json.tmp`,
   `MoveFileExW(MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)`).
4. Só o `ROOT\Lunmi.exe` (Rust) instala, atualiza, ativa e desinstala. A app Electron pede-lhe.
5. Toda a falha escreve uma linha com causa em `DATA\logs\install.log` e devolve um código
   estável (tabela no fim).

## Layout
```
ROOT\
  Lunmi.exe                lançador (alvo dos atalhos e da entrada de desinstalação)
  current.json
  store\<nome>-<chave>\    pacote extraído; existe .complete com a chave quando acabou
  versions\<versão>\       árvore da app Electron, montada por hardlinks a partir de base+app
  staging\                 transferências <nome>-<chave>.tar.zst.part (retoma) e extrações .partial
DATA\logs\install.log
```

### Pacotes
| nome | conteúdo | origem no build |
|---|---|---|
| `base` | Electron (win-unpacked) sem `resources\app.asar`, `resources\app\`, `resources\lunmi-components.json`; `Lunmi.exe` do Electron renomeado para `Lunmi-app.exe` | `electron-builder --dir` |
| `app` | `resources\app.asar`, `resources\app\` (código Python), `resources\lunmi-components.json`, licenças | idem |
| `python` | `desktop/resources/python` | `fetch_runtime.py`, `fetch_web.py`, `assemble_resources.py` |
| `voice` | `desktop/resources/voice` sem `models\` | `fetch_voice.py` |

Chave = primeiros 16 hex (minúsculas) do SHA-256 da lista de todos os ficheiros regulares da
pasta (excluindo `.complete`), ordenada pelo caminho relativo POSIX em bytes UTF-8, cada linha
`<caminho>\0<tamanho em decimal>\0<sha256 hex minúsculas do ficheiro>\n`. Qualquer verificador
pode recomputá-la. Binário: `desktop/installer/target/release/Lunmi.exe` (crate `lunmi-setup`,
`[[bin]] name = "Lunmi"`). Arquivo `<nome>-<chave>.tar.zst`: tar POSIX com caminhos
ordenados, mtime 0, uid/gid 0, zstd nível 19. Extração recusa caminhos absolutos, `..`,
ligações e dispositivos.

`versions\<v>\` = ficheiros de `store\base-<k>` + `store\app-<k>` (os de `app` sobrepõem-se), por
hardlink; se o hardlink falhar, cópia. A app encontra Python e voz em
`ROOT\store\python-<k>` / `voice-<k>` usando as chaves de `resources\lunmi-components.json`
(`componentDir()` em `desktop/electron/backend.ts`: `path.resolve(resourcesRoot, "..", "..", "..", "store", name + "-" + key)`).

### current.json
```json
{"schema":1,"version":"1.0.0-beta.2","packs":{"base":"…","app":"…","python":"…","voice":"…"},
 "healthy":false,"attempts":0,
 "previous":{"version":"1.0.0-beta.1","packs":{…}}}
```

## Manifesto de release
URL fixo do canal: `https://github.com/MateusCosta888/Lunmi/releases/download/channel-beta/latest.json`
e `latest.json.sig` (64 bytes, Ed25519 sobre os bytes exatos de `latest.json`, chave pública em
`desktop/electron/update-public-key.json`; o Rust embute a mesma chave). Máximo 64 KiB.
```json
{"schema":1,"channel":"beta","version":"1.0.0-beta.2","published":"2026-11-04T12:00:00Z",
 "notes":"texto simples",
 "packs":[{"name":"app","key":"0123456789abcdef","size":7340032,"unpacked":9000000,
           "sha256":"<64 hex do .tar.zst>",
           "url":"https://github.com/MateusCosta888/Lunmi/releases/download/packs/app-0123456789abcdef.tar.zst"}],
 "setup":{"url":"…/v1.0.0-beta.2/Lunmi-1.0.0-beta.2-Setup.exe","size":0,"sha256":"…"}}
```
Regras de validação: `version` semver `X.Y.Z-beta.N` ou `X.Y.Z`; exatamente os 4 nomes de pacote;
`url` só `https://github.com/MateusCosta888/Lunmi/releases/download/...`; redirecionamentos só para
os hosts de `desktop/electron/update-download.ts`; versão mais nova do que a instalada.

Releases no GitHub: `packs` (assets de pacotes, nunca apagados enquanto referenciados),
`channel-beta` (`latest.json` + `.sig`, substituídos ao publicar), `v<versão>` (setup + cópia do
manifesto). `voice-models-1` mantém-se (espelho do ditado).

## Setup (`Lunmi-<versão>-Setup.exe`)
O próprio lançador com um payload anexado: `[exe][pacotes .tar.zst…][manifest.json][índice][LUNMISFX v2]`.
O índice dá offset/tamanho/sha256 de cada pacote. O manifesto embutido é o mesmo esquema **sem**
`setup` (evita a autorreferência); o CI corre
`Lunmi.exe bundle <manifesto-sem-setup.json> <pasta-com-os-.tar.zst> <saída-Setup.exe>`, calcula
size/sha256 do setup e só então escreve e assina o `latest.json` final (com `setup`).

`resources\lunmi-components.json` mantém o formato atual lido por `componentDir()`:
`{"python":"<16 hex>","voice":"<16 hex>"}`. Ordem no CI: `pack` python e voice → escrever as
chaves neste ficheiro dentro da pasta do pacote `app` → `pack` app. A instalação usa a mesma rotina que a
atualização (`install_pack`), lendo do payload em vez da rede. Funciona sem internet.
UI: a atual (`desktop/installer/ui/index.html`), sem escolha de pasta.

## CLI do `ROOT\Lunmi.exe`
Toda a saída para a app são linhas JSON no stdout; nunca caminhos nem URLs na mensagem de erro.
| comando | efeito |
|---|---|
| (nenhum) / `--launch [args…]` | lança `versions\<current>\Lunmi-app.exe args…`; se `healthy=false` e `attempts≥2`, ativa `previous`, regista `rollback` |
| `--setup` (quando há payload) | UI de instalação; instala, cria atalhos e `HKCU\…\Uninstall\Lunmi`, lança |
| `--check` | `{"phase":"available","version","notes"}` ou `{"phase":"idle"}` ou `{"phase":"error","error"}` |
| `--stage <versão>` | transfere e instala pacotes em falta, monta `versions\<v>`; progresso `{"phase":"downloading","percent"}` … `{"phase":"ready","version"}` |
| `--activate <versão> [--wait-pid <pid>]` | espera até 30 s que o processo `<pid>` (o Electron principal) termine, grava `current.json` (antigo vai para `previous`, `healthy=false`, `attempts=0`), lança |
| `--healthy <versão>` | `healthy=true` se for a versão atual; depois apaga o que não é current/previous (`gc`) |
| `--uninstall` | pergunta; remove `ROOT`, atalhos e chave de registo; `DATA` fica salvo se o utilizador não pedir o contrário |
| `pack <dir> <nome> <saída>` | (CI) gera `<nome>-<chave>.tar.zst` e imprime `{"name","key","size","unpacked","sha256"}` |

`attempts` sobe 1 a cada lançamento enquanto `healthy=false`.

**Exclusão:** `--stage`, `--activate`, o setup e `--gc` tomam `ROOT\install.lock` (aberto sem partilha;
`--stage`/`--gc` ocupados → `busy`). `gc` mantém current, previous e qualquer versão montada mais nova
do que a atual (pronta para ativar) com os seus pacotes. `--healthy` sem o bloqueio em 10 s grava
`ROOT\healthy.<versão>`, aplicado no lançamento seguinte, e não faz `gc`.

## Lado Electron
- `desktop/electron/updates.ts` mantém a interface `UpdateState`/`UpdatesApi`
  (`desktop/src/shared/updates.ts`) e o IPC atual. `check()` → `--check`; `install()` →
  `--stage` (fase `downloading` com percentagem), depois `ready`; reiniciar →
  spawn `--activate <v>` desacoplado e `app.quit()`.
- Após `session.ready`, a app corre `Lunmi.exe --healthy <a sua versão>` (desacoplado, ignora falhas).
- Em desenvolvimento (`npm start`) não há `ROOT\Lunmi.exe`: atualizações `disabled`.
- Remove `electron-updater`, `NsisUpdater`, `update-download.ts`, `latest.yml`.

## Códigos de erro (`error` no JSON; a UI traduz)
`offline` · `source` (host/URL fora da lista) · `signature` · `hash` · `space` · `locked`
(não foi possível trocar/remover após retentativas) · `extract` · `busy` (app ainda aberta após 30 s)
· `internal`. Todos registados com detalhe em `install.log`. `UpdateState.error`
(`desktop/src/shared/updates.ts`) passa a aceitar estes códigos além dos atuais
(`unavailable`, `verification`, `busy`, `cancelled`), com texto próprio no painel de atualizações.

## Matriz de aceitação (VM, antes de publicar)
1. Instalação limpa offline; abre à primeira.
2. beta.1 → beta.2 só com `app` mudado: transfere só `app`; Python não é reextraído.
3. Atualizar com a app aberta (a usar voz).
4. Rede cortada a meio: retoma sem recomeçar.
5. Assinatura errada / hash errado: recusa, versão atual intacta.
6. Versão nova que não chega a `healthy` em 2 arranques: volta sozinha à anterior.
7. Documentos indexado + proteção de pastas ligada: sem efeito.
8. Utilizador com acentos.
9. Desinstalar mantém `DATA`; reinstalar reaproveita voz e ditado.
10. Dados das alphas aparecem na beta.
