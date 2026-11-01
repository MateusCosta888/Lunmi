//! Instalador da Lunmi: janela própria (WebView2) por cima do instalador NSIS do electron-builder.
//!
//! O executável final é `lunmi-setup.exe` + payload (o Setup NSIS) + manifesto JSON + trailer de
//! 24 bytes (`payload_len`, `manifest_len`, "LUNMISFX"), montado por `scripts/wrap_installer.mjs`.
//! O NSIS corre em silêncio (`/S --lunmi-ui`); o progresso vem do que ele escreve no disco.
#![windows_subsystem = "windows"]

use std::borrow::Cow;
use std::fs;
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::os::windows::ffi::OsStrExt;
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};
use std::thread;
use std::time::Duration;

use serde::Deserialize;
use serde_json::{json, Value};
use tao::event::{Event, StartCause, WindowEvent};
use tao::event_loop::{ControlFlow, EventLoopBuilder, EventLoopProxy};
use tao::platform::run_return::EventLoopExtRunReturn;
use tao::platform::windows::{WindowBuilderExtWindows, WindowExtWindows};
use tao::window::WindowBuilder;
use wry::http::{header::CONTENT_TYPE, Response};
use wry::WebViewBuilder;

const MAGIC: &[u8; 8] = b"LUNMISFX";
const APP_EXE: &str = "Lunmi.exe";

const INDEX: &str = include_str!("../ui/index.html");
const ORB: &str = include_str!("../../src/features/chat/orb/liveRenderer.js");
const FONT: &[u8] = include_bytes!("../../src/assets/fonts/PlusJakartaSans-Variable.ttf");
const SYMBOL: &[u8] = include_bytes!("../../src/assets/branding/lunmi-symbol.svg");

#[derive(Deserialize, Clone)]
struct Component { name: String, key: String, archive_bytes: u64, unpacked_bytes: u64 }

#[derive(Deserialize, Clone)]
struct Manifest { version: String, app_bytes: u64, components: Vec<Component> }

struct Bundle { exe: PathBuf, payload_offset: u64, payload_len: u64, manifest: Manifest }

fn read_bundle() -> io::Result<Bundle> {
    let bad = || io::Error::new(io::ErrorKind::InvalidData, "instalador incompleto");
    let exe = std::env::current_exe()?;
    let mut file = fs::File::open(&exe)?;
    let len = file.metadata()?.len();
    let mut trailer = [0u8; 24];
    file.seek(SeekFrom::End(-24))?;
    file.read_exact(&mut trailer)?;
    if &trailer[16..] != MAGIC { return Err(bad()); }
    let payload_len = u64::from_le_bytes(trailer[0..8].try_into().unwrap());
    let manifest_len = u64::from_le_bytes(trailer[8..16].try_into().unwrap());
    let manifest_offset = len.checked_sub(24 + manifest_len).ok_or_else(bad)?;
    let payload_offset = manifest_offset.checked_sub(payload_len).ok_or_else(bad)?;
    let mut raw = vec![0u8; manifest_len as usize];
    file.seek(SeekFrom::Start(manifest_offset))?;
    file.read_exact(&mut raw)?;
    let manifest = serde_json::from_slice(&raw).map_err(|_| bad())?;
    Ok(Bundle { exe, payload_offset, payload_len, manifest })
}

/// Pasta temporária desta execução: o setup NSIS copiado e os dados do WebView2.
fn work_dir() -> PathBuf { std::env::temp_dir().join(format!("lunmi-setup-{}", std::process::id())) }

const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;

/// Pasta própria e real (não junction/symlink), exatamente `work_dir()`; cria-a se faltar.
fn ensure_work_dir() -> io::Result<PathBuf> {
    use std::os::windows::fs::MetadataExt;
    let dir = work_dir();
    if !dir.is_absolute() { return Err(io::Error::other("pasta temporária relativa")); }
    match fs::symlink_metadata(&dir) {
        Ok(m) if m.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 || !m.is_dir() =>
            return Err(io::Error::other("a pasta temporária do instalador não é uma pasta normal")),
        Ok(_) => {}
        Err(_) => fs::create_dir_all(&dir)?,
    }
    Ok(dir)
}

/// Apaga a pasta de trabalho só se for a pasta real desta execução; o WebView2 pode segurar
/// ficheiros uns segundos depois de fechar. O que não sair fica registado como resíduo.
fn clean_work_dir() {
    use std::os::windows::fs::MetadataExt;
    let dir = work_dir();
    for _ in 0..20 {
        match fs::symlink_metadata(&dir) {
            Err(_) => return,
            Ok(m) if m.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 => {
                log(&format!("cleanup skipped: {} is a reparse point", dir.display()));
                return;
            }
            Ok(_) => if fs::remove_dir_all(&dir).is_ok() { return; },
        }
        thread::sleep(Duration::from_millis(250));
    }
    log(&format!("cleanup residue: {}", dir.display()));
}

/// Diagnóstico local (%LOCALAPPDATA%\Lunmi\logs\setup.log): modo, códigos de saída e durações.
/// Só argumentos de linha de comando e caminhos; nunca dados da conta ou chaves.
fn log(line: &str) {
    let Ok(root) = std::env::var("LOCALAPPDATA") else { return };
    let dir = Path::new(&root).join("Lunmi").join("logs");
    let path = dir.join("setup.log");
    let _ = fs::create_dir_all(&dir);
    if fs::metadata(&path).map(|m| m.len() > 512 * 1024).unwrap_or(false) { let _ = fs::rename(&path, dir.join("setup.log.1")); }
    let secs = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(&path) {
        let _ = writeln!(f, "{secs} pid={} {line}", std::process::id());
    }
}

/// Apaga a pasta de trabalho desta execução em qualquer saída (erro, fallback, fim normal).
/// Declarar antes do WebView para ser libertada depois dele.
struct WorkDirGuard;
impl Drop for WorkDirGuard { fn drop(&mut self) { clean_work_dir(); } }

/// Margem além do próprio payload no volume TEMP.
const TEMP_MARGIN: u64 = 64 << 20;

/// Copia o Setup NSIS para %TEMP%; `progress` recebe a fração copiada.
fn extract_payload(bundle: &Bundle, progress: &dyn Fn(f64)) -> io::Result<PathBuf> {
    let dir = ensure_work_dir()?;
    // O NSIS só confere o TEMP depois de arrancar; sem espaço para a cópia nem chegaria a correr.
    if let Some(free) = free_bytes(&dir.to_string_lossy()) {
        if free < bundle.payload_len + TEMP_MARGIN {
            return Err(io::Error::new(io::ErrorKind::StorageFull, format!(
                "espaço insuficiente na pasta temporária: são precisos {} MB e há {} MB",
                (bundle.payload_len + TEMP_MARGIN) >> 20, free >> 20)));
        }
    }
    let target = dir.join("Lunmi-Setup.exe");
    copy_range(&bundle.exe, bundle.payload_offset, bundle.payload_len, &target, progress)?;
    log(&format!("payload extracted bytes={}", bundle.payload_len));
    Ok(target)
}

/// Copia `len` bytes de `src` a partir de `offset`; em qualquer falha apaga o ficheiro parcial.
fn copy_range(src: &Path, offset: u64, len: u64, target: &Path, progress: &dyn Fn(f64)) -> io::Result<()> {
    let result = (|| {
        let mut input = fs::File::open(src)?;
        input.seek(SeekFrom::Start(offset))?;
        let mut input = input.take(len);
        let mut out = io::BufWriter::with_capacity(1 << 20, fs::File::create(target)?);
        let mut buf = vec![0u8; 8 << 20];
        let mut copied = 0u64;
        loop {
            let n = input.read(&mut buf)?;
            if n == 0 { break; }
            out.write_all(&buf[..n])?;
            copied += n as u64;
            progress(copied as f64 / len.max(1) as f64);
        }
        out.flush()?;
        if copied != len { return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "instalador incompleto")); }
        Ok(())
    })();
    if result.is_err() { let _ = fs::remove_file(target); }
    result
}

// ---------- Argumentos (os mesmos do NSIS; o electron-updater passa --updated /S --force-run [/D=]) ----------

struct Args { updated: bool, silent: bool, force_run: bool, dir: Option<String>, rest: Vec<String> }

fn parse_args() -> Args {
    let mut args = Args { updated: false, silent: false, force_run: false, dir: None, rest: vec![] };
    for a in std::env::args().skip(1) {
        match a.as_str() {
            "--updated" => { args.updated = true; args.rest.push(a); }
            "/S" => args.silent = true,
            "--force-run" => { args.force_run = true; args.rest.push(a); }
            _ if a.starts_with("/D=") => args.dir = Some(a[3..].to_string()),
            _ => args.rest.push(a),
        }
    }
    args
}

fn run_payload(setup: &Path, silent: bool, extra: &[String], dir: Option<&str>) -> io::Result<std::process::Child> {
    let mut cmd = Command::new(setup);
    if silent { cmd.arg("/S").creation_flags(0x0800_0000); } // CREATE_NO_WINDOW: nada de consola a piscar
    cmd.args(extra);
    // O NSIS só aceita /D= no fim e sem aspas, mesmo com espaços.
    if let Some(d) = dir { cmd.raw_arg(format!("/D={d}")); }
    cmd.spawn()
}

// ---------- Windows: disco, registo, janela ----------

fn wide(s: &str) -> Vec<u16> { std::ffi::OsStr::new(s).encode_wide().chain(Some(0)).collect() }

/// Espaço livre no volume do caminho (sobe até à primeira pasta que exista).
fn free_bytes(path: &str) -> Option<u64> {
    let mut p = PathBuf::from(path);
    while !p.exists() { if !p.pop() { return None; } }
    let mut free = 0u64;
    let ok = unsafe { windows_sys::Win32::Storage::FileSystem::GetDiskFreeSpaceExW(wide(&p.to_string_lossy()).as_ptr(), &mut free, std::ptr::null_mut(), std::ptr::null_mut()) };
    (ok != 0).then_some(free)
}

/// Pasta da instalação existente (chave de desinstalação do electron-builder em HKCU).
fn installed_dir() -> Option<String> {
    use windows_sys::Win32::System::Registry::*;
    let base = r"Software\Microsoft\Windows\CurrentVersion\Uninstall";
    let read = |sub: &str, name: &str| -> Option<String> {
        let mut buf = vec![0u16; 1024];
        let mut size = (buf.len() * 2) as u32;
        let rc = unsafe { RegGetValueW(HKEY_CURRENT_USER, wide(sub).as_ptr(), wide(name).as_ptr(), RRF_RT_REG_SZ, std::ptr::null_mut(), buf.as_mut_ptr().cast(), &mut size) };
        if rc != 0 { return None; }
        buf.truncate((size as usize / 2).saturating_sub(1));
        Some(String::from_utf16_lossy(&buf))
    };
    let mut key: HKEY = std::ptr::null_mut();
    if unsafe { RegOpenKeyExW(HKEY_CURRENT_USER, wide(base).as_ptr(), 0, KEY_READ, &mut key) } != 0 { return None; }
    let mut found = None;
    for i in 0.. {
        let mut name = [0u16; 256];
        let mut len = name.len() as u32;
        let rc = unsafe { RegEnumKeyExW(key, i, name.as_mut_ptr(), &mut len, std::ptr::null(), std::ptr::null_mut(), std::ptr::null_mut(), std::ptr::null_mut()) };
        if rc != 0 { break; }
        let sub = format!(r"{base}\{}", String::from_utf16_lossy(&name[..len as usize]));
        if read(&sub, "DisplayName").as_deref() == Some("Lunmi") {
            // O electron-builder nem sempre preenche InstallLocation; o desinstalador está na pasta.
            found = read(&sub, "InstallLocation").filter(|d| !d.is_empty()).or_else(|| {
                let cmd = read(&sub, "UninstallString")?;
                let exe = cmd.trim_start_matches('"').split('"').next()?.to_string();
                Path::new(&exe).parent().map(|p| p.to_string_lossy().to_string())
            });
            break;
        }
    }
    unsafe { RegCloseKey(key) };
    found
}

fn default_dir() -> String {
    installed_dir().unwrap_or_else(|| {
        let local = std::env::var("LOCALAPPDATA").unwrap_or_default();
        format!(r"{local}\Programs\Lunmi")
    })
}

fn style_window(hwnd: isize) {
    use windows_sys::Win32::Graphics::Dwm::{DwmSetWindowAttribute, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND};
    use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows_sys::Win32::UI::WindowsAndMessaging::{LoadIconW, SendMessageW, ICON_BIG, ICON_SMALL, WM_SETICON};
    let hwnd = hwnd as windows_sys::Win32::Foundation::HWND;
    unsafe {
        let pref = DWMWCP_ROUND;
        DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE as _, (&pref as *const i32).cast(), 4);
        // Ícone 1 do executável (build.rs), para a barra de tarefas e o Alt+Tab.
        let icon = LoadIconW(GetModuleHandleW(std::ptr::null()), 1 as _);
        if !icon.is_null() {
            SendMessageW(hwnd, WM_SETICON, ICON_BIG as usize, icon as isize);
            SendMessageW(hwnd, WM_SETICON, ICON_SMALL as usize, icon as isize);
        }
    }
}

// ---------- Progresso ----------

fn dir_size(path: &Path) -> u64 {
    let Ok(entries) = fs::read_dir(path) else { return fs::metadata(path).map(|m| m.len()).unwrap_or(0) };
    entries.flatten().map(|e| match e.file_type() {
        Ok(t) if t.is_dir() => dir_size(&e.path()),
        _ => e.metadata().map(|m| m.len()).unwrap_or(0),
    }).sum()
}

/// Estima o avanço pelo que o NSIS escreve: componentes em `<dir>-runtime` (arquivo .7z e depois
/// `<nome>-<chave>.partial`, com `.lunmi-complete` no fim) e a aplicação em `<dir>`.
struct Tracker { dir: PathBuf, runtime: PathBuf, missing: Vec<Component>, app_bytes: u64, app_floor: Option<u64>, best: f64 }

impl Tracker {
    fn new(dir: &str, manifest: &Manifest) -> Self {
        let runtime = PathBuf::from(format!("{dir}-runtime"));
        let missing = manifest.components.iter()
            .filter(|c| !runtime.join(format!("{}-{}", c.name, c.key)).join(".lunmi-complete").exists())
            .cloned().collect();
        Tracker { dir: PathBuf::from(dir), runtime, missing, app_bytes: manifest.app_bytes.max(1), app_floor: None, best: 0.0 }
    }

    fn total(&self) -> u64 { self.app_bytes + self.missing.iter().map(|c| c.archive_bytes + c.unpacked_bytes).sum::<u64>() }

    /// (fração 0..1, passo atual: o nome do componente ou "app")
    fn sample(&mut self) -> (f64, String) {
        let total = self.total();
        let mut done = 0u64;
        let mut current = None;
        for c in &self.missing {
            let size = c.archive_bytes + c.unpacked_bytes;
            let target = self.runtime.join(format!("{}-{}", c.name, c.key));
            if target.join(".lunmi-complete").exists() { done += size; continue; }
            let partial = PathBuf::from(format!("{}.partial", target.display()));
            done += size.min(dir_size(&self.runtime.join(format!("{}.7z", c.name))) + dir_size(&partial));
            current = Some(c.name.clone());
            break;
        }
        if let Some(step) = current { return (self.keep(done, total), step); }
        // Numa atualização a versão antiga ainda está na pasta: conta a partir do menor tamanho visto.
        let now = dir_size(&self.dir);
        let floor = self.app_floor.map_or(now, |f| f.min(now));
        self.app_floor = Some(floor);
        done += self.app_bytes.min(now - floor);
        (self.keep(done, total), "app".into())
    }

    fn keep(&mut self, done: u64, total: u64) -> f64 {
        self.best = self.best.max(done as f64 / total.max(1) as f64);
        self.best
    }
}

// ---------- Interface ----------

enum UserEvent { Ipc(String), Eval(String), Exit(u8) }

fn send(proxy: &EventLoopProxy<UserEvent>, msg: Value) {
    let _ = proxy.send_event(UserEvent::Eval(format!("window.lunmi && window.lunmi.update({msg})")));
}

fn install(bundle: &Bundle, args: &Args, dir: String, desktop: bool, start_menu: bool, proxy: EventLoopProxy<UserEvent>) {
    let progress = |p: f64, step: &str| send(&proxy, json!({ "type": "progress", "percent": p * 100.0, "step": step }));
    let mut tracker = Tracker::new(&dir, &bundle.manifest);
    // A barra reparte-se pelos bytes: copiar o setup (meio peso, é rápido) e o que o NSIS escreve.
    let copy_weight = bundle.payload_len as f64 * 0.5;
    let share = copy_weight / (copy_weight + tracker.total() as f64);
    let setup = match extract_payload(bundle, &|f| progress(f * share, "prepare")) {
        Ok(p) => p,
        Err(e) => {
            log(&format!("extract failed: {e}"));
            recover_before_dispatch(args, &dir, "extract");
            return send(&proxy, json!({ "type": "error", "code": -1, "detail": e.to_string() }));
        }
    };
    let mut extra = args.rest.clone();
    extra.push("--lunmi-ui".into());
    if !desktop { extra.push("--no-desktop-shortcut".into()); }
    if !start_menu { extra.push("--no-start-menu-shortcut".into()); }
    let skipped: Vec<&str> = bundle.manifest.components.iter()
        .filter(|c| !tracker.missing.iter().any(|m| m.name == c.name)).map(|c| c.name.as_str()).collect();
    send(&proxy, json!({ "type": "skipped", "steps": skipped }));
    // Numa atualização o NSIS encontra a pasta no registo; só se reenvia o /D= que a app passou.
    let target = if args.updated { args.dir.as_deref() } else { Some(dir.as_str()) };
    log(&format!("nsis start dir={dir} target={target:?} args={extra:?} missing={:?}", tracker.missing.iter().map(|c| &c.name).collect::<Vec<_>>()));
    let started = std::time::Instant::now();
    let outcome = dispatch(
        || run_payload(&setup, true, &extra, target),
        |child: &mut std::process::Child| child.try_wait().map(|s| s.map(|s| s.code())),
        || {
            let (f, step) = tracker.sample();
            progress(share + f * (0.99 - share), &step);
            thread::sleep(Duration::from_millis(400));
        },
    );
    // Com o NSIS possivelmente ainda a correr (estado incerto) o setup fica; a guarda tenta no fim.
    if !matches!(outcome, Dispatch::Uncertain(_)) { let _ = fs::remove_file(&setup); }
    match outcome {
        Dispatch::Finished(Some(0)) => {
            log(&format!("nsis exit code=0 secs={}", started.elapsed().as_secs()));
            progress(1.0, "shortcuts");
            // Numa atualização com --force-run o próprio NSIS já reabriu a Lunmi.
            if args.updated && !args.force_run { launch(&dir); }
            send(&proxy, json!({ "type": "done", "dir": dir }));
        }
        Dispatch::Finished(code) => {
            log(&format!("nsis exit code={code:?} secs={}", started.elapsed().as_secs()));
            send(&proxy, json!({ "type": "error", "code": code.unwrap_or(-1) }));
        }
        Dispatch::NotStarted(e) => {
            log(&format!("nsis spawn failed: {e}"));
            recover_before_dispatch(args, &dir, "spawn");
            send(&proxy, json!({ "type": "error", "code": -1, "detail": e.to_string() }));
        }
        Dispatch::Uncertain(e) => {
            // O NSIS arrancou: pode ainda estar a instalar ou já ter reaberto a Lunmi. Não se reabre nada.
            log(&format!("nsis wait failed, state uncertain, no recovery: {e}"));
            send(&proxy, json!({ "type": "error", "code": -2, "detail": e.to_string() }));
        }
    }
}

/// Resultado do despacho do NSIS. Só `NotStarted` autoriza reabrir a versão anterior.
#[derive(Debug)]
enum Dispatch { NotStarted(io::Error), Finished(Option<i32>), Uncertain(io::Error) }

/// Arranca o processo e acompanha-o: `poll` devolve `Some(código)` quando terminou e `None`
/// enquanto corre (chamando `tick`). Um erro depois do arranque é estado incerto, nunca "não arrancou".
fn dispatch<C>(spawn: impl FnOnce() -> io::Result<C>, mut poll: impl FnMut(&mut C) -> io::Result<Option<Option<i32>>>, mut tick: impl FnMut()) -> Dispatch {
    let mut child = match spawn() { Ok(c) => c, Err(e) => return Dispatch::NotStarted(e) };
    loop {
        match poll(&mut child) {
            Ok(Some(code)) => return Dispatch::Finished(code),
            Ok(None) => tick(),
            Err(e) => return Dispatch::Uncertain(e),
        }
    }
}

fn launch(dir: &str) { let _ = Command::new(Path::new(dir).join(APP_EXE)).current_dir(dir).spawn(); }

/// A Lunmi fechou para atualizar mas o NSIS não chegou a arrancar (cópia ou spawn falhados):
/// nada foi alterado, por isso reabre-se a versão instalada, sem `--updated` (nada é repetido).
/// Depois de o NSIS arrancar é o `lunmiAbort` dele que reabre.
fn recover_before_dispatch(args: &Args, dir: &str, reason: &str) {
    if !args.updated { return; }
    log(&format!("update not dispatched ({reason}); reopening installed version at {dir}"));
    launch(dir);
}

fn needed_bytes(bundle: &Bundle, dir: &str) -> u64 {
    let t = Tracker::new(dir, &bundle.manifest);
    let biggest_archive = t.missing.iter().map(|c| c.archive_bytes).max().unwrap_or(0);
    bundle.manifest.app_bytes + t.missing.iter().map(|c| c.unpacked_bytes).sum::<u64>() + biggest_archive + bundle.payload_len
}

fn asset(path: &str) -> Response<Cow<'static, [u8]>> {
    let (mime, body): (&str, Cow<'static, [u8]>) = match path {
        "/" | "/index.html" => ("text/html; charset=utf-8", Cow::Borrowed(INDEX.as_bytes())),
        "/orb.js" => ("text/javascript; charset=utf-8", Cow::Owned(ORB.replace("export function mountLiveOrb", "function mountLiveOrb").into_bytes())),
        "/font.ttf" => ("font/ttf", Cow::Borrowed(FONT)),
        "/lunmi-symbol.svg" => ("image/svg+xml", Cow::Borrowed(SYMBOL)),
        _ => return Response::builder().status(404).body(Cow::Borrowed(&[][..])).unwrap(),
    };
    Response::builder().header(CONTENT_TYPE, mime).body(body).unwrap()
}

fn main() -> ExitCode {
    let args = parse_args();
    let bundle = match read_bundle() {
        Ok(b) => b,
        Err(_) => { message("Este instalador está incompleto ou danificado. Volta a descarregá-lo."); return ExitCode::from(1); }
    };

    let _work = WorkDirGuard; // limpa %TEMP%\lunmi-setup-<pid> em todos os retornos (se for a pasta real)
    log(&format!("start version={} updated={} silent={} force_run={} dir={:?} rest={:?}",
        bundle.manifest.version, args.updated, args.silent, args.force_run, args.dir, args.rest));

    // /S sem --updated: instalação automática (testes, scripts), sem janela.
    if args.silent && !args.updated { return headless(&bundle, &args, true, "headless"); }

    let dir = args.dir.clone().unwrap_or_else(default_dir);
    let update = args.updated;
    let mut event_loop = EventLoopBuilder::<UserEvent>::with_user_event().build();
    let proxy = event_loop.create_proxy();
    let (w, h) = if update { (560.0, 330.0) } else { (860.0, 540.0) };
    let window = WindowBuilder::new()
        .with_title(if update { "Atualização da Lunmi" } else { "Instalador da Lunmi" })
        .with_inner_size(tao::dpi::LogicalSize::new(w, h))
        .with_resizable(false)
        .with_decorations(false)
        .with_undecorated_shadow(true)
        .with_visible(false)
        .build(&event_loop)
        .expect("janela");
    if let Some(m) = window.current_monitor() {
        let (ms, mp, ws) = (m.size(), m.position(), window.outer_size());
        window.set_outer_position(tao::dpi::PhysicalPosition::new(mp.x + (ms.width as i32 - ws.width as i32) / 2, mp.y + (ms.height as i32 - ws.height as i32) / 2));
    }
    style_window(window.hwnd());

    let init = json!({
        "mode": if update { "update" } else { "install" },
        "version": bundle.manifest.version,
        "dir": dir,
        "free": free_bytes(&dir),
        "need": needed_bytes(&bundle, &dir),
    });
    let ipc_proxy = proxy.clone();
    // Dados do WebView2 na pasta temporária desta execução, nunca ao lado do Setup (Downloads, cache do updater).
    let work = match ensure_work_dir() {
        Ok(d) => d,
        Err(e) => { log(&format!("work dir unusable: {e}")); drop(window); return headless(&bundle, &args, args.silent, "fallback"); }
    };
    let mut web_context = wry::WebContext::new(Some(work.join("webview")));
    let webview = WebViewBuilder::new_with_web_context(&mut web_context)
        .with_custom_protocol("lunmi".into(), |_, req| asset(req.uri().path()))
        .with_url("lunmi://app/index.html")
        .with_background_color((3, 5, 8, 255))
        .with_initialization_script(format!("window.LUNMI_INIT = {init};"))
        .with_ipc_handler(move |req| { let _ = ipc_proxy.send_event(UserEvent::Ipc(req.body().clone())); })
        .with_devtools(cfg!(debug_assertions))
        .build(&window);
    let webview = match webview {
        Ok(v) => v,
        Err(e) => {
            // Sem WebView2: o instalador NSIS normal faz tudo, com a sua própria janela.
            log(&format!("webview unavailable: {e}"));
            drop(window);
            drop(web_context);
            return headless(&bundle, &args, args.silent, "fallback");
        }
    };

    let bundle = std::sync::Arc::new(bundle);
    let args = std::sync::Arc::new(args);
    let mut busy = false;
    let mut exit_code = 1u8;
    event_loop.run_return(|event, _, flow| {
        *flow = ControlFlow::Wait;
        match event {
            Event::NewEvents(StartCause::Init) => {}
            Event::WindowEvent { event: WindowEvent::CloseRequested, .. } if !busy => *flow = ControlFlow::Exit,
            Event::UserEvent(UserEvent::Eval(js)) => { let _ = webview.evaluate_script(&js); }
            Event::UserEvent(UserEvent::Exit(code)) => { exit_code = code; *flow = ControlFlow::Exit; }
            Event::UserEvent(UserEvent::Ipc(raw)) => {
                let Ok(msg) = serde_json::from_str::<Value>(&raw) else { return };
                match msg["cmd"].as_str().unwrap_or("") {
                    "ready" => {
                        // A janela pequena de atualização fica por cima: a Lunmi fechou e o utilizador espera por ela.
                        if update { window.set_always_on_top(true); }
                        window.set_visible(true);
                        window.set_focus();
                    }
                    "drag" => { let _ = window.drag_window(); }
                    "minimize" => window.set_minimized(true),
                    "close" if !busy => *flow = ControlFlow::Exit,
                    "pick" => {
                        let current = msg["dir"].as_str().unwrap_or("").to_string();
                        if let Some(mut picked) = rfd::FileDialog::new().set_parent(&window).set_directory(&current).pick_folder() {
                            // Como o NSIS: a pasta escolhida recebe a subpasta Lunmi.
                            if !picked.file_name().is_some_and(|n| n.eq_ignore_ascii_case("Lunmi")) { picked.push("Lunmi"); }
                            let d = picked.to_string_lossy().to_string();
                            send(&proxy, json!({ "type": "dir", "dir": d, "free": free_bytes(&d), "need": needed_bytes(&bundle, &d) }));
                        }
                    }
                    "install" if !busy => {
                        busy = true;
                        let (b, a, p) = (bundle.clone(), args.clone(), proxy.clone());
                        let d = msg["dir"].as_str().map(str::to_string).unwrap_or_else(|| dir.clone());
                        let (desk, start) = (msg["desktop"].as_bool().unwrap_or(true), msg["startMenu"].as_bool().unwrap_or(true));
                        thread::spawn(move || install(&b, &a, d, desk, start, p));
                    }
                    "finished" => {
                        // Fim da instalação (com êxito ou não): fechar volta a ser permitido.
                        busy = false;
                        if msg["open"].as_bool() == Some(true) { launch(msg["dir"].as_str().unwrap_or(&dir)); }
                        let code = msg["code"].as_i64().unwrap_or(0).clamp(0, 255) as u8;
                        if msg["exit"].as_bool() == Some(true) { let _ = proxy.send_event(UserEvent::Exit(code)); } else { exit_code = code; }
                    }
                    _ => {}
                }
            }
            _ => {}
        }
    });
    drop(webview);
    drop(web_context);
    log(&format!("exit code={exit_code}"));
    ExitCode::from(exit_code)
}

/// Sem janela própria: corre o NSIS (silencioso ou com a janela dele) e devolve o seu código.
fn headless(bundle: &Bundle, args: &Args, silent: bool, mode: &str) -> ExitCode {
    let started = std::time::Instant::now();
    let dir = || args.dir.clone().unwrap_or_else(default_dir);
    let setup = match extract_payload(bundle, &|_| ()) {
        Ok(s) => s,
        Err(e) => {
            log(&format!("{mode} extract failed: {e}"));
            recover_before_dispatch(args, &dir(), mode);
            return ExitCode::from(1);
        }
    };
    let outcome = dispatch(|| run_payload(&setup, silent, &args.rest, args.dir.as_deref()),
        |child: &mut std::process::Child| child.wait().map(|s| Some(s.code())), || {});
    if !matches!(outcome, Dispatch::Uncertain(_)) { let _ = fs::remove_file(&setup); }
    match outcome {
        Dispatch::Finished(code) => {
            log(&format!("{mode} nsis exit code={code:?} secs={}", started.elapsed().as_secs()));
            ExitCode::from(code.unwrap_or(1).clamp(0, 255) as u8)
        }
        Dispatch::NotStarted(e) => {
            log(&format!("{mode} spawn failed: {e}"));
            recover_before_dispatch(args, &dir(), mode);
            ExitCode::from(1)
        }
        Dispatch::Uncertain(e) => {
            log(&format!("{mode} wait failed, state uncertain, no recovery: {e}"));
            ExitCode::from(1)
        }
    }
}

fn message(text: &str) {
    use windows_sys::Win32::UI::WindowsAndMessaging::{MessageBoxW, MB_ICONERROR, MB_OK};
    unsafe { MessageBoxW(std::ptr::null_mut(), wide(text).as_ptr(), wide("Instalador da Lunmi").as_ptr(), MB_OK | MB_ICONERROR) };
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("lunmi-setup-test-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn copy_range_copies_the_exact_slice() {
        let d = scratch("ok");
        fs::write(d.join("src"), b"abcdefghij").unwrap();
        copy_range(&d.join("src"), 2, 5, &d.join("out"), &|_| ()).unwrap();
        assert_eq!(fs::read(d.join("out")).unwrap(), b"cdefg");
        fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn interrupted_copy_removes_the_partial_file() {
        let d = scratch("short");
        fs::write(d.join("src"), vec![7u8; 100]).unwrap();
        let err = copy_range(&d.join("src"), 0, 200, &d.join("out"), &|_| ()).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::UnexpectedEof);
        assert!(!d.join("out").exists(), "o parcial tem de ser apagado");
        fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn unwritable_target_fails_without_touching_it() {
        let d = scratch("dir");
        fs::write(d.join("src"), b"abc").unwrap();
        fs::create_dir(d.join("out")).unwrap(); // um diretório no lugar do ficheiro: create falha
        assert!(copy_range(&d.join("src"), 0, 3, &d.join("out"), &|_| ()).is_err());
        assert!(d.join("out").is_dir());
        fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn junction_work_dir_is_refused_and_left_alone() {
        let d = scratch("junction");
        let target = d.join("target"); fs::create_dir(&target).unwrap();
        fs::write(target.join("keep.txt"), b"x").unwrap();
        let link = work_dir();
        let _ = fs::remove_dir_all(&link);
        let ok = Command::new("cmd").args(["/c", "mklink", "/J"]).arg(&link).arg(&target).status().unwrap().success();
        assert!(ok);
        assert!(ensure_work_dir().is_err(), "uma junction no lugar da pasta tem de ser recusada");
        clean_work_dir();
        assert!(target.join("keep.txt").exists(), "a limpeza não pode seguir a junction");
        fs::remove_dir(&link).unwrap(); // remove só a junction
        fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn dispatch_separates_not_started_finished_and_uncertain() {
        // Não arrancou: o único caso que autoriza reabrir a versão anterior.
        let o = dispatch(|| Err::<u32, _>(io::Error::other("spawn")), |_| Ok(Some(Some(0))), || {});
        assert!(matches!(o, Dispatch::NotStarted(_)));
        // Arrancou e terminou: código preservado, ticks enquanto corre.
        let mut ticks = 0;
        let mut polls = 0;
        let o = dispatch(|| Ok(7u32), |_| { polls += 1; Ok(if polls < 3 { None } else { Some(Some(2)) }) }, || ticks += 1);
        assert!(matches!(o, Dispatch::Finished(Some(2))));
        assert_eq!(ticks, 2);
        // Arrancou e o acompanhamento falhou: incerto, nunca NotStarted.
        let mut polls = 0;
        let o = dispatch(|| Ok(7u32), |_| { polls += 1; if polls < 2 { Ok(None) } else { Err(io::Error::other("wait")) } }, || {});
        assert!(matches!(o, Dispatch::Uncertain(_)));
    }

    #[test]
    fn missing_setup_fails_to_spawn() {
        let d = scratch("spawn");
        assert!(run_payload(&d.join("nao-existe.exe"), true, &[], Some(r"C:\Lunmi Teste")).is_err());
        fs::remove_dir_all(d).unwrap();
    }
}
