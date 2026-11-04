//! Setup offline: `[exe][pacotes][manifest.json][índice][trailer]`, comando `bundle` do CI,
//! instalação a partir do payload (mesma `install_pack` da atualização), atalhos e registo.
use std::fs;
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::process::Command;

use serde::{Deserialize, Serialize};

use crate::common::{free_bytes, log, replace_file, wide, Fail, Res};
use crate::layout::{activated, Layout, Staged};
use crate::pack::{install_pack, is_complete, sha256_file};
use crate::update::Manifest;

const MAGIC: &[u8; 8] = b"LUNMIPK2";
const TRAILER: u64 = 32; // exe_len, manifest_len, index_len (u64 LE) + MAGIC

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Entry { pub name: String, pub key: String, pub offset: u64, pub size: u64, pub sha256: String, pub unpacked: u64 }

pub struct Payload { pub exe: PathBuf, pub exe_len: u64, pub manifest: Manifest, pub entries: Vec<Entry> }

/// O payload do próprio executável, se for um setup.
pub fn read_payload(exe: &Path) -> Option<Payload> {
    let mut f = fs::File::open(exe).ok()?;
    let len = f.metadata().ok()?.len();
    if len < TRAILER { return None; }
    let mut t = [0u8; TRAILER as usize];
    f.seek(SeekFrom::End(-(TRAILER as i64))).ok()?;
    f.read_exact(&mut t).ok()?;
    if &t[24..] != MAGIC { return None; }
    let n = |i: usize| u64::from_le_bytes(t[i..i + 8].try_into().unwrap());
    let (exe_len, manifest_len, index_len) = (n(0), n(8), n(16));
    let index_at = len.checked_sub(TRAILER + index_len)?;
    let manifest_at = index_at.checked_sub(manifest_len)?;
    let mut read = |at: u64, size: u64| -> Option<Vec<u8>> {
        let mut b = vec![0u8; usize::try_from(size).ok().filter(|s| *s <= 1 << 20)?];
        f.seek(SeekFrom::Start(at)).ok()?;
        f.read_exact(&mut b).ok()?;
        Some(b)
    };
    let manifest: Manifest = serde_json::from_slice(&read(manifest_at, manifest_len)?).ok()?;
    let entries: Vec<Entry> = serde_json::from_slice(&read(index_at, index_len)?).ok()?;
    manifest.validate(false).ok()?;
    let ends_ok = entries.iter().all(|e| e.offset >= exe_len && e.offset + e.size <= manifest_at);
    (ends_ok && entries.len() == manifest.packs.len()).then(|| Payload { exe: exe.to_path_buf(), exe_len, manifest, entries })
}

/// CI: `Lunmi.exe bundle <manifesto-sem-setup.json> <pasta-com-os-.tar.zst> <saída>`.
pub fn bundle(manifest_path: &Path, packs_dir: &Path, out: &Path) -> Res<()> {
    let raw = fs::read(manifest_path)?;
    let manifest: Manifest = serde_json::from_slice(&raw).map_err(|e| Fail::new("internal", e.to_string()))?;
    manifest.validate(true)?;
    let me = std::env::current_exe()?;
    let exe_len = read_payload(&me).map(|p| p.exe_len).unwrap_or(fs::metadata(&me)?.len());
    let tmp = out.with_extension("tmp");
    let mut w = io::BufWriter::with_capacity(1 << 20, fs::File::create(&tmp)?);
    io::copy(&mut fs::File::open(&me)?.take(exe_len), &mut w)?;
    let mut offset = exe_len;
    let mut entries = Vec::new();
    for p in &manifest.packs {
        let file = packs_dir.join(format!("{}-{}.tar.zst", p.name, p.key));
        let size = fs::metadata(&file).map_err(|e| Fail::new("internal", format!("{}: {e}", file.display())))?.len();
        let sha = sha256_file(&file)?;
        if size != p.size || sha != p.sha256 { return Err(Fail::new("hash", format!("{} não corresponde ao manifesto", file.display()))); }
        io::copy(&mut fs::File::open(&file)?, &mut w)?;
        entries.push(Entry { name: p.name.clone(), key: p.key.clone(), offset, size, sha256: sha, unpacked: p.unpacked });
        offset += size;
    }
    let index = serde_json::to_vec(&entries).unwrap();
    w.write_all(&raw)?;
    w.write_all(&index)?;
    for v in [exe_len, raw.len() as u64, index.len() as u64] { w.write_all(&v.to_le_bytes())?; }
    w.write_all(MAGIC)?;
    w.into_inner().map_err(|e| Fail::new("internal", e.to_string()))?.sync_all()?;
    fs::rename(&tmp, out)?;
    Ok(())
}

/// Bytes que a instalação ainda precisa no disco (pacotes por extrair e uma margem).
pub fn needed(payload: &Payload, layout: &Layout) -> u64 {
    payload.entries.iter().filter(|e| !is_complete(&layout.store(), &e.name, &e.key)).map(|e| e.unpacked).sum::<u64>() + (256 << 20)
}

/// Passo da UI para cada pacote (base e app contam como "app").
pub fn step(name: &str) -> &'static str { match name { "python" => "python", "voice" => "voice", _ => "app" } }

/// Instala tudo o que o payload traz, copia o lançador para `ROOT\Lunmi.exe` e ativa a versão.
/// `progress(fração, passo)`.
pub type Progress = std::sync::Arc<dyn Fn(f64, &str) + Send + Sync>;

pub fn install(payload: &Payload, layout: &Layout, desktop: bool, start_menu: bool, progress: Progress) -> Res<()> {
    let need = needed(payload, layout);
    if let Some(free) = free_bytes(&layout.root) {
        if free < need { return Err(Fail::new("space", format!("precisa de {} MB, há {} MB", need >> 20, free >> 20))); }
    }
    let total: u64 = payload.entries.iter().map(|e| e.size).sum::<u64>().max(1);
    let mut before = 0u64;
    for e in &payload.entries {
        let name = step(&e.name);
        progress(before as f64 / total as f64 * 0.95, name);
        if !is_complete(&layout.store(), &e.name, &e.key) {
            let mut f = fs::File::open(&payload.exe)?;
            f.seek(SeekFrom::Start(e.offset))?;
            let (p, base) = (progress.clone(), before);
            let tick = move |read: u64| p((base + read) as f64 / total as f64 * 0.95, name);
            install_pack(f, e.size, &e.sha256, &e.name, &e.key, &layout.store(), &layout.staging(), Box::new(tick))?;
        }
        before += e.size;
    }
    progress(0.96, "app");
    let version = payload.manifest.version.clone();
    layout.assemble(&version, &payload.manifest.keys())?;
    install_launcher(&payload.exe, payload.exe_len, &layout.root)?;
    let staged = Staged { version: version.clone(), packs: payload.manifest.keys() };
    let old = layout.read_current();
    let mut current = activated(old.as_ref(), &staged);
    // Reinstalar a mesma versão não deixa a instalação à espera de confirmação.
    if old.as_ref().is_some_and(|c| c.version == version && c.healthy) { current.healthy = true; current.previous = old.and_then(|c| c.previous); }
    layout.write_current(&current)?;
    progress(0.98, "shortcuts");
    // Com LUNMI_ROOT (testes, CI) a árvore é descartável: sem atalhos nem entrada no registo.
    if std::env::var_os("LUNMI_ROOT").is_none() {
        let launcher = layout.root.join("Lunmi.exe");
        shortcuts(&launcher, desktop, start_menu);
        register(&layout.root, &version, need);
    }
    log(&format!("installed {version} at {}", layout.root.display()));
    Ok(())
}

/// Copia só a parte executável (sem payload). Um lançador em uso muda de nome primeiro.
pub fn install_launcher(src: &Path, len: u64, root: &Path) -> Res<()> {
    fs::create_dir_all(root)?;
    let target = root.join("Lunmi.exe");
    let tmp = root.join("Lunmi.exe.new");
    io::copy(&mut fs::File::open(src)?.take(len), &mut fs::File::create(&tmp)?)?;
    if replace_file(&tmp, &target).is_err() {
        let old = root.join("Lunmi.exe.old");
        let _ = fs::remove_file(&old);
        fs::rename(&target, &old).map_err(|e| Fail::new("locked", format!("Lunmi.exe: {e}")))?;
        replace_file(&tmp, &target).map_err(|e| Fail::new("locked", format!("Lunmi.exe: {e}")))?;
    }
    Ok(())
}

fn powershell(script: &str) -> bool {
    use std::os::windows::process::CommandExt;
    Command::new("powershell.exe").args(["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script])
        .creation_flags(0x0800_0000).status().map(|s| s.success()).unwrap_or(false)
}

fn ps_quote(s: &str) -> String { format!("'{}'", s.replace('\'', "''")) }

pub fn shortcut_paths() -> Vec<(bool, String)> {
    vec![(true, "[Environment]::GetFolderPath('Desktop')".into()), (false, "[Environment]::GetFolderPath('Programs')".into())]
}

fn shortcuts(launcher: &Path, desktop: bool, start_menu: bool) {
    for (is_desktop, folder) in shortcut_paths() {
        if (is_desktop && !desktop) || (!is_desktop && !start_menu) { continue; }
        let target = ps_quote(&launcher.to_string_lossy());
        let ok = powershell(&format!(
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path ({folder}) 'Lunmi.lnk')); $s.TargetPath={target}; \
             $s.WorkingDirectory=(Split-Path {target}); $s.IconLocation={target}+',0'; $s.Description='Lunmi'; $s.Save()"));
        if !ok { log(&format!("shortcut failed desktop={is_desktop}")); }
    }
}

pub fn remove_shortcuts() {
    for (_, folder) in shortcut_paths() {
        powershell(&format!("Remove-Item -LiteralPath (Join-Path ({folder}) 'Lunmi.lnk') -ErrorAction SilentlyContinue"));
    }
}

pub const UNINSTALL_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Lunmi";

/// Entrada em "Aplicações instaladas" (por utilizador, sem administrador).
fn register(root: &Path, version: &str, bytes: u64) {
    use windows_sys::Win32::System::Registry::*;
    let mut key: HKEY = std::ptr::null_mut();
    let rc = unsafe { RegCreateKeyExW(HKEY_CURRENT_USER, wide(UNINSTALL_KEY).as_ptr(), 0, std::ptr::null(), 0, KEY_WRITE, std::ptr::null(), &mut key, std::ptr::null_mut()) };
    if rc != 0 { log(&format!("register failed rc={rc}")); return; }
    let launcher = root.join("Lunmi.exe").to_string_lossy().to_string();
    let set = |name: &str, value: &str| unsafe {
        let v = wide(value);
        RegSetValueExW(key, wide(name).as_ptr(), 0, REG_SZ, v.as_ptr().cast(), (v.len() * 2) as u32);
    };
    let dword = |name: &str, value: u32| unsafe { RegSetValueExW(key, wide(name).as_ptr(), 0, REG_DWORD, (&value as *const u32).cast(), 4); };
    set("DisplayName", "Lunmi");
    set("DisplayVersion", version);
    set("Publisher", "Lunmi");
    set("InstallLocation", &root.to_string_lossy());
    set("DisplayIcon", &launcher);
    set("UninstallString", &format!("\"{launcher}\" --uninstall"));
    dword("NoModify", 1);
    dword("NoRepair", 1);
    dword("EstimatedSize", u32::try_from(bytes >> 10).unwrap_or(u32::MAX));
    unsafe { RegCloseKey(key) };
}

/// Mantém "Aplicações instaladas" certo depois de ativar ou de voltar atrás.
pub fn record_version(version: &str) {
    use windows_sys::Win32::System::Registry::*;
    if std::env::var_os("LUNMI_ROOT").is_some() { return; }
    let v = wide(version);
    let rc = unsafe { RegSetKeyValueW(HKEY_CURRENT_USER, wide(UNINSTALL_KEY).as_ptr(), wide("DisplayVersion").as_ptr(), REG_SZ, v.as_ptr().cast(), (v.len() * 2) as u32) };
    if rc != 0 { log(&format!("version not recorded rc={rc}")); }
}

pub fn unregister() {
    use windows_sys::Win32::System::Registry::{RegDeleteKeyW, HKEY_CURRENT_USER};
    unsafe { RegDeleteKeyW(HKEY_CURRENT_USER, wide(UNINSTALL_KEY).as_ptr()) };
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pack::pack;
    use crate::pack::tests::{scratch, tree};

    #[test]
    fn a_bundled_payload_reads_back_and_installs_into_a_fresh_root() {
        let d = scratch("bundle");
        let mut packs = vec![];
        for (name, file) in [("base", "Lunmi-app.exe"), ("app", "resources/app.asar"), ("python", "python.exe"), ("voice", "runtime/python.exe")] {
            let src = d.join("src").join(name);
            tree(&src, &[(file, name.as_bytes())]);
            let i = pack(&src, name, &d.join("packs")).unwrap();
            packs.push(serde_json::json!({ "name": i.name, "key": i.key, "size": i.size, "unpacked": i.unpacked, "sha256": i.sha256,
                "url": format!("https://github.com/MateusCosta888/Lunmi/releases/download/packs/{}-{}.tar.zst", i.name, i.key) }));
        }
        let manifest = serde_json::json!({ "schema": 1, "version": "1.0.0-beta.1", "notes": "", "packs": packs });
        fs::write(d.join("manifest.json"), serde_json::to_vec(&manifest).unwrap()).unwrap();
        // O executável de teste faz de lançador: o payload vai a seguir aos seus bytes.
        let out = d.join("Setup.exe");
        bundle(&d.join("manifest.json"), &d.join("packs"), &out).unwrap();
        let payload = read_payload(&out).expect("payload");
        assert_eq!(payload.exe_len, fs::metadata(std::env::current_exe().unwrap()).unwrap().len());
        assert_eq!(payload.entries.len(), 4);
        let layout = Layout::new(d.join("root"));
        let mut entries = payload.entries.clone();
        entries.sort_by(|a, b| a.name.cmp(&b.name));
        for e in &entries {
            let mut f = fs::File::open(&out).unwrap();
            f.seek(SeekFrom::Start(e.offset)).unwrap();
            install_pack(f, e.size, &e.sha256, &e.name, &e.key, &layout.store(), &layout.staging(), Box::new(|_| {})).unwrap();
        }
        let v = layout.assemble("1.0.0-beta.1", &payload.manifest.keys()).unwrap();
        assert_eq!(fs::read(v.join("Lunmi-app.exe")).unwrap(), b"base");
        install_launcher(&out, payload.exe_len, &layout.root).unwrap();
        assert_eq!(fs::metadata(layout.root.join("Lunmi.exe")).unwrap().len(), payload.exe_len, "o lançador instalado não leva o payload");
        assert!(read_payload(&layout.root.join("Lunmi.exe")).is_none());
    }

    #[test]
    fn a_plain_executable_has_no_payload() {
        assert!(read_payload(&std::env::current_exe().unwrap()).is_none());
    }
}
