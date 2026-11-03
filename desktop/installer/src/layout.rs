//! `current.json`, montagem de `versions\<v>` a partir do `store` e limpeza do que já não é usado.
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::common::{log, remove_tree, rename_with_retry, replace_file, Fail, Res};
use crate::pack::{is_complete, COMPLETE, NAMES};

pub type Packs = BTreeMap<String, String>;

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Previous { pub version: String, pub packs: Packs }

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Current {
    pub schema: u32,
    pub version: String,
    pub packs: Packs,
    pub healthy: bool,
    pub attempts: u32,
    pub previous: Option<Previous>,
}

/// Escrito em `versions\<v>\lunmi-version.json` quando a versão fica montada.
#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Staged { pub version: String, pub packs: Packs }

pub struct Layout { pub root: PathBuf }

impl Layout {
    pub fn new(root: PathBuf) -> Self { Layout { root } }
    pub fn store(&self) -> PathBuf { self.root.join("store") }
    pub fn staging(&self) -> PathBuf { self.root.join("staging") }
    pub fn versions(&self) -> PathBuf { self.root.join("versions") }
    pub fn version_dir(&self, v: &str) -> PathBuf { self.versions().join(v) }
    pub fn app_exe(&self, v: &str) -> PathBuf { self.version_dir(v).join("Lunmi-app.exe") }
    fn current_path(&self) -> PathBuf { self.root.join("current.json") }

    pub fn read_current(&self) -> Option<Current> {
        let raw = fs::read(self.current_path()).ok()?;
        serde_json::from_slice::<Current>(&raw).ok().filter(|c| c.schema == 1)
    }

    /// Escreve `current.json.tmp` e troca-o de uma vez: nunca fica meio escrito.
    pub fn write_current(&self, c: &Current) -> Res<()> {
        fs::create_dir_all(&self.root)?;
        let tmp = self.root.join("current.json.tmp");
        fs::write(&tmp, serde_json::to_vec_pretty(c).unwrap())?;
        replace_file(&tmp, &self.current_path()).map_err(|e| Fail::new("locked", format!("current.json: {e}")))
    }

    pub fn is_staged(&self, v: &str) -> Option<Staged> {
        let dir = self.version_dir(v);
        if !dir.join(COMPLETE).is_file() { return None; }
        serde_json::from_slice::<Staged>(&fs::read(dir.join("lunmi-version.json")).ok()?).ok().filter(|s| s.version == v)
    }

    /// `versions\<v>` = base + app (os de app sobrepõem-se), por hardlink; cópia se o hardlink falhar.
    pub fn assemble(&self, version: &str, packs: &Packs) -> Res<PathBuf> {
        let target = self.version_dir(version);
        if self.is_staged(version).is_some_and(|s| &s.packs == packs) { return Ok(target); }
        for name in NAMES {
            let key = packs.get(name).ok_or_else(|| Fail::new("internal", format!("pacote {name} em falta")))?;
            if !is_complete(&self.store(), name, key) { return Err(Fail::new("internal", format!("{name}-{key} não está no store"))); }
        }
        let partial = self.versions().join(format!("{version}.partial"));
        remove_tree(&partial)?;
        fs::create_dir_all(&partial)?;
        for name in ["base", "app"] {
            let src = self.store().join(format!("{name}-{}", packs[name]));
            link_tree(&src, &src, &partial)?;
        }
        fs::write(partial.join("lunmi-version.json"), serde_json::to_vec(&Staged { version: version.into(), packs: packs.clone() }).unwrap())?;
        fs::write(partial.join(COMPLETE), version)?;
        remove_tree(&target)?;
        rename_with_retry(&partial, &target, 60).map_err(|e| Fail::new("locked", format!("versions\\{version}: {e}")))?;
        Ok(target)
    }

    /// Apaga versões e pacotes que nem a atual, nem a anterior, nem uma versão preparada mais nova
    /// (à espera de reinício) usam. Chamar só com o `InstallLock`. Em uso → fica para a próxima.
    pub fn gc(&self) {
        let Some(cur) = self.read_current() else { return };
        let mut keep_versions = BTreeSet::from([cur.version.clone()]);
        let mut keep_packs: BTreeSet<String> = cur.packs.iter().map(|(n, k)| format!("{n}-{k}")).collect();
        if let Some(p) = &cur.previous {
            keep_versions.insert(p.version.clone());
            keep_packs.extend(p.packs.iter().map(|(n, k)| format!("{n}-{k}")));
        }
        for staged in self.staged_newer_than(&cur.version) {
            keep_versions.insert(staged.version.clone());
            keep_packs.extend(staged.packs.iter().map(|(n, k)| format!("{n}-{k}")));
        }
        for (dir, keep) in [(self.versions(), &keep_versions), (self.store(), &keep_packs)] {
            let Ok(entries) = fs::read_dir(&dir) else { continue };
            for e in entries.flatten() {
                let name = e.file_name().to_string_lossy().to_string();
                if keep.contains(&name) { continue; }
                if let Err(err) = remove_tree(&e.path()) { log(&format!("gc kept {}: {err}", e.path().display())); }
            }
        }
        let _ = remove_tree(&self.staging());
    }

    /// Versões montadas e completas mais novas do que `current` (prontas para `--activate`).
    pub fn staged_newer_than(&self, current: &str) -> Vec<Staged> {
        let Ok(entries) = fs::read_dir(self.versions()) else { return vec![] };
        entries.flatten()
            .filter_map(|e| self.is_staged(&e.file_name().to_string_lossy()))
            .filter(|s| crate::update::newer(&s.version, current))
            .collect()
    }
}

/// Exclusão entre processos do lançador (`stage`, `activate`, setup, `gc`): `ROOT\install.lock`
/// aberto sem partilha. O Windows liberta-o se o processo morrer.
pub struct InstallLock(#[allow(dead_code)] fs::File);

impl InstallLock {
    /// Espera até `wait_ms`; `None` se outro processo o mantiver.
    pub fn acquire(root: &Path, wait_ms: u64) -> Option<InstallLock> {
        use std::os::windows::fs::OpenOptionsExt;
        let _ = fs::create_dir_all(root);
        let deadline = std::time::Instant::now() + std::time::Duration::from_millis(wait_ms);
        loop {
            match fs::OpenOptions::new().read(true).write(true).create(true).truncate(false).share_mode(0).open(root.join("install.lock")) {
                Ok(f) => return Some(InstallLock(f)),
                Err(_) if std::time::Instant::now() < deadline => std::thread::sleep(std::time::Duration::from_millis(200)),
                Err(_) => return None,
            }
        }
    }
}

fn link_tree(base: &Path, dir: &Path, out: &Path) -> io::Result<()> {
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        let path = entry.path();
        let rel = path.strip_prefix(base).unwrap();
        if rel.as_os_str() == COMPLETE { continue; }
        if entry.file_type()?.is_dir() { fs::create_dir_all(out.join(rel))?; link_tree(base, &path, out)?; continue; }
        let to = out.join(rel);
        if to.exists() { fs::remove_file(&to)?; }
        if fs::hard_link(&path, &to).is_err() { fs::copy(&path, &to)?; }
    }
    Ok(())
}

/// Decisão do lançador para uma versão ainda não confirmada: devolve o estado a gravar e se
/// houve regresso à anterior. Puro, para testar sem processos.
pub fn on_launch(cur: &Current) -> (Current, bool) {
    if cur.healthy { return (cur.clone(), false); }
    if cur.attempts >= 2 {
        if let Some(prev) = &cur.previous {
            return (Current { schema: 1, version: prev.version.clone(), packs: prev.packs.clone(), healthy: true, attempts: 0, previous: None }, true);
        }
    }
    (Current { attempts: cur.attempts + 1, ..cur.clone() }, false)
}

/// Nova versão ativa; a que estava passa a `previous` (só se tiver ficado confirmada).
pub fn activated(old: Option<&Current>, staged: &Staged) -> Current {
    let previous = old.filter(|c| c.healthy && c.version != staged.version)
        .map(|c| Previous { version: c.version.clone(), packs: c.packs.clone() })
        .or_else(|| old.and_then(|c| c.previous.clone()));
    Current { schema: 1, version: staged.version.clone(), packs: staged.packs.clone(), healthy: false, attempts: 0, previous }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pack::tests::{scratch, tree};
    use crate::pack::{install_pack, pack};

    fn packs(keys: &[(&str, &str)]) -> Packs { keys.iter().map(|(n, k)| (n.to_string(), k.to_string())).collect() }

    #[test]
    fn current_is_written_whole_and_read_back() {
        let d = scratch("current");
        let l = Layout::new(d.clone());
        let c = Current { schema: 1, version: "1.0.0-beta.1".into(), packs: packs(&[("app", "a")]), healthy: false, attempts: 0, previous: None };
        l.write_current(&c).unwrap();
        assert_eq!(l.read_current().unwrap(), c);
        assert!(!d.join("current.json.tmp").exists());
    }

    #[test]
    fn unconfirmed_version_rolls_back_on_the_third_launch() {
        let prev = Previous { version: "1.0.0-beta.1".into(), packs: packs(&[("app", "old")]) };
        let mut c = Current { schema: 1, version: "1.0.0-beta.2".into(), packs: packs(&[("app", "new")]), healthy: false, attempts: 0, previous: Some(prev.clone()) };
        for expected in 1..=2 { let (n, back) = on_launch(&c); assert!(!back); assert_eq!(n.attempts, expected); c = n; }
        let (n, back) = on_launch(&c);
        assert!(back && n.version == prev.version && n.healthy);
        // Sem anterior não há para onde voltar: continua a tentar.
        let solo = Current { previous: None, ..c };
        assert!(!on_launch(&solo).1);
    }

    #[test]
    fn activation_keeps_only_a_confirmed_previous() {
        let staged = Staged { version: "2".into(), packs: packs(&[("app", "b")]) };
        let good = Current { schema: 1, version: "1".into(), packs: packs(&[("app", "a")]), healthy: true, attempts: 0, previous: None };
        assert_eq!(activated(Some(&good), &staged).previous.unwrap().version, "1");
        let bad = Current { healthy: false, previous: Some(Previous { version: "0".into(), packs: Packs::new() }), ..good };
        assert_eq!(activated(Some(&bad), &staged).previous.unwrap().version, "0");
    }

    #[test]
    fn assemble_links_base_and_app_and_gc_removes_the_rest() {
        let d = scratch("assemble");
        let l = Layout::new(d.join("root"));
        let mut keys = Packs::new();
        for (name, files) in [("base", vec![("Lunmi-app.exe", &b"electron"[..]), ("resources/x.pak", b"pak")]),
                              ("app", vec![("resources/app.asar", &b"asar"[..])]),
                              ("python", vec![("python.exe", &b"py"[..])]), ("voice", vec![("runtime/python.exe", &b"v"[..])])] {
            let src = d.join("src").join(name);
            tree(&src, &files);
            let info = pack(&src, name, &d.join("out")).unwrap();
            let f = fs::File::open(d.join("out").join(format!("{name}-{}.tar.zst", info.key))).unwrap();
            install_pack(f, info.size, &info.sha256, name, &info.key, &l.store(), &l.staging(), Box::new(|_| {})).unwrap();
            keys.insert(name.into(), info.key);
        }
        let v = l.assemble("1.0.0-beta.1", &keys).unwrap();
        assert_eq!(fs::read(v.join("resources/app.asar")).unwrap(), b"asar");
        assert_eq!(fs::read(v.join("Lunmi-app.exe")).unwrap(), b"electron");
        assert!(l.is_staged("1.0.0-beta.1").is_some());
        fs::create_dir_all(l.store().join("app-stale")).unwrap();
        fs::create_dir_all(l.version_dir("0.9.0")).unwrap();
        l.write_current(&activated(None, &l.is_staged("1.0.0-beta.1").unwrap())).unwrap();
        l.gc();
        assert!(!l.store().join("app-stale").exists() && !l.version_dir("0.9.0").exists());
        assert!(l.app_exe("1.0.0-beta.1").exists());

        // Uma versão preparada e à espera de reinício sobrevive ao gc de um --healthy tardio.
        let src = d.join("src").join("app2");
        tree(&src, &[("resources/app.asar", b"asar2")]);
        let info = pack(&src, "app", &d.join("out")).unwrap();
        let f = fs::File::open(d.join("out").join(format!("app-{}.tar.zst", info.key))).unwrap();
        install_pack(f, info.size, &info.sha256, "app", &info.key, &l.store(), &l.staging(), Box::new(|_| {})).unwrap();
        let mut next = keys.clone();
        next.insert("app".into(), info.key.clone());
        l.assemble("1.0.0-beta.2", &next).unwrap();
        l.gc();
        assert!(l.is_staged("1.0.0-beta.2").is_some(), "a versão pronta não pode desaparecer");
        assert!(l.store().join(format!("app-{}", info.key)).exists());
    }

    #[test]
    fn the_install_lock_is_exclusive_between_holders() {
        let d = scratch("lock");
        let held = InstallLock::acquire(&d, 0).unwrap();
        assert!(InstallLock::acquire(&d, 300).is_none());
        drop(held);
        assert!(InstallLock::acquire(&d, 0).is_some());
    }
}
