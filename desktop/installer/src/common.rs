//! Erros com código estável, registo em `DATA\logs\install.log` e caminhos (docs/UPDATE-SYSTEM.md).
use std::fmt;
use std::fs;
use std::io::{self, Write};
use std::os::windows::ffi::OsStrExt;
use std::path::{Path, PathBuf};

/// Códigos do contrato; `detail` só vai para o log, nunca para a app.
#[derive(Debug)]
pub struct Fail { pub code: &'static str, pub detail: String }

impl Fail {
    pub fn new(code: &'static str, detail: impl Into<String>) -> Self { Fail { code, detail: detail.into() } }
}
impl fmt::Display for Fail {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { write!(f, "{}: {}", self.code, self.detail) }
}
impl From<io::Error> for Fail {
    fn from(e: io::Error) -> Self {
        let code = match e.raw_os_error() {
            Some(112) | Some(39) => "space",           // ERROR_DISK_FULL, ERROR_HANDLE_DISK_FULL
            Some(5) | Some(32) | Some(33) => "locked",  // acesso negado, partilha, bloqueio
            _ if e.kind() == io::ErrorKind::StorageFull => "space",
            _ => "internal",
        };
        Fail::new(code, e.to_string())
    }
}
pub type Res<T> = Result<T, Fail>;

pub fn wide(s: &str) -> Vec<u16> { std::ffi::OsStr::new(s).encode_wide().chain(Some(0)).collect() }
pub fn wide_path(p: &Path) -> Vec<u16> { p.as_os_str().encode_wide().chain(Some(0)).collect() }

fn local_app_data() -> PathBuf { PathBuf::from(std::env::var_os("LOCALAPPDATA").unwrap_or_default()) }

/// Programa. `LUNMI_ROOT` só existe para testes e para o CI montar árvores fora da instalação pessoal.
pub fn root() -> PathBuf {
    std::env::var_os("LUNMI_ROOT").map(PathBuf::from).unwrap_or_else(|| local_app_data().join("Programs").join("Lunmi"))
}

/// Dados do utilizador (= LUNMI_USER_DATA da app); o instalador só escreve aqui o log.
pub fn data() -> PathBuf {
    std::env::var_os("LUNMI_DATA").map(PathBuf::from).unwrap_or_else(|| local_app_data().join("Lunmi"))
}

pub fn log(line: &str) {
    let dir = data().join("logs");
    let path = dir.join("install.log");
    let _ = fs::create_dir_all(&dir);
    if fs::metadata(&path).map(|m| m.len() > 512 * 1024).unwrap_or(false) { let _ = fs::rename(&path, dir.join("install.log.1")); }
    let secs = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(&path) {
        let _ = writeln!(f, "{secs} pid={} {line}", std::process::id());
    }
}

/// Uma linha JSON no stdout (protocolo com a app e com o CI).
pub fn emit(value: serde_json::Value) {
    let mut out = io::stdout().lock();
    let _ = writeln!(out, "{value}");
    let _ = out.flush();
}

/// Espaço livre no volume do caminho (sobe até à primeira pasta que exista).
pub fn free_bytes(path: &Path) -> Option<u64> {
    let mut p = path.to_path_buf();
    while !p.exists() { if !p.pop() { return None; } }
    let mut free = 0u64;
    let ok = unsafe { windows_sys::Win32::Storage::FileSystem::GetDiskFreeSpaceExW(wide_path(&p).as_ptr(), &mut free, std::ptr::null_mut(), std::ptr::null_mut()) };
    (ok != 0).then_some(free)
}

/// `MoveFileExW(REPLACE_EXISTING | WRITE_THROUGH)`: troca atómica de um ficheiro.
pub fn replace_file(from: &Path, to: &Path) -> io::Result<()> {
    use windows_sys::Win32::Storage::FileSystem::{MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH};
    let ok = unsafe { MoveFileExW(wide_path(from).as_ptr(), wide_path(to).as_ptr(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) };
    if ok == 0 { Err(io::Error::last_os_error()) } else { Ok(()) }
}

/// Renomeia uma pasta acabada de escrever. O indexador e o antivírus seguram ficheiros novos por
/// instantes e uma pasta com um ficheiro aberto não muda de nome: tenta durante até ~60 s.
pub fn rename_with_retry(from: &Path, to: &Path, attempts: u32) -> io::Result<()> {
    let mut last = None;
    for i in 0..attempts.max(1) {
        match fs::rename(from, to) {
            Ok(()) => return Ok(()),
            Err(e) => { last = Some(e); if i + 1 < attempts { std::thread::sleep(std::time::Duration::from_millis(1000)); } }
        }
    }
    Err(last.unwrap())
}

/// Apaga uma árvore se existir (sem seguir junctions: `remove_dir_all` não as segue).
pub fn remove_tree(path: &Path) -> io::Result<()> {
    match fs::symlink_metadata(path) {
        Err(_) => Ok(()),
        Ok(m) if m.is_dir() => fs::remove_dir_all(path),
        Ok(_) => fs::remove_file(path),
    }
}
