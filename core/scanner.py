"""
Directory Audit & Resource Discovery Scanner (core/scanner.py).
Scans Skyrim SE Data directory, Ukrainian Reference (OPN/TPN) directory, and SSE-AT directory
using portable paths loaded from config.json.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict

from core.config import (
    PROJECT_ROOT,
    get_game_data_dir,
    get_reference_dir,
    get_sseat_dir,
)

GAME_DATA_DIR = get_game_data_dir()
REFERENCE_DIR = get_reference_dir()
SSEAT_DIR = get_sseat_dir()
DATA_OUTPUT_DIR = PROJECT_ROOT / "data"

OPN_BASE_MODULES = {"skyrim", "update", "dawnguard", "hearthfires", "dragonborn"}


def classify_reference_module(filename: str) -> str:
    """Classify a reference localization file as OPN (Base + DLC) or TPN (Anniversary Edition / CC)."""
    lower = filename.lower()
    if lower == "translate_english.txt" or lower.startswith("credits") or lower.startswith("font"):
        return "OPN"
    if "_english." in lower:
        mod_name = lower.rsplit("_english.", 1)[0]
    else:
        mod_name = os.path.splitext(lower)[0]
    if mod_name in OPN_BASE_MODULES:
        return "OPN"
    if mod_name.startswith("cc") or mod_name.startswith("_resourcepack"):
        return "TPN"
    return "OPN"


def scan_game_data(data_dir: Path = GAME_DATA_DIR) -> Dict[str, Any]:
    """Scan Skyrim Special Edition Data directory for BSAs, ESMs, ESLs, ESPs, and loose Strings."""
    result: Dict[str, Any] = {
        "path": str(data_dir),
        "exists": data_dir.exists(),
        "bsa_archives": [],
        "base_esms": [],
        "cc_plugins": [],
        "other_plugins": [],
        "loose_strings_dir_exists": False,
        "loose_strings_files": [],
        "subdirectories": [],
    }
    if not data_dir.exists():
        return result

    for entry in sorted(data_dir.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_dir():
            result["subdirectories"].append(entry.name)
            if entry.name.lower() == "strings":
                result["loose_strings_dir_exists"] = True
                for sf in sorted(entry.iterdir(), key=lambda p: p.name.lower()):
                    if sf.is_file():
                        result["loose_strings_files"].append({
                            "name": sf.name,
                            "size": sf.stat().st_size,
                        })
        elif entry.is_file():
            ext = entry.suffix.lower()
            size = entry.stat().st_size
            stem_lower = entry.stem.lower()
            info = {"name": entry.name, "size": size}
            if ext == ".bsa":
                result["bsa_archives"].append(info)
            elif ext in (".esm", ".esl", ".esp"):
                if stem_lower in OPN_BASE_MODULES:
                    result["base_esms"].append(info)
                elif stem_lower.startswith("cc") or stem_lower == "_resourcepack":
                    result["cc_plugins"].append(info)
                else:
                    result["other_plugins"].append(info)

    return result


def scan_reference_dir(ref_dir: Path = REFERENCE_DIR) -> Dict[str, Any]:
    """Scan Ukrainian reference directory (OPN & TPN) and categorize all relevant files."""
    target_exts = {".strings", ".dlstrings", ".ilstrings", ".txt", ".bsa", ".esp", ".esm", ".esl"}
    result: Dict[str, Any] = {
        "path": str(ref_dir),
        "exists": ref_dir.exists(),
        "subfolders": [],
        "opn_files": [],
        "tpn_files": [],
        "backup_or_other_files": [],
        "counts_by_extension": {},
    }
    if not ref_dir.exists():
        return result

    for root, _dirs, files in os.walk(ref_dir):
        rel_root = os.path.relpath(root, ref_dir)
        if rel_root != ".":
            result["subfolders"].append(rel_root)
        for fname in sorted(files, key=lambda x: x.lower()):
            fpath = Path(root) / fname
            rel_path = os.path.relpath(fpath, ref_dir)
            ext = fpath.suffix.lower()
            size = fpath.stat().st_size

            result["counts_by_extension"][ext] = result["counts_by_extension"].get(ext, 0) + 1

            if ext in target_exts:
                group = classify_reference_module(fname)
                entry = {
                    "rel_path": rel_path,
                    "name": fname,
                    "ext": ext,
                    "size": size,
                    "group": group,
                }
                if group == "OPN":
                    result["opn_files"].append(entry)
                else:
                    result["tpn_files"].append(entry)
            elif ".old" in fname.lower():
                result["backup_or_other_files"].append({
                    "rel_path": rel_path,
                    "name": fname,
                    "size": size,
                })

    return result


def scan_sseat_dir(sseat_dir: Path = SSEAT_DIR) -> Dict[str, Any]:
    """Recursively list and categorize all files in the configured SSE-AT directory."""
    config_exts = {".json", ".xml", ".txt", ".csv", ".sqlite", ".db", ".ini", ".reg", ".dic", ".aff"}
    binary_exts = {".exe", ".dll", ".pyd", ".tlb"}

    result: Dict[str, Any] = {
        "path": str(sseat_dir),
        "exists": sseat_dir.exists(),
        "total_files": 0,
        "config_and_data_files": [],
        "binary_assemblies": [],
        "database_languages": {},
    }
    if not sseat_dir.exists():
        return result

    for root, _dirs, files in os.walk(sseat_dir):
        for fname in sorted(files, key=lambda x: x.lower()):
            result["total_files"] += 1
            fpath = Path(root) / fname
            rel_path = os.path.relpath(fpath, sseat_dir)
            ext = fpath.suffix.lower()
            size = fpath.stat().st_size

            parts = Path(rel_path).parts
            if len(parts) >= 4 and parts[0].lower() == "res" and parts[1].lower() == "app" and parts[2].lower() == "database":
                lang = parts[3].lower()
                result["database_languages"].setdefault(lang, []).append({
                    "name": fname,
                    "rel_path": rel_path,
                    "size": size,
                })

            if ext in config_exts:
                result["config_and_data_files"].append({
                    "rel_path": rel_path,
                    "name": fname,
                    "ext": ext,
                    "size": size,
                })
            elif ext in binary_exts:
                result["binary_assemblies"].append({
                    "rel_path": rel_path,
                    "name": fname,
                    "ext": ext,
                    "size": size,
                })

    return result


def run_directory_audit(save_json: bool = True) -> Dict[str, Any]:
    """Execute full directory audit and return structured report."""
    game_audit = scan_game_data()
    ref_audit = scan_reference_dir()
    sseat_audit = scan_sseat_dir()

    audit_data = {
        "game_data": game_audit,
        "reference": ref_audit,
        "sseat": sseat_audit,
    }
    if save_json:
        DATA_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = DATA_OUTPUT_DIR / "directory_audit.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(audit_data, f, ensure_ascii=False, indent=2)

    return audit_data


if __name__ == "__main__":
    run_directory_audit()
