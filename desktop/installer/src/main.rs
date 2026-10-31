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

/// Copia o Setup NSIS para %TEMP%; `progress` recebe a fração copiada.
fn extract_payload(bundle: &Bundle, progress: &dyn Fn(f64)) -> io::Result<PathBuf> {
    let dir = std::env::temp_dir().join(format!("lunmi-setup-{}", std::process::id()));
    fs::create_dir_all(&dir)?;
    let target = dir.join("Lunmi-Setup.exe");
    let mut src = fs::File::open(&bundle.exe)?;
    src.seek(SeekFrom::Start(bundle.payload_offset))?;
    let mut src = src.take(bundle.payload_len);
    let mut out = io::BufWriter::with_capacity(1 << 20, fs::File::create(&target)?);
    let mut buf = vec![0u8; 8 << 20];
    let mut copied = 0u64;
    loop {
        let n = src.read(&mut buf)?;
        if n == 0 { break; }
        out.write_all(&buf[..n])?;
        copied += n as u64;
        progress(copied as f64 / bundle.payload_len.max(1) as f64);
    }
    out.flush()?;
    if copied != bundle.payload_len { return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "instalador incompleto")); }
    Ok(target)
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
        Err(e) => return send(&proxy, json!({ "type": "error", "code": -1, "detail": e.to_string() })),
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
    let result = run_payload(&setup, true, &extra, target).and_then(|mut child| loop {
        if let Some(status) = child.try_wait()? { break Ok(status); }
        let (f, step) = tracker.sample();
        progress(share + f * (0.99 - share), &step);
        thread::sleep(Duration::from_millis(400));
    });
    let _ = fs::remove_dir_all(setup.parent().unwrap());
    match result {
        Ok(s) if s.success() => {
            progress(1.0, "shortcuts");
            // Numa atualização com --force-run o próprio NSIS já reabriu a Lunmi.
            if args.updated && !args.force_run { launch(&dir); }
            send(&proxy, json!({ "type": "done", "dir": dir }));
        }
        Ok(s) => send(&proxy, json!({ "type": "error", "code": s.code().unwrap_or(-1) })),
        Err(e) => send(&proxy, json!({ "type": "error", "code": -1, "detail": e.to_string() })),
    }
}

fn launch(dir: &str) { let _ = Command::new(Path::new(dir).join(APP_EXE)).current_dir(dir).spawn(); }

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

    // /S sem --updated: instalação automática (testes, scripts), sem janela.
    if args.silent && !args.updated {
        return match extract_payload(&bundle, &|_| ()).and_then(|s| {
            let code = run_payload(&s, true, &args.rest, args.dir.as_deref())?.wait()?.code().unwrap_or(1);
            let _ = fs::remove_dir_all(s.parent().unwrap());
            Ok(code)
        }) { Ok(c) => ExitCode::from(c as u8), Err(_) => ExitCode::from(1) };
    }

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
    let webview = WebViewBuilder::new()
        .with_custom_protocol("lunmi".into(), |_, req| asset(req.uri().path()))
        .with_url("lunmi://app/index.html")
        .with_background_color((3, 5, 8, 255))
        .with_initialization_script(format!("window.LUNMI_INIT = {init};"))
        .with_ipc_handler(move |req| { let _ = ipc_proxy.send_event(UserEvent::Ipc(req.body().clone())); })
        .with_devtools(cfg!(debug_assertions))
        .build(&window);
    let webview = match webview {
        Ok(v) => v,
        Err(_) => {
            // Sem WebView2: o instalador NSIS normal faz tudo, com a sua própria janela.
            drop(window);
            return match extract_payload(&bundle, &|_| ()).and_then(|s| {
                let code = run_payload(&s, args.silent, &args.rest, args.dir.as_deref())?.wait()?.code().unwrap_or(1);
                let _ = fs::remove_dir_all(s.parent().unwrap());
                Ok(code)
            }) { Ok(c) => ExitCode::from(c as u8), Err(_) => ExitCode::from(1) };
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
    ExitCode::from(exit_code)
}

fn message(text: &str) {
    use windows_sys::Win32::UI::WindowsAndMessaging::{MessageBoxW, MB_ICONERROR, MB_OK};
    unsafe { MessageBoxW(std::ptr::null_mut(), wide(text).as_ptr(), wide("Instalador da Lunmi").as_ptr(), MB_OK | MB_ICONERROR) };
}
