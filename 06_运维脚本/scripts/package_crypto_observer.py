"""Build a small allowlisted source archive, excluding credentials and runtime data."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path


def build(output, mode="observer"):
    if mode not in {"observer", "testnet", "research"}:
        raise ValueError("mode must be observer, testnet, or research")
    # This script also lives under 06_运维脚本/scripts; resolve the project
    # root from its current package depth instead of assuming scripts/ is
    # directly under the root.
    root = Path(__file__).resolve().parents[2]
    files = [root/"pyproject.toml", root/"README.md"]
    files += sorted((root/"quant").rglob("*.py"))
    files += sorted((root/"deploy/tencent").glob("*"))
    script_names = ["crypto_market_check.py", "backup_crypto_observer.py"]
    doc_names = ["CRYPTO_OBSERVATION.md", "TENCENT_DEPLOY.md"]
    if mode == "research":
        doc_names.append("CRYPTO_RESEARCH.md")
    if mode == "testnet":
        script_names.append("testnet_market_check.py")
        script_names.append("systemd_alert.py")
        doc_names += ["TENCENT_TESTNET.md", "CRYPTO_ADAPTIVE.md", "CRYPTO_RESEARCH.md",
                      "ADAPTIVE_ACCEPTANCE_TEMPLATE.json"]
    files += [root/"scripts"/name for name in script_names]
    files += [root/"docs"/name for name in doc_names]
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError("Archive already exists; choose a new filename")
    hashes = {}
    for path in files:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Expected regular source file: {path.relative_to(root)}")
        hashes[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_mode = ("spot_testnet_shadow_and_gated_auto_orders" if mode == "testnet"
                     else ("research_commands_with_existing_testnet_code" if mode == "research" else "signal_only"))
    manifest = json.dumps({"mode":manifest_mode, "files":hashes,
                           "contains_credentials":False, "contains_database":False}, indent=2).encode()
    with tarfile.open(output, "w:gz") as archive:
        for path in files:
            archive.add(path, arcname=str(path.relative_to(root)), recursive=False)
        info = tarfile.TarInfo("DEPLOY_MANIFEST.json")
        info.size = len(manifest)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(manifest))
    sha = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix+".sha256").write_text(f"{sha}  {output.name}\n")
    return {"archive":str(output), "sha256":sha, "files":len(files)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", choices=("observer", "testnet", "research"), default="observer")
    args = parser.parse_args()
    print(json.dumps(build(args.output, mode=args.mode), indent=2))
