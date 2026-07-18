//! Verify a staging updater artifact and prove a one-byte mutation is refused.

use base64::{engine::general_purpose::STANDARD, Engine as _};
use minisign_verify::{PublicKey, Signature};
use std::{env, fs, path::PathBuf};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let arguments = env::args_os()
        .skip(1)
        .map(PathBuf::from)
        .collect::<Vec<_>>();
    if arguments.len() != 3 {
        return Err("usage: verify_updater_artifact ARTIFACT SIGNATURE TAURI_CONFIG".into());
    }
    let artifact = fs::read(&arguments[0])?;
    if artifact.is_empty() {
        return Err("updater artifact is empty".into());
    }
    let signature_box = STANDARD.decode(fs::read_to_string(&arguments[1])?.trim())?;
    let signature = Signature::decode(std::str::from_utf8(&signature_box)?)?;
    let config: serde_json::Value = serde_json::from_slice(&fs::read(&arguments[2])?)?;
    let encoded_public_key = config
        .pointer("/plugins/updater/pubkey")
        .and_then(serde_json::Value::as_str)
        .ok_or("updater public key is missing")?;
    let public_key_box = STANDARD.decode(encoded_public_key)?;
    let public_key = PublicKey::decode(std::str::from_utf8(&public_key_box)?)?;
    public_key.verify(&artifact, &signature, true)?;

    let mut tampered = artifact;
    tampered[0] ^= 1;
    if public_key.verify(&tampered, &signature, true).is_ok() {
        return Err("tampered updater artifact unexpectedly verified".into());
    }
    println!("updater signature verified; one-byte tamper refused");
    Ok(())
}
