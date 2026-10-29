"""Build the pinned Codex TUI with the Ruti footer item on Windows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

TAG = "rust-v0.164.0"
COMMIT = "6aaac694803004a63c6f393d153c96d22521a6b9"


def run(args, *, cwd=None, env=None):
    print("+ " + " ".join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), cwd=cwd, env=env, check=True)


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-root", type=Path, default=repo / "build" / "codex-ruti")
    parser.add_argument("--jobs", type=int, default=1)
    args = parser.parse_args()
    if os.name != "nt" or args.jobs < 1:
        parser.error("Windows and at least one build job are required")
    root = args.build_root.resolve()
    source = root / "source"
    patch = repo / "codex" / "patches" / "statusline-ruti-0.164.0.patch"
    git = shutil.which("git")
    if not git or not patch.is_file():
        raise RuntimeError("Git and the reviewed Codex patch are required")
    root.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        run([git, "clone", "--depth", "1", "--branch", TAG,
             "https://github.com/openai/codex.git", source])
    commit = subprocess.check_output([git, "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if commit != COMMIT:
        raise RuntimeError("source checkout differs from the reviewed Codex commit")
    applied = subprocess.run([git, "apply", "--reverse", "--check", str(patch)],
                             cwd=source, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if applied.returncode:
        run([git, "apply", "--check", patch], cwd=source)
        run([git, "apply", patch], cwd=source)

    env = dict(os.environ)
    local_cargo = root / "cargo" / "bin" / "cargo.exe"
    cargo = str(local_cargo) if local_cargo.is_file() else shutil.which("cargo")
    if not cargo:
        raise RuntimeError("install Rust 1.99.1 with rustfmt, clippy and rust-src first")
    if local_cargo.is_file():
        env["CARGO_HOME"] = str(root / "cargo")
        env["RUSTUP_HOME"] = str(root / "rustup")
    vswhere = Path(env.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft Visual Studio/Installer/vswhere.exe"
    vs = subprocess.check_output([str(vswhere), "-latest", "-products", "*", "-requires",
                                  "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                                  "-property", "installationPath"], text=True).strip()
    if not vs:
        raise RuntimeError("install MSVC x64 tools and the Windows SDK in Visual Studio Build Tools")
    devcmd = Path(vs) / "Common7" / "Tools" / "VsDevCmd.bat"
    comspec = env.get("COMSPEC", "C:/Windows/System32/cmd.exe")
    # cmd.exe uses its own quote rules, rather than the C runtime argv convention.
    command = f'"{comspec}" /d /s /c "call "{devcmd}" -arch=x64 -host_arch=x64 >nul && set"'
    initialized = subprocess.check_output(command, env=env, text=True)
    env = {key.upper(): value for key, value in env.items()}
    for line in initialized.splitlines():
        key, sep, value = line.partition("=")
        if sep and key:
            env[key.upper()] = value
    env["PATH"] = str(Path(cargo).parent) + os.pathsep + env.get("PATH", "")
    env["CARGO_BUILD_JOBS"] = str(args.jobs)
    workspace = source / "codex-rs"
    # The release tag changes workspace versions but carries the earlier lockfile.
    run([cargo, "update", "--workspace"], cwd=workspace, env=env)
    run([cargo, "build", "--locked", "--profile", "dev-small", "-p", "codex-cli", "--bin", "codex"],
        cwd=workspace, env=env)
    binary = workspace / "target" / "dev-small" / "codex.exe"
    manifest = {"source_tag": TAG, "source_commit": COMMIT, "profile": "dev-small",
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest()}
    (root / "artifact.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(binary, flush=True)


if __name__ == "__main__":
    main()
