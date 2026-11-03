//! `Lunmi.exe`: instalador (quando traz payload), lançador, atualizador e desinstalador.
//! Contrato completo em docs/UPDATE-SYSTEM.md.
#![windows_subsystem = "windows"]

mod common;
mod layout;
mod pack;
mod setup;
mod update;

use std::borrow::Cow;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use serde_json::{json, Value};
use tao::event::{Event, StartCause, WindowEvent};
use tao::event_loop::{ControlFlow, EventLoopBuilder, EventLoopProxy};
use tao::platform::run_return::EventLoopExtRunReturn;
use tao::platform::windows::{WindowBuilderExtWindows, WindowExtWindows};
use tao::window::WindowBuilder;
use wry::http::{header::CONTENT_TYPE, Response};
use wry::WebViewBuilder;

use common::{data, emit, free_bytes, log, remove_tree, root, wide, Fail, Res};
use layout::{activated, on_launch, Layout};

const INDEX: &str = include_str!("../ui/index.html");
const ORB: &str = include_str!("../../src/features/chat/orb/liveRenderer.js");
const FONT: &[u8] = include_bytes!("../../src/assets/fonts/PlusJakartaSans-Variable.ttf");
const SYMBOL: &[u8] = include_bytes!("../../src/assets/branding/lunmi-symbol.svg");

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let me = std::env::current_exe().unwrap_or_default();
    let payload = setup::read_payload(&me);
    let first = args.first().map(String::as_str).unwrap_or("");
    let layout = Layout::new(root());
    let result: Res<ExitCode> = match first {
        "pack" if args.len() == 4 => pack::pack(Path::new(&args[1]), &args[2], Path::new(&args[3]))
            .map(|i| { emit(serde_json::to_value(i).unwrap()); ExitCode::SUCCESS }).map_err(Fail::from),
        "bundle" if args.len() == 4 => setup::bundle(Path::new(&args[1]), Path::new(&args[2]), Path::new(&args[3])).map(|_| ExitCode::SUCCESS),
        "--check" => update::check(&layout).map(|m| {
            emit(match m { Some(m) => json!({ "phase": "available", "version": m.version, "notes": m.notes }), None => json!({ "phase": "idle" }) });
            ExitCode::SUCCESS
        }),
        "--stage" if args.len() >= 2 => update::stage(&layout, &args[1]).map(|_| { emit(json!({ "phase": "ready", "version": args[1] })); ExitCode::SUCCESS }),
        "--activate" if args.len() >= 2 => activate(&layout, &args[1], arg_after(&args, "--wait-pid").and_then(|p| p.parse().ok())),
        "--healthy" if args.len() >= 2 => healthy(&layout, &args[1]),
        "--gc" => { layout.gc(); Ok(ExitCode::SUCCESS) }
        "--uninstall" => uninstall(&layout),
        _ if payload.is_some() => {
            let silent = args.iter().any(|a| a == "/S" || a == "--silent");
            return setup_ui(payload.unwrap(), layout, silent);
        }
        "--launch" => launch(&layout, &args[1..]),
        _ => launch(&layout, &args),
    };
    match result {
        Ok(code) => code,
        Err(e) => {
            log(&format!("{first} failed: {e}"));
            if matches!(first, "--check" | "--stage" | "--activate" | "--healthy" | "pack" | "bundle") {
                emit(json!({ "phase": "error", "error": e.code }));
            } else {
                message(&friendly(&e));
            }
            ExitCode::from(1)
        }
    }
}

fn arg_after<'a>(args: &'a [String], flag: &str) -> Option<&'a str> {
    args.iter().position(|a| a == flag).and_then(|i| args.get(i + 1)).map(String::as_str)
}

/// Texto para o utilizador; o detalhe técnico fica em install.log.
fn friendly(e: &Fail) -> String {
    let log_path = data().join("logs").join("install.log");
    let why = match e.code {
        "space" => "Não há espaço suficiente no disco.",
        "locked" => "Um ficheiro da Lunmi está em uso. Fecha a Lunmi e tenta outra vez.",
        "offline" => "Sem ligação à internet.",
        "signature" | "hash" | "source" => "A transferência não passou na verificação de segurança e foi recusada.",
        "extract" => "Um pacote da Lunmi está danificado.",
        "busy" => "A Lunmi ainda está aberta.",
        _ => "Ocorreu um erro inesperado.",
    };
    format!("{why}\n\nNada foi alterado na versão instalada. Detalhes em {}", log_path.display())
}

// ---------- Lançar, ativar, confirmar ----------

fn launch(layout: &Layout, args: &[String]) -> Res<ExitCode> {
    let Some(cur) = layout.read_current() else {
        return Err(Fail::new("internal", "a Lunmi não está instalada (falta current.json)"));
    };
    let (next, rolled_back) = on_launch(&cur);
    if rolled_back { log(&format!("rollback {} -> {} (não confirmou em 2 arranques)", cur.version, next.version)); }
    if !layout.app_exe(&next.version).is_file() {
        // A pasta da versão desapareceu (limpeza manual, antivírus): remonta-a a partir do store.
        layout.assemble(&next.version, &next.packs)?;
    }
    if next != cur { layout.write_current(&next)?; }
    let exe = layout.app_exe(&next.version);
    let launcher = std::env::current_exe().unwrap_or_default();
    Command::new(&exe).args(args).current_dir(layout.version_dir(&next.version))
        .env("LUNMI_LAUNCHER", &launcher)
        .spawn().map_err(|e| Fail::new("internal", format!("{}: {e}", exe.display())))?;
    Ok(ExitCode::SUCCESS)
}

fn wait_pid(pid: u32, ms: u32) -> bool {
    use windows_sys::Win32::Foundation::{CloseHandle, WAIT_OBJECT_0};
    use windows_sys::Win32::System::Threading::{OpenProcess, WaitForSingleObject};
    const SYNCHRONIZE: u32 = 0x0010_0000;
    let h = unsafe { OpenProcess(SYNCHRONIZE, 0, pid) };
    if h.is_null() { return true; } // já terminou
    let r = unsafe { WaitForSingleObject(h, ms) };
    unsafe { CloseHandle(h) };
    r == WAIT_OBJECT_0
}

fn activate(layout: &Layout, version: &str, wait: Option<u32>) -> Res<ExitCode> {
    let staged = layout.is_staged(version).ok_or_else(|| Fail::new("internal", format!("{version} não está preparada")))?;
    if let Some(pid) = wait {
        if !wait_pid(pid, 30_000) { return Err(Fail::new("busy", format!("o processo {pid} não terminou em 30 s"))); }
    }
    // O lançador novo vem no pacote app; o que está a correr muda de nome e é substituído.
    let fresh = layout.version_dir(version).join("Lunmi.exe");
    if fresh.is_file() {
        let len = fs::metadata(&fresh)?.len();
        if let Err(e) = setup::install_launcher(&fresh, len, &layout.root) { log(&format!("launcher not replaced: {e}")); }
    }
    let old = layout.read_current();
    layout.write_current(&activated(old.as_ref(), &staged))?;
    log(&format!("activated {version} (antes {:?})", old.map(|c| c.version)));
    launch(layout, &[])
}

fn healthy(layout: &Layout, version: &str) -> Res<ExitCode> {
    if let Some(mut cur) = layout.read_current() {
        if cur.version == version && !cur.healthy {
            cur.healthy = true;
            cur.attempts = 0;
            layout.write_current(&cur)?;
            log(&format!("healthy {version}"));
        }
    }
    layout.gc();
    let _ = fs::remove_file(layout.root.join("Lunmi.exe.old"));
    Ok(ExitCode::SUCCESS)
}

// ---------- Desinstalar ----------

fn ask(text: &str, default_no: bool) -> bool {
    use windows_sys::Win32::UI::WindowsAndMessaging::{MessageBoxW, IDYES, MB_DEFBUTTON2, MB_ICONQUESTION, MB_YESNO};
    let flags = MB_YESNO | MB_ICONQUESTION | if default_no { MB_DEFBUTTON2 } else { 0 };
    unsafe { MessageBoxW(std::ptr::null_mut(), wide(text).as_ptr(), wide("Lunmi").as_ptr(), flags) == IDYES }
}

fn uninstall(layout: &Layout) -> Res<ExitCode> {
    if !ask("Desinstalar a Lunmi deste computador?", false) { return Ok(ExitCode::SUCCESS); }
    let wipe = ask("Apagar também as conversas, a memória, as chaves e os modelos de voz?\n\nSe escolheres Não, ficam guardados para uma próxima instalação.", true);
    for dir in [layout.versions(), layout.store(), layout.staging()] {
        remove_tree(&dir).map_err(|e| Fail::new("locked", format!("{}: {e}", dir.display())))?;
    }
    setup::remove_shortcuts();
    setup::unregister();
    if wipe { let _ = remove_tree(&data()); }
    log(&format!("uninstalled (dados apagados: {wipe})"));
    // O próprio Lunmi.exe está em uso: um cmd escondido apaga a pasta depois de sairmos.
    use std::os::windows::process::CommandExt;
    let _ = Command::new("cmd.exe").raw_arg(format!("/c ping -n 3 127.0.0.1 >nul & rmdir /s /q \"{}\"", layout.root.display()))
        .creation_flags(0x0800_0000).spawn();
    Ok(ExitCode::SUCCESS)
}

// ---------- Janela do setup ----------

enum UserEvent { Ipc(String), Eval(String), Exit(u8) }

fn send(proxy: &EventLoopProxy<UserEvent>, msg: Value) {
    let _ = proxy.send_event(UserEvent::Eval(format!("window.lunmi && window.lunmi.update({msg})")));
}

fn work_dir() -> PathBuf { std::env::temp_dir().join(format!("lunmi-setup-{}", std::process::id())) }

struct WorkDirGuard;
impl Drop for WorkDirGuard {
    fn drop(&mut self) {
        for _ in 0..20 {
            if remove_tree(&work_dir()).is_ok() { return; }
            thread::sleep(Duration::from_millis(250));
        }
    }
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

fn style_window(hwnd: isize) {
    use windows_sys::Win32::Graphics::Dwm::{DwmSetWindowAttribute, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND};
    use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows_sys::Win32::UI::WindowsAndMessaging::{LoadIconW, SendMessageW, ICON_BIG, ICON_SMALL, WM_SETICON};
    let hwnd = hwnd as windows_sys::Win32::Foundation::HWND;
    unsafe {
        let pref = DWMWCP_ROUND;
        DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE as _, (&pref as *const i32).cast(), 4);
        let icon = LoadIconW(GetModuleHandleW(std::ptr::null()), 1 as _);
        if !icon.is_null() {
            SendMessageW(hwnd, WM_SETICON, ICON_BIG as usize, icon as isize);
            SendMessageW(hwnd, WM_SETICON, ICON_SMALL as usize, icon as isize);
        }
    }
}

fn run_install(payload: &setup::Payload, layout: &Layout, desktop: bool, start_menu: bool, proxy: Option<EventLoopProxy<UserEvent>>) -> Res<()> {
    let skipped: Vec<&str> = payload.entries.iter()
        .filter(|e| pack::is_complete(&layout.store(), &e.name, &e.key) && e.name != "base" && e.name != "app")
        .map(|e| setup::step(&e.name)).collect();
    if let Some(p) = &proxy { send(p, json!({ "type": "skipped", "steps": skipped })); }
    let progress: setup::Progress = match proxy.clone() {
        Some(p) => Arc::new(move |f: f64, step: &str| send(&p, json!({ "type": "progress", "percent": f * 100.0, "step": step }))),
        None => Arc::new(|_, _| {}),
    };
    let started = std::time::Instant::now();
    let r = setup::install(payload, layout, desktop, start_menu, progress);
    log(&format!("setup {} in {}s: {:?}", payload.manifest.version, started.elapsed().as_secs(), r.as_ref().err()));
    r
}

fn setup_ui(payload: setup::Payload, layout: Layout, silent: bool) -> ExitCode {
    let _work = WorkDirGuard;
    log(&format!("setup start version={} silent={silent}", payload.manifest.version));
    if silent {
        return match run_install(&payload, &layout, true, true, None) {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => { emit(json!({ "phase": "error", "error": e.code })); ExitCode::from(1) }
        };
    }
    let dir = layout.root.to_string_lossy().to_string();
    let mut event_loop = EventLoopBuilder::<UserEvent>::with_user_event().build();
    let proxy = event_loop.create_proxy();
    let window = WindowBuilder::new()
        .with_title("Instalador da Lunmi")
        .with_inner_size(tao::dpi::LogicalSize::new(860.0, 540.0))
        .with_resizable(false).with_decorations(false).with_undecorated_shadow(true).with_visible(false)
        .build(&event_loop).expect("janela");
    if let Some(m) = window.current_monitor() {
        let (ms, mp, ws) = (m.size(), m.position(), window.outer_size());
        window.set_outer_position(tao::dpi::PhysicalPosition::new(mp.x + (ms.width as i32 - ws.width as i32) / 2, mp.y + (ms.height as i32 - ws.height as i32) / 2));
    }
    style_window(window.hwnd());
    let init = json!({ "mode": "install", "fixed": true, "version": payload.manifest.version, "dir": dir,
                       "free": free_bytes(&layout.root), "need": setup::needed(&payload, &layout) });
    let ipc_proxy = proxy.clone();
    let _ = fs::create_dir_all(work_dir());
    let mut web_context = wry::WebContext::new(Some(work_dir().join("webview")));
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
            // Sem WebView2: instala sem janela e diz o resultado numa caixa simples.
            log(&format!("webview unavailable: {e}"));
            drop(window);
            return match run_install(&payload, &layout, true, true, None) {
                Ok(()) => { let _ = launch(&layout, &[]); ExitCode::SUCCESS }
                Err(e) => { message(&friendly(&e)); ExitCode::from(1) }
            };
        }
    };
    let payload = Arc::new(payload);
    let layout = Arc::new(layout);
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
                    "ready" => { window.set_visible(true); window.set_focus(); }
                    "drag" => { let _ = window.drag_window(); }
                    "minimize" => window.set_minimized(true),
                    "close" if !busy => *flow = ControlFlow::Exit,
                    "install" if !busy => {
                        busy = true;
                        let (pl, la, p) = (payload.clone(), layout.clone(), proxy.clone());
                        let (desk, start) = (msg["desktop"].as_bool().unwrap_or(true), msg["startMenu"].as_bool().unwrap_or(true));
                        thread::spawn(move || match run_install(&pl, &la, desk, start, Some(p.clone())) {
                            Ok(()) => send(&p, json!({ "type": "done", "dir": la.root.to_string_lossy() })),
                            Err(e) => send(&p, json!({ "type": "error", "code": 1, "text": friendly(&e) })),
                        });
                    }
                    "finished" => {
                        busy = false;
                        if msg["open"].as_bool() == Some(true) {
                            let _ = Command::new(layout.root.join("Lunmi.exe")).current_dir(&layout.root).spawn();
                        }
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
    log(&format!("setup exit code={exit_code}"));
    ExitCode::from(exit_code)
}

fn message(text: &str) {
    use windows_sys::Win32::UI::WindowsAndMessaging::{MessageBoxW, MB_ICONERROR, MB_OK};
    unsafe { MessageBoxW(std::ptr::null_mut(), wide(text).as_ptr(), wide("Lunmi").as_ptr(), MB_OK | MB_ICONERROR) };
}

