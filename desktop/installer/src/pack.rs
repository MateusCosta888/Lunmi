//! Pacotes `<nome>-<chave>.tar.zst` e a rotina única que os instala em `store\` (setup e atualização).
use std::fs;
use std::io::{self, BufReader, Read, Write};
use std::path::{Component, Path, PathBuf};

use serde::Serialize;
use sha2::{Digest, Sha256};

use crate::common::{log, remove_tree, rename_with_retry, Fail, Res};

pub const NAMES: [&str; 4] = ["base", "app", "python", "voice"];
pub const COMPLETE: &str = ".complete";
const RENAME_ATTEMPTS: u32 = 60;

/// Ficheiros regulares relativos (POSIX, ordenados por bytes UTF-8), sem o `.complete` da raiz.
fn files(dir: &Path) -> io::Result<Vec<(String, PathBuf)>> {
    fn walk(base: &Path, dir: &Path, out: &mut Vec<(String, PathBuf)>) -> io::Result<()> {
        for entry in fs::read_dir(dir)? {
            let entry = entry?;
            let kind = entry.file_type()?;
            let path = entry.path();
            if kind.is_symlink() { return Err(io::Error::other(format!("ligação não permitida num pacote: {}", path.display()))); }
            if kind.is_dir() { walk(base, &path, out)?; continue; }
            let rel = path.strip_prefix(base).unwrap().to_string_lossy().replace('\\', "/");
            if rel == COMPLETE { continue; }
            out.push((rel, path));
        }
        Ok(())
    }
    let mut out = Vec::new();
    walk(dir, dir, &mut out)?;
    out.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    Ok(out)
}

pub fn sha256_file(path: &Path) -> io::Result<String> {
    let mut hash = Sha256::new();
    let mut f = BufReader::with_capacity(1 << 20, fs::File::open(path)?);
    io::copy(&mut f, &mut hash)?;
    Ok(hex::encode(hash.finalize()))
}

/// Chave do contrato: 16 hex do SHA-256 de `<caminho>\0<tamanho>\0<sha256>\n` por ficheiro.
pub fn content_key(dir: &Path) -> io::Result<(String, u64)> {
    let mut total = Sha256::new();
    let mut bytes = 0u64;
    for (rel, path) in files(dir)? {
        let size = fs::metadata(&path)?.len();
        bytes += size;
        total.update(format!("{rel}\0{size}\0{}\n", sha256_file(&path)?).as_bytes());
    }
    Ok((hex::encode(total.finalize())[..16].to_string(), bytes))
}

#[derive(Serialize, Debug, Clone)]
pub struct PackInfo { pub name: String, pub key: String, pub size: u64, pub unpacked: u64, pub sha256: String }

/// Gera `<saída>\<nome>-<chave>.tar.zst` determinístico (ordem fixa, mtime 0, sem dono).
pub fn pack(dir: &Path, name: &str, out_dir: &Path) -> io::Result<PackInfo> {
    let (key, unpacked) = content_key(dir)?;
    fs::create_dir_all(out_dir)?;
    let target = out_dir.join(format!("{name}-{key}.tar.zst"));
    let tmp = out_dir.join(format!("{name}-{key}.tar.zst.tmp"));
    {
        let mut encoder = zstd::Encoder::new(io::BufWriter::with_capacity(1 << 20, fs::File::create(&tmp)?), 19)?;
        let threads = std::thread::available_parallelism().map(|n| n.get() as u32).unwrap_or(1);
        encoder.multithread(threads)?;
        let mut builder = tar::Builder::new(encoder);
        builder.mode(tar::HeaderMode::Deterministic);
        for (rel, path) in files(dir)? {
            let mut header = tar::Header::new_gnu();
            header.set_size(fs::metadata(&path)?.len());
            header.set_mode(0o644);
            header.set_mtime(0);
            header.set_uid(0);
            header.set_gid(0);
            header.set_entry_type(tar::EntryType::Regular);
            builder.append_data(&mut header, &rel, fs::File::open(&path)?)?;
        }
        builder.into_inner()?.finish()?.flush()?;
    }
    fs::rename(&tmp, &target)?;
    Ok(PackInfo { name: name.into(), key, size: fs::metadata(&target)?.len(), unpacked, sha256: sha256_file(&target)? })
}

/// Lê e calcula o SHA-256 do que passa.
struct Hashing<R> { inner: R, hash: Sha256, read: u64, progress: Box<dyn FnMut(u64)> }
impl<R: Read> Read for Hashing<R> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let n = self.inner.read(buf)?;
        self.hash.update(&buf[..n]);
        self.read += n as u64;
        (self.progress)(self.read);
        Ok(n)
    }
}

/// Só caminhos relativos simples: nada absoluto, `..`, letras de unidade ou ligações.
fn safe_relative(path: &Path) -> Option<PathBuf> {
    let mut out = PathBuf::new();
    for c in path.components() {
        match c { Component::Normal(p) => out.push(p), Component::CurDir => {}, _ => return None }
    }
    (!out.as_os_str().is_empty()).then_some(out)
}

pub fn is_complete(store: &Path, name: &str, key: &str) -> bool {
    fs::read_to_string(store.join(format!("{name}-{key}")).join(COMPLETE)).map(|k| k.trim() == key).unwrap_or(false)
}

/// Instala um pacote verificado em `store\<nome>-<chave>`. Extrai para `staging\…\.partial`,
/// confere o SHA-256 do arquivo e a chave do conteúdo, e só então muda o nome.
/// `progress` recebe os bytes do arquivo já lidos.
pub fn install_pack(src: impl Read, size: u64, sha256: &str, name: &str, key: &str, store: &Path, staging: &Path,
                    progress: Box<dyn FnMut(u64)>) -> Res<PathBuf> {
    let target = store.join(format!("{name}-{key}"));
    if is_complete(store, name, key) { return Ok(target); }
    fs::create_dir_all(store)?;
    fs::create_dir_all(staging)?;
    let partial = staging.join(format!("{name}-{key}.partial"));
    remove_tree(&partial)?;
    fs::create_dir_all(&partial)?;
    let result = (|| -> Res<()> {
        let mut reader = Hashing { inner: src.take(size), hash: Sha256::new(), read: 0, progress };
        {
            let decoder = zstd::Decoder::new(&mut reader).map_err(|e| Fail::new("extract", e.to_string()))?;
            let mut archive = tar::Archive::new(decoder);
            for entry in archive.entries().map_err(|e| Fail::new("extract", e.to_string()))? {
                let mut entry = entry.map_err(|e| Fail::new("extract", e.to_string()))?;
                let kind = entry.header().entry_type();
                let path = entry.path().map_err(|e| Fail::new("extract", e.to_string()))?.into_owned();
                let rel = safe_relative(&path).ok_or_else(|| Fail::new("extract", format!("caminho recusado: {}", path.display())))?;
                if kind.is_dir() { fs::create_dir_all(partial.join(&rel))?; continue; }
                if !kind.is_file() { return Err(Fail::new("extract", format!("tipo recusado: {}", path.display()))); }
                let out = partial.join(&rel);
                if let Some(parent) = out.parent() { fs::create_dir_all(parent)?; }
                let mut file = io::BufWriter::with_capacity(1 << 20, fs::File::create(&out)?);
                io::copy(&mut entry, &mut file)?;
                file.flush()?;
            }
        }
        io::copy(&mut reader, &mut io::sink())?; // o resto do zstd conta para o hash
        if reader.read != size { return Err(Fail::new("hash", format!("{name}: {} de {size} bytes", reader.read))); }
        let got = hex::encode(reader.hash.finalize());
        if got != sha256 { return Err(Fail::new("hash", format!("{name}: sha256 {got} != {sha256}"))); }
        let (real, _) = content_key(&partial)?;
        if real != key { return Err(Fail::new("hash", format!("{name}: chave {real} != {key}"))); }
        fs::write(partial.join(COMPLETE), key)?;
        remove_tree(&target)?; // restos sem .complete de uma tentativa antiga
        rename_with_retry(&partial, &target, RENAME_ATTEMPTS)
            .map_err(|e| Fail::new("locked", format!("{name}: {e}")))?;
        Ok(())
    })();
    if let Err(e) = &result {
        log(&format!("install_pack {name}-{key} failed: {e}"));
        let _ = remove_tree(&partial);
    }
    result.map(|_| target)
}

#[cfg(test)]
pub mod tests {
    use super::*;

    pub fn scratch(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("lunmi-pack-test-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }

    pub fn tree(dir: &Path, files: &[(&str, &[u8])]) {
        for (p, body) in files {
            let path = dir.join(p);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, body).unwrap();
        }
    }

    fn install(info: &PackInfo, out: &Path, store: &Path) -> Res<PathBuf> {
        let f = fs::File::open(out.join(format!("{}-{}.tar.zst", info.name, info.key))).unwrap();
        install_pack(f, info.size, &info.sha256, &info.name, &info.key, store, &store.join("..").join("staging"), Box::new(|_| {}))
    }

    #[test]
    fn key_matches_the_documented_serialization() {
        let d = scratch("key");
        tree(&d, &[("b.txt", b"bb"), ("a/x.bin", b"x"), (".complete", b"ignored")]);
        let line = |p: &str, b: &[u8]| format!("{p}\0{}\0{}\n", b.len(), hex::encode(Sha256::digest(b)));
        let expected = hex::encode(Sha256::digest(format!("{}{}", line("a/x.bin", b"x"), line("b.txt", b"bb")).as_bytes()));
        assert_eq!(content_key(&d).unwrap(), (expected[..16].to_string(), 3));
    }

    #[test]
    fn pack_is_deterministic_and_round_trips() {
        let d = scratch("round");
        let src = d.join("src");
        tree(&src, &[("python.exe", b"exe"), ("Lib/os.py", b"import x"), ("Lib/deep/caminho com espaço e ç.py", b"ok")]);
        let a = pack(&src, "python", &d.join("out1")).unwrap();
        let b = pack(&src, "python", &d.join("out2")).unwrap();
        assert_eq!((a.key.clone(), a.sha256.clone()), (b.key, b.sha256), "mesmo conteúdo, mesmo arquivo");
        let installed = install(&a, &d.join("out1"), &d.join("store")).unwrap();
        assert_eq!(fs::read(installed.join("Lib/deep/caminho com espaço e ç.py")).unwrap(), b"ok");
        assert!(is_complete(&d.join("store"), "python", &a.key));
        assert!(!d.join("staging").join(format!("python-{}.partial", a.key)).exists());
    }

    #[test]
    fn a_damaged_archive_is_refused_and_leaves_nothing() {
        let d = scratch("damaged");
        let src = d.join("src");
        tree(&src, &[("python.exe", &[7u8; 5000])]);
        let info = pack(&src, "python", &d.join("out")).unwrap();
        let wrong = PackInfo { sha256: "0".repeat(64), ..info.clone() };
        let err = install(&wrong, &d.join("out"), &d.join("store")).unwrap_err();
        assert_eq!(err.code, "hash");
        assert!(!d.join("store").join(format!("python-{}", info.key)).exists());
    }

    #[test]
    fn traversal_paths_are_refused() {
        let d = scratch("evil");
        let archive = d.join("evil.tar.zst");
        {
            let enc = zstd::Encoder::new(fs::File::create(&archive).unwrap(), 3).unwrap();
            let mut b = tar::Builder::new(enc);
            let mut h = tar::Header::new_gnu();
            h.set_size(1);
            h.set_entry_type(tar::EntryType::Regular);
            // set_path recusa "..", por isso escreve-se o nome cru no cabeçalho.
            h.as_old_mut().name[..9].copy_from_slice(b"../x.txt\0");
            h.set_cksum();
            b.append(&h, &b"x"[..]).unwrap();
            b.into_inner().unwrap().finish().unwrap();
        }
        let size = fs::metadata(&archive).unwrap().len();
        let sha = sha256_file(&archive).unwrap();
        let err = install_pack(fs::File::open(&archive).unwrap(), size, &sha, "app", "0123456789abcdef", &d.join("store"), &d.join("staging"), Box::new(|_| {})).unwrap_err();
        assert_eq!(err.code, "extract");
        assert!(!d.join("x.txt").exists());
    }

    #[test]
    fn a_rename_blocked_by_an_open_file_is_retried() {
        use std::os::windows::fs::OpenOptionsExt;
        let d = scratch("held");
        let from = d.join("a.partial");
        tree(&from, &[("held.txt", b"x")]);
        // Sem FILE_SHARE_DELETE, como o indexador: a pasta não pode mudar de nome enquanto está aberto.
        let held = fs::OpenOptions::new().read(true).share_mode(1).open(from.join("held.txt")).unwrap();
        assert!(fs::rename(&from, d.join("a")).is_err(), "o bloqueio tem de impedir a mudança de nome");
        let release = std::thread::spawn(move || { std::thread::sleep(std::time::Duration::from_millis(2500)); drop(held); });
        rename_with_retry(&from, &d.join("a"), 10).unwrap();
        release.join().unwrap();
        assert!(d.join("a").join("held.txt").exists());
    }
}
