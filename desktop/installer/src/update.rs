//! Manifesto assinado do canal, transferência com retoma e instalação dos pacotes em falta.
use std::cmp::Ordering;
use std::fs;
use std::io::{Read, Write};
use std::path::Path;
use std::time::Duration;

use base64::Engine;
use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use serde::Deserialize;
use serde_json::json;

use crate::common::{emit, free_bytes, log, Fail, Res};
use crate::layout::{Layout, Packs};
use crate::pack::{install_pack, is_complete, sha256_file, NAMES};

pub const CHANNEL: &str = "https://github.com/MateusCosta888/Lunmi/releases/download/channel-beta/latest.json";
const RELEASES: &str = "https://github.com/MateusCosta888/Lunmi/releases/download/";
/// Hosts para onde o GitHub redireciona os assets (igual a desktop/electron/update-download.ts).
const HOSTS: [&str; 4] = ["github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com", "github-releases.githubusercontent.com"];
const PUBLIC_KEY_JSON: &str = include_str!("../../electron/update-public-key.json");
const MANIFEST_MAX: u64 = 64 * 1024;
/// Esperas entre tentativas quando a ligação cai (a do utilizador cai em transferências grandes).
const RETRY_WAITS: [u64; 5] = [2, 5, 10, 20, 30];

#[derive(Deserialize, Clone, Debug)]
pub struct PackRef { pub name: String, pub key: String, pub size: u64, pub unpacked: u64, pub sha256: String, pub url: String }

#[derive(Deserialize, Clone, Debug)]
pub struct Manifest {
    pub schema: u32,
    pub version: String,
    #[serde(default)] pub notes: String,
    pub packs: Vec<PackRef>,
}

impl Manifest {
    pub fn keys(&self) -> Packs { self.packs.iter().map(|p| (p.name.clone(), p.key.clone())).collect() }

    pub fn validate(&self, require_urls: bool) -> Res<()> {
        let bad = |why: &str| Err(Fail::new("signature", format!("manifesto inválido: {why}")));
        if self.schema != 1 || parse_version(&self.version).is_none() { return bad("versão"); }
        let mut names: Vec<&str> = self.packs.iter().map(|p| p.name.as_str()).collect();
        names.sort_unstable();
        let mut expected = NAMES.to_vec();
        expected.sort_unstable();
        if names != expected { return bad("pacotes"); }
        for p in &self.packs {
            let hexn = |s: &str, n: usize| s.len() == n && s.bytes().all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase());
            if !hexn(&p.key, 16) || !hexn(&p.sha256, 64) || p.size == 0 { return bad(&p.name); }
            if require_urls && !p.url.starts_with(RELEASES) { return Err(Fail::new("source", format!("url de {}", p.name))); }
        }
        Ok(())
    }
}

fn public_key() -> VerifyingKey {
    let v: serde_json::Value = serde_json::from_str(PUBLIC_KEY_JSON).expect("update-public-key.json");
    let pem = v["publicKey"].as_str().expect("publicKey");
    let b64: String = pem.lines().filter(|l| !l.starts_with("-----")).collect();
    let der = base64::engine::general_purpose::STANDARD.decode(b64).expect("chave pública");
    // SubjectPublicKeyInfo Ed25519: os últimos 32 bytes são a chave.
    VerifyingKey::from_bytes(der[der.len() - 32..].try_into().unwrap()).expect("chave Ed25519")
}

pub fn verify_manifest(raw: &[u8], sig: &[u8], key: &VerifyingKey) -> Res<Manifest> {
    let sig = Signature::from_slice(sig).map_err(|_| Fail::new("signature", "assinatura com tamanho errado"))?;
    key.verify(raw, &sig).map_err(|_| Fail::new("signature", "assinatura não confere"))?;
    let m: Manifest = serde_json::from_slice(raw).map_err(|e| Fail::new("signature", e.to_string()))?;
    m.validate(true)?;
    Ok(m)
}

/// `X.Y.Z` ou `X.Y.Z-beta.N`; uma versão final é mais nova do que qualquer beta da mesma base.
pub fn parse_version(v: &str) -> Option<(u64, u64, u64, Option<u64>)> {
    let (core, pre) = match v.split_once('-') { Some((c, p)) => (c, Some(p)), None => (v, None) };
    let mut it = core.split('.').map(|n| n.parse::<u64>().ok());
    let (a, b, c) = (it.next()??, it.next()??, it.next()??);
    if it.next().is_some() { return None; }
    let pre = match pre { None => None, Some(p) => Some(p.strip_prefix("beta.")?.parse().ok()?) };
    Some((a, b, c, pre))
}

pub fn newer(candidate: &str, installed: &str) -> bool {
    let Some(a) = parse_version(candidate) else { return false };
    let Some(b) = parse_version(installed) else { return true }; // nada instalado (ou ilegível)
    let core = (a.0, a.1, a.2).cmp(&(b.0, b.1, b.2));
    let order = if core != Ordering::Equal { core } else {
        match (a.3, b.3) { (None, None) => Ordering::Equal, (None, Some(_)) => Ordering::Greater, (Some(_), None) => Ordering::Less, (Some(x), Some(y)) => x.cmp(&y) }
    };
    order == Ordering::Greater
}

// ---------- Rede ----------

fn agent() -> ureq::Agent {
    let tls = native_tls::TlsConnector::new().expect("TLS do Windows");
    ureq::AgentBuilder::new().tls_connector(std::sync::Arc::new(tls)).redirects(0)
        .timeout_connect(Duration::from_secs(20)).timeout_read(Duration::from_secs(60)).user_agent("Lunmi-Updater").build()
}

fn allowed(url: &str) -> bool {
    let Some(rest) = url.strip_prefix("https://") else { return false };
    let host = rest.split(['/', '?', '#']).next().unwrap_or("");
    HOSTS.contains(&host)
}

/// GET com redirecionamentos validados um a um (só hosts do GitHub).
fn get(agent: &ureq::Agent, url: &str, range_from: u64) -> Res<ureq::Response> {
    let mut url = url.to_string();
    for _ in 0..6 {
        if !allowed(&url) { return Err(Fail::new("source", "endereço fora do GitHub")); }
        let mut req = agent.get(&url);
        if range_from > 0 { req = req.set("Range", &format!("bytes={range_from}-")); }
        let resp = match req.call() {
            Ok(r) => r,
            Err(ureq::Error::Status(_, r)) => r,
            Err(e) => return Err(Fail::new("offline", e.to_string())),
        };
        match resp.status() {
            301 | 302 | 303 | 307 | 308 => {
                url = resp.header("location").ok_or_else(|| Fail::new("source", "redirecionamento sem destino"))?.to_string();
            }
            200 | 206 => return Ok(resp),
            416 if range_from > 0 => return Ok(resp),
            s => return Err(Fail::new(if s >= 500 { "offline" } else { "source" }, format!("HTTP {s}"))),
        }
    }
    Err(Fail::new("source", "demasiados redirecionamentos"))
}

fn get_small(agent: &ureq::Agent, url: &str) -> Res<Vec<u8>> {
    let mut body = Vec::new();
    get(agent, url, 0)?.into_reader().take(MANIFEST_MAX + 1).read_to_end(&mut body).map_err(|e| Fail::new("offline", e.to_string()))?;
    if body.len() as u64 > MANIFEST_MAX { return Err(Fail::new("signature", "manifesto demasiado grande")); }
    Ok(body)
}

pub fn fetch_manifest() -> Res<Manifest> {
    let agent = agent();
    let raw = get_small(&agent, CHANNEL)?;
    let sig = get_small(&agent, &format!("{CHANNEL}.sig"))?;
    verify_manifest(&raw, &sig, &public_key())
}

/// Transfere para `part`, continuando do que já existe; `on` recebe os bytes acumulados.
fn download(agent: &ureq::Agent, p: &PackRef, part: &Path, on: &mut dyn FnMut(u64)) -> Res<()> {
    let mut attempt = 0;
    loop {
        let have = fs::metadata(part).map(|m| m.len()).unwrap_or(0).min(p.size);
        if have == p.size { return Ok(()); }
        let result = (|| -> Res<()> {
            let resp = get(agent, &p.url, have)?;
            let resumed = resp.status() == 206;
            let mut file = fs::OpenOptions::new().create(true).write(true).append(resumed).truncate(!resumed).open(part)?;
            let mut done = if resumed { have } else { 0 };
            let mut reader = resp.into_reader();
            let mut buf = vec![0u8; 1 << 20];
            loop {
                let n = reader.read(&mut buf).map_err(|e| Fail::new("offline", e.to_string()))?;
                if n == 0 { break; }
                if done + n as u64 > p.size { return Err(Fail::new("hash", format!("{} maior do que o anunciado", p.name))); }
                file.write_all(&buf[..n])?;
                done += n as u64;
                on(done);
            }
            file.flush()?;
            Ok(())
        })();
        match result {
            Ok(()) => { attempt = 0; continue; } // volta a medir: completo ou ligação cortada sem erro
            Err(e) if e.code == "offline" && attempt < RETRY_WAITS.len() => {
                log(&format!("download {} retry {}: {e}", p.name, attempt + 1));
                std::thread::sleep(Duration::from_secs(RETRY_WAITS[attempt]));
                attempt += 1;
            }
            Err(e) => return Err(e),
        }
    }
}

// ---------- Comandos ----------

pub fn check(layout: &Layout) -> Res<Option<Manifest>> {
    let installed = layout.read_current().map(|c| c.version).unwrap_or_default();
    let m = fetch_manifest()?;
    Ok(newer(&m.version, &installed).then_some(m))
}

/// Transfere e instala o que falta da versão anunciada e monta `versions\<v>`.
pub fn stage(layout: &Layout, version: &str) -> Res<()> {
    let m = fetch_manifest()?;
    if m.version != version { return Err(Fail::new("source", format!("o canal anuncia {} e não {version}", m.version))); }
    let missing: Vec<&PackRef> = m.packs.iter().filter(|p| !is_complete(&layout.store(), &p.name, &p.key)).collect();
    let need: u64 = missing.iter().map(|p| p.size + p.unpacked).sum::<u64>() + (64 << 20);
    if let Some(free) = free_bytes(&layout.root) {
        if free < need { return Err(Fail::new("space", format!("precisa de {} MB, há {} MB", need >> 20, free >> 20))); }
    }
    let total: u64 = missing.iter().map(|p| p.size).sum::<u64>().max(1);
    let agent = agent();
    fs::create_dir_all(layout.staging())?;
    let mut before = 0u64;
    let mut last = -1i64;
    for p in &missing {
        let part = layout.staging().join(format!("{}-{}.tar.zst.part", p.name, p.key));
        download(&agent, p, &part, &mut |done| {
            let pct = ((before + done) * 100 / total) as i64;
            if pct != last { last = pct; emit(json!({ "phase": "downloading", "percent": pct })); }
        })?;
        if fs::metadata(&part)?.len() != p.size || sha256_file(&part)? != p.sha256 {
            let _ = fs::remove_file(&part);
            return Err(Fail::new("hash", format!("{} não confere", p.name)));
        }
        emit(json!({ "phase": "installing", "percent": ((before + p.size) * 100 / total) as i64 }));
        install_pack(fs::File::open(&part)?, p.size, &p.sha256, &p.name, &p.key, &layout.store(), &layout.staging(), Box::new(|_| {}))?;
        let _ = fs::remove_file(&part);
        before += p.size;
    }
    layout.assemble(&m.version, &m.keys())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::{Signer, SigningKey};

    fn manifest_json() -> serde_json::Value {
        let pack = |n: &str| json!({ "name": n, "key": "0123456789abcdef", "size": 10, "unpacked": 20, "sha256": "a".repeat(64),
                                     "url": format!("{RELEASES}packs/{n}-0123456789abcdef.tar.zst") });
        json!({ "schema": 1, "channel": "beta", "version": "1.0.0-beta.2", "notes": "x",
                "packs": [pack("base"), pack("app"), pack("python"), pack("voice")] })
    }

    #[test]
    fn the_embedded_key_matches_the_electron_one() { let _ = public_key(); }

    #[test]
    fn signature_must_match_the_exact_bytes() {
        let sk = SigningKey::from_bytes(&[7u8; 32]);
        let raw = serde_json::to_vec(&manifest_json()).unwrap();
        let sig = sk.sign(&raw).to_bytes();
        assert_eq!(verify_manifest(&raw, &sig, &sk.verifying_key()).unwrap().version, "1.0.0-beta.2");
        let mut tampered = raw.clone();
        *tampered.last_mut().unwrap() = b' ';
        assert_eq!(verify_manifest(&tampered, &sig, &sk.verifying_key()).unwrap_err().code, "signature");
        let other = SigningKey::from_bytes(&[8u8; 32]);
        assert_eq!(verify_manifest(&raw, &sig, &other.verifying_key()).unwrap_err().code, "signature");
    }

    #[test]
    fn manifests_outside_the_contract_are_refused() {
        let sk = SigningKey::from_bytes(&[7u8; 32]);
        let check = |m: serde_json::Value| { let raw = serde_json::to_vec(&m).unwrap(); verify_manifest(&raw, &sk.sign(&raw).to_bytes(), &sk.verifying_key()) };
        let mut m = manifest_json(); m["packs"][0]["url"] = json!("https://evil.example/base.tar.zst");
        assert_eq!(check(m).unwrap_err().code, "source");
        let mut m = manifest_json(); m["packs"].as_array_mut().unwrap().pop();
        assert!(check(m).is_err());
        let mut m = manifest_json(); m["packs"][1]["key"] = json!("../../x");
        assert!(check(m).is_err());
    }

    #[test]
    fn versions_compare_like_the_release_channel() {
        assert!(newer("1.0.0-beta.2", "1.0.0-beta.1"));
        assert!(newer("1.0.0-beta.10", "1.0.0-beta.9"));
        assert!(newer("1.0.0", "1.0.0-beta.9"));
        assert!(!newer("1.0.0-beta.1", "1.0.0"));
        assert!(newer("1.0.0-beta.1", ""), "sem instalação, qualquer versão válida é nova");
        assert!(!newer("1.0.0-alpha.1", "0.9.0"), "só betas e finais");
    }

    #[test]
    fn only_github_hosts_are_contacted() {
        assert!(allowed("https://github.com/MateusCosta888/Lunmi/releases/download/x"));
        assert!(allowed("https://release-assets.githubusercontent.com/a?b"));
        assert!(!allowed("http://github.com/x"));
        assert!(!allowed("https://github.com.evil.example/x"));
    }
}
