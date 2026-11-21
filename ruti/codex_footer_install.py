"""Install the locally built Codex Ruti footer alongside the stock CLI."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import tomllib

from .config import STATE_ROOT
from .config import REPO_ROOT

def configure_footer(before: str) -> tuple[str, list[str]]:
    current = tomllib.loads(before)
    items = current.get("tui", {}).get("status_line")
    if items is None:
        items = ["model-with-reasoning", "current-dir", "thread-name"]
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise ValueError("tui.status_line must be a list of strings")
    original = list(items)
    if "ruti" in items:
        return before, original
    items = ["ruti", *items]
    encoded = json.dumps(items, ensure_ascii=False)
    section = re.search(r"(?m)^[ \t]*\[tui\][ \t]*(?:#[^\n]*)?\r?$", before)
    if section:
        following = re.search(r"(?m)^[ \t]*\[[^\r\n]+\][ \t]*(?:#[^\n]*)?\r?$", before[section.end():])
        section_end = section.end() + following.start() if following else len(before)
        key = re.search(r"(?m)^[ \t]*status_line[ \t]*=[ \t]*", before[section.end():section_end])
        if key:
            start = section.end() + key.end()
            if before[start:start + 1] != "[":
                raise ValueError("unsupported status_line value")
            quote = None
            escape = False
            comment = False
            depth = 0
            end = None
            for position in range(start, section_end):
                ch = before[position]
                if comment:
                    comment = ch != "\n"
                elif quote:
                    if escape:
                        escape = False
                    elif ch == "\\" and quote == '"':
                        escape = True
                    elif ch == quote:
                        quote = None
                elif ch == "#":
                    comment = True
                elif ch in "\"'":
                    quote = ch
                elif ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        end = position + 1
                        break
            if end is None:
                raise ValueError("cannot locate the status_line array")
            after = before[:start] + encoded + before[end:]
        elif "status_line" in current.get("tui", {}):
            raise ValueError("status_line is defined outside [tui]; edit it manually")
        else:
            after = before[:section.end()] + "\nstatus_line = " + encoded + before[section.end():]
    else:
        if "status_line" in current.get("tui", {}):
            raise ValueError("status_line uses a dotted key; edit it manually")
        after = before.rstrip() + "\n\n[tui]\nstatus_line = " + encoded + "\n"
    expected = copy.deepcopy(current)
    expected.setdefault("tui", {})["status_line"] = items
    if tomllib.loads(after) != expected:
        raise ValueError("configuration edit would change unrelated settings")
    return after, original


def save_text(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + f".ruti-{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepend_user_path(before: str, directory: Path) -> str:
    value = str(directory)
    key = value.replace("/", "\\").rstrip("\\").casefold()
    others = [entry for entry in before.split(";")
              if entry and entry.replace("/", "\\").rstrip("\\").casefold() != key]
    return ";".join([value, *others])


def broadcast_environment() -> bool:
    import ctypes
    from ctypes import wintypes
    send = ctypes.windll.user32.SendMessageTimeoutW
    send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR,
                     wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    send.restype = wintypes.LPARAM
    result = ctypes.c_size_t()
    return bool(send(0xFFFF, 0x001A, 0, "Environment", 0x0002, 1000, ctypes.byref(result)))


def copy_binary(source: Path, target: Path, digest: str, stamp: str) -> None:
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
        return
    if target.exists():
        shutil.copy2(target, target.with_name(target.name + ".backup-" + stamp))
    temporary = target.with_name(target.name + f"-{os.getpid()}.tmp")
    try:
        shutil.copy2(source, temporary)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
            raise RuntimeError("executable changed while copying")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--stock", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    artifact = json.loads(args.manifest.read_text(encoding="utf-8"))
    digest = hashlib.sha256(args.binary.read_bytes()).hexdigest()
    patch_path = REPO_ROOT / "codex" / "patches" / "statusline-ruti-0.164.0.patch"
    patch_digest = hashlib.sha256(patch_path.read_bytes()).hexdigest()
    if (artifact.get("binary_sha256") != digest
            or artifact.get("source_tag") != "rust-v0.164.0"
            or artifact.get("source_commit") != "6aaac694803004a63c6f393d153c96d22521a6b9"
            or artifact.get("patch_sha256") != patch_digest):
        raise ValueError("binary does not match the reviewed Ruti build manifest")
    if not args.stock.is_file() or not args.stock.is_absolute():
        raise ValueError("stock Codex executable must be an existing absolute path")
    if sys.platform != "win32":
        raise RuntimeError("the native PATH installer currently supports Windows")
    subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", "import ruti.footer"],
                   cwd=Path(sys.executable).resolve().parent, check=True, timeout=4,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
        try:
            original_path, path_kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            original_path, path_kind = "", winreg.REG_EXPAND_SZ
    if not isinstance(original_path, str):
        raise ValueError("unexpected user PATH value type")
    install_dir = STATE_ROOT / "codex-cli" / "0.164.0-ruti"
    binary = install_dir / "codex.exe"
    stock_copy = install_dir / "codex-stock.exe"
    if args.stock.resolve() == binary.resolve() or hashlib.sha256(args.stock.read_bytes()).hexdigest() == digest:
        raise ValueError("stock fallback points to the custom build; supply the original Codex executable")
    installed_path = prepend_user_path(original_path, install_dir)
    config_dir = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    config_path = config_dir / "config.toml"
    before_config = config_path.read_text(encoding="utf-8-sig") if config_path.exists() else ""
    after_config, _original = configure_footer(before_config)
    plan = {"binary": str(binary), "config": str(config_path),
            "user_path_prepend": str(install_dir), "stock_fallback": str(stock_copy), "stock_source": str(args.stock),
            "sha256": digest, "apply": args.apply}
    if args.apply:
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
        changes = [(config_path, before_config, after_config)]
        for path, before, _after in changes:
            actual = path.read_text(encoding="utf-8-sig") if path.exists() else ""
            if actual != before:
                raise RuntimeError(f"{path} changed while preparing the installation")
        install_dir.mkdir(parents=True, exist_ok=True)
        copy_binary(args.binary, binary, digest, stamp)
        copy_binary(args.stock, stock_copy, hashlib.sha256(args.stock.read_bytes()).hexdigest(), stamp)
        save_text(install_dir.parent / "python.path", str(Path(sys.executable).resolve()) + "\n")
        backups = []
        for path, before, after in changes:
            if before == after:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                backup = path.with_name(path.name + ".ruti-backup-" + stamp)
                shutil.copy2(path, backup)
                backups.append(str(backup))
            save_text(path, after)
        shutil.copy2(args.manifest, install_dir / "artifact.json")
        for name in ("CODEX-LICENSE", "CODEX-NOTICE"):
            shutil.copy2(REPO_ROOT / "codex" / "patches" / name, install_dir / name)
        plan["backups"] = backups
        metadata = install_dir / "installation.json"
        if metadata.exists():
            shutil.copy2(metadata, metadata.with_name(metadata.name + ".backup-" + stamp))
        save_text(metadata, json.dumps({
            **plan, "original_user_path": original_path, "installed_user_path": installed_path,
        }, ensure_ascii=False, indent=2))
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
            try:
                current_path, current_kind = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current_path, current_kind = "", winreg.REG_EXPAND_SZ
            if (current_path, current_kind) != (original_path, path_kind):
                raise RuntimeError("user PATH changed during installation; rerun the installer")
            winreg.SetValueEx(key, "Path", 0, path_kind, installed_path)
        plan["environment_broadcast"] = broadcast_environment()
    print(json.dumps(plan, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
