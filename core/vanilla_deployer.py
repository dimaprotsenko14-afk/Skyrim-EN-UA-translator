"""
Phase 2 - Step 3: Build & Deploy Vanilla Ukrainian Localization from English Originals (core/vanilla_deployer.py)
1. Reads all 241 extracted English files from data/extracted_en/
2. Translates every entry via skyrim_tm.db + HybridTranslator fallback
3. Compiles binary .strings, .dlstrings, .ilstrings and UTF-16 LE translate_english.txt via core/strings_writer.py
   into build_vanilla_uk/Data/Strings/ and build_vanilla_uk/Data/Interface/
4. Copies Ukrainian font & Scaleform interface files (fonts_en.swf, fontconfig.txt, book.swf, etc.)
   from reference/Interface/ into build_vanilla_uk/Data/Interface/
5. Provides safe deploy_to_game() and rollback_from_game() with deployed_manifest.json tracking.
"""

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Dict, List, Tuple, Any

from core.database import SkyrimTranslationDB, DB_PATH
from core.hybrid_translator import (
    HybridTranslator,
    _google_neural_translate,
    apply_ukrainian_lore_postprocessing,
    find_lore_terms_in_text,
)
from core.scanner import PROJECT_ROOT, REFERENCE_DIR, GAME_DATA_DIR
from core.strings_parser import (
    parse_strings_file,
    parse_translate_txt,
    extract_module_and_ext,
)
from core.strings_writer import write_string_table_file, write_translate_txt

EXTRACTED_EN_DIR = PROJECT_ROOT / "data" / "extracted_en"
BUILD_VANILLA_UK_DIR = PROJECT_ROOT / "build_vanilla_uk"
BUILD_DATA_DIR = BUILD_VANILLA_UK_DIR / "Data"
MANIFEST_PATH = PROJECT_ROOT / "data" / "deployed_manifest.json"

CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")


def _load_fast_tm_maps(db_path: Path = DB_PATH) -> Tuple[Dict[Tuple[str, int], str], Dict[str, str], Dict[str, str]]:
    """
    Load all 100,184 pairs from skyrim_tm.db into memory for fast bulk compilation:
      - by_mod_sid: {(module_name_lower, string_id): uk_text}
      - by_en_lower: {en_text_lower: uk_text}
      - ui_key_map: {form_id ($Key): uk_text}
    """
    db = SkyrimTranslationDB(db_path)
    cur = db.conn.cursor()
    cur.execute(
        """
        SELECT source_group, module_name, record_type, string_id, form_id, en_text, uk_text, is_glossary_term
        FROM translations;
        """
    )
    by_mod_sid: Dict[Tuple[str, int], str] = {}
    by_en_lower: Dict[str, str] = {}
    ui_key_map: Dict[str, str] = {}

    for row in cur.fetchall():
        mod = (row["module_name"] or "").lower()
        sid = row["string_id"]
        en_t = row["en_text"] or ""
        uk_t = row["uk_text"] or ""
        rec_t = row["record_type"] or ""
        fid = row["form_id"] or ""

        if sid is not None and mod:
            by_mod_sid[(mod, int(sid))] = uk_t

        if rec_t == "UI_KEY" and fid:
            ui_key_map[fid] = uk_t

        en_l = en_t.strip().lower()
        if en_l:
            # Prefer Cyrillic translations over untranslated English placeholders
            if en_l not in by_en_lower or (
                CYRILLIC_RE.search(uk_t) and not CYRILLIC_RE.search(by_en_lower[en_l])
            ):
                by_en_lower[en_l] = uk_t

    db.close()
    return by_mod_sid, by_en_lower, ui_key_map


def build_vanilla_uk_from_extracted_en(
    extracted_en_dir: Path = EXTRACTED_EN_DIR,
    build_data_dir: Path = BUILD_DATA_DIR,
    ref_dir: Path = REFERENCE_DIR,
    translate_unmatched_cc: bool = True,
) -> Dict[str, Any]:
    """
    Compile all 241 extracted English files into Ukrainian binary string tables and UI files
    inside build_vanilla_uk/Data/, and copy required Ukrainian Scaleform font/UI assets.
    """
    print("=" * 72)
    print("[Phase 2 - Step 3] Compiling Vanilla English Skyrim to Ukrainian...")
    t0 = time.time()

    if build_data_dir.exists():
        shutil.rmtree(build_data_dir)
    out_strings_dir = build_data_dir / "Strings"
    out_interface_dir = build_data_dir / "Interface"
    out_strings_dir.mkdir(parents=True, exist_ok=True)
    out_interface_dir.mkdir(parents=True, exist_ok=True)

    by_mod_sid, by_en_lower, ui_key_map = _load_fast_tm_maps(DB_PATH)

    en_strings_dir = extracted_en_dir / "strings"
    en_string_files = sorted(
        [
            f
            for f in en_strings_dir.iterdir()
            if f.is_file() and f.suffix.lower() in (".strings", ".dlstrings", ".ilstrings")
        ],
        key=lambda p: p.name.lower(),
    )

    compiled_string_files = 0
    total_string_entries = 0
    tm_matched_entries = 0
    hybrid_translated_entries = 0

    for en_file in en_string_files:
        mod, ext = extract_module_and_ext(en_file.name)
        en_entries = parse_strings_file(en_file)
        uk_entries: Dict[int, str] = {}

        is_unmatched_module = mod in ("_resourcepack", "ccasvsse001-almsivi")
        pending_batch_sids: List[Tuple[int, str]] = []

        for sid, en_text in en_entries.items():
            total_string_entries += 1
            if not en_text or not en_text.strip():
                uk_entries[sid] = en_text
                continue

            # 1. Exact (module_name, string_id) lookup in skyrim_tm.db
            uk_val = by_mod_sid.get((mod, sid))
            if uk_val is not None and (CYRILLIC_RE.search(uk_val) or not is_unmatched_module):
                uk_entries[sid] = uk_val
                tm_matched_entries += 1
                continue

            # 2. Case-insensitive exact text lookup in skyrim_tm.db
            en_l = en_text.strip().lower()
            if en_l in by_en_lower and CYRILLIC_RE.search(by_en_lower[en_l]):
                uk_entries[sid] = by_en_lower[en_l]
                tm_matched_entries += 1
                continue

            # 3. Queue unmatched short strings in _resourcepack / ccasvsse001-almsivi for fast batch translation
            if translate_unmatched_cc and is_unmatched_module and ext == "strings" and "\n" not in en_text and len(en_text) <= 80:
                pending_batch_sids.append((sid, en_text))
            else:
                uk_entries[sid] = uk_val if uk_val is not None else en_text

        # Execute batch neural + glossary translation for pending_batch_sids
        if pending_batch_sids:
            batch_size = 40
            for i in range(0, len(pending_batch_sids), batch_size):
                chunk = pending_batch_sids[i : i + batch_size]
                joined_en = "\n".join(item[1] for item in chunk)
                try:
                    joined_uk = _google_neural_translate(joined_en, source="en", target="uk")
                    uk_lines = [line.strip() for line in joined_uk.splitlines()]
                    if len(uk_lines) == len(chunk):
                        for (sid, orig_en), tr_uk in zip(chunk, uk_lines):
                            lore = find_lore_terms_in_text(orig_en)
                            clean_uk = apply_ukrainian_lore_postprocessing(tr_uk, lore)
                            uk_entries[sid] = clean_uk
                            by_en_lower[orig_en.strip().lower()] = clean_uk
                            hybrid_translated_entries += 1
                    else:
                        for sid, orig_en in chunk:
                            uk_entries[sid] = orig_en
                except Exception:
                    for sid, orig_en in chunk:
                        uk_entries[sid] = orig_en

        out_path = out_strings_dir / en_file.name
        write_string_table_file(uk_entries, out_path, deduplicate=True)
        compiled_string_files += 1

    # Compile Interface/translate_english.txt
    en_tr_file = extracted_en_dir / "interface" / "translate_english.txt"
    compiled_ui_files = 0
    total_ui_keys = 0
    if en_tr_file.exists():
        en_ui = parse_translate_txt(en_tr_file)
        uk_ui: Dict[str, str] = {}
        for key, en_val in en_ui.items():
            total_ui_keys += 1
            if key in ui_key_map:
                uk_ui[key] = ui_key_map[key]
                tm_matched_entries += 1
            elif en_val.strip().lower() in by_en_lower:
                uk_ui[key] = by_en_lower[en_val.strip().lower()]
                tm_matched_entries += 1
            else:
                uk_ui[key] = en_val
        out_tr_path = out_interface_dir / "translate_english.txt"
        write_translate_txt(uk_ui, out_tr_path)
        compiled_ui_files += 1

    # Copy Ukrainian font & Scaleform UI files from reference/Interface (excluding translate_english.txt which we compiled!)
    ref_interface_dir = ref_dir / "Interface"
    copied_font_ui_files: List[str] = []
    if ref_interface_dir.exists():
        for root, _dirs, files in os.walk(ref_interface_dir):
            rel_root = os.path.relpath(root, ref_interface_dir)
            for fname in sorted(files):
                if fname.lower() == "translate_english.txt":
                    continue
                src_f = Path(root) / fname
                dst_dir = out_interface_dir if rel_root == "." else (out_interface_dir / rel_root)
                dst_dir.mkdir(parents=True, exist_ok=True)
                dst_f = dst_dir / fname
                shutil.copy2(src_f, dst_f)
                copied_font_ui_files.append(os.path.relpath(dst_f, build_data_dir))

    elapsed = time.time() - t0
    total_compiled = compiled_string_files + compiled_ui_files
    print(f"  - Compiled {total_compiled} English files ({compiled_string_files} string tables + {compiled_ui_files} translate_english.txt) in {elapsed:.2f}s")
    print(f"  - Total entries processed: {total_string_entries + total_ui_keys:,} (TM matched: {tm_matched_entries:,}, Hybrid neural fallback: {hybrid_translated_entries:,})")
    print(f"  - Copied {len(copied_font_ui_files)} Ukrainian font & Scaleform SWF/TXT files to {out_interface_dir}")

    return {
        "build_data_dir": str(build_data_dir),
        "compiled_string_files": compiled_string_files,
        "compiled_ui_files": compiled_ui_files,
        "total_compiled_files": total_compiled,
        "total_entries": total_string_entries + total_ui_keys,
        "tm_matched_entries": tm_matched_entries,
        "hybrid_translated_entries": hybrid_translated_entries,
        "copied_font_ui_files": copied_font_ui_files,
        "elapsed_sec": round(elapsed, 2),
    }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def deploy_to_game(
    build_data_dir: Path = BUILD_DATA_DIR,
    game_data_dir: Path = GAME_DATA_DIR,
    manifest_path: Path = MANIFEST_PATH,
) -> Dict[str, Any]:
    """
    Copy build_vanilla_uk/Data/* into the configured Skyrim Special Edition/Data/ directory
    and record an exact manifest deployed_manifest.json of every file placed in Data/.
    """
    print("=" * 72)
    print(f"[Deploy] Deploying compiled Ukrainian localization to {game_data_dir}...")
    if not build_data_dir.exists():
        raise FileNotFoundError(f"Build directory does not exist: {build_data_dir}")

    deployed_files: List[Dict[str, Any]] = []
    for root, _dirs, files in os.walk(build_data_dir):
        rel_dir = os.path.relpath(root, build_data_dir)
        for fname in sorted(files):
            src_path = Path(root) / fname
            rel_file = fname if rel_dir == "." else os.path.join(rel_dir, fname)
            dst_path = game_data_dir / rel_file
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path)
            deployed_files.append({
                "rel_path": rel_file.replace("\\", "/"),
                "abs_path": str(dst_path),
                "size": dst_path.stat().st_size,
                "sha256": _sha256_file(dst_path),
            })

    manifest = {
        "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_build_dir": str(build_data_dir),
        "target_game_data_dir": str(game_data_dir),
        "total_deployed_files": len(deployed_files),
        "files": deployed_files,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # Also save a copy inside build_vanilla_uk/deployed_manifest.json
    build_manifest = build_data_dir.parent / "deployed_manifest.json"
    with open(build_manifest, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"  - Deployed {len(deployed_files)} files into {game_data_dir}")
    print(f"  - Manifest recorded at: {manifest_path}")
    return manifest


def record_deployed_files_in_manifest(
    deployed_paths: List[Path],
    game_data_dir: Path = GAME_DATA_DIR,
    manifest_path: Path = MANIFEST_PATH,
) -> Dict[str, Any]:
    """
    Append or update deployed files in deployed_manifest.json so rollback_from_game()
    tracks 100% of Vanilla, Essential SKSE Framework, and Global Mod files placed in Data\\ (or Game Root).
    """
    existing_map: Dict[str, Dict[str, Any]] = {}
    if manifest_path.exists():
        try:
            old_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            for item in old_data.get("files", []):
                existing_map[os.path.normcase(item["abs_path"])] = item
        except Exception:
            pass

    for p in deployed_paths:
        p = Path(p)
        if not p.exists() or not p.is_file():
            continue
        try:
            rel_p = str(p.relative_to(game_data_dir)).replace("\\", "/")
        except ValueError:
            rel_p = p.name
        existing_map[os.path.normcase(str(p))] = {
            "rel_path": rel_p,
            "abs_path": str(p),
            "size": p.stat().st_size,
            "sha256": _sha256_file(p),
        }

    files_list = sorted(existing_map.values(), key=lambda x: x["rel_path"].lower())
    manifest = {
        "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_build_dir": str(BUILD_DATA_DIR),
        "target_game_data_dir": str(game_data_dir),
        "total_deployed_files": len(files_list),
        "files": files_list,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    build_manifest = BUILD_DATA_DIR.parent / "deployed_manifest.json"
    if BUILD_DATA_DIR.parent.exists():
        build_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def rollback_from_game(
    game_data_dir: Path = GAME_DATA_DIR,
    manifest_path: Path = MANIFEST_PATH,
) -> Dict[str, Any]:
    """
    Delete only the deployed loose files listed in deployed_manifest.json (and remove
    empty subdirectories), restoring the game to 100% pure English.
    """
    print("=" * 72)
    print(f"[Rollback] Rolling back deployed Ukrainian & Mod files from {game_data_dir}...")
    if not manifest_path.exists():
        print(f"  - No manifest found at {manifest_path}; nothing to roll back.")
        return {"deleted_count": 0, "remaining_deployed_files": 0}

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    deleted_count = 0
    for item in manifest.get("files", []):
        target_p = Path(item["abs_path"])
        if target_p.exists() and target_p.is_file():
            target_p.unlink()
            deleted_count += 1

    # Restore original SkyrimSE.exe if backed up in data/backup_game_exe/SkyrimSE.exe
    exe_backup = PROJECT_ROOT / "data" / "backup_game_exe" / "SkyrimSE.exe"
    game_exe = game_data_dir.parent / "SkyrimSE.exe"
    if exe_backup.exists():
        shutil.copy2(exe_backup, game_exe)

    # Clean up empty directories created during deployment (bottom-up walk)
    for root, dirs, _files in os.walk(game_data_dir, topdown=False):
        for d in dirs:
            dp = Path(root) / d
            try:
                if dp.exists() and dp.is_dir() and not any(dp.iterdir()):
                    dp.rmdir()
            except OSError:
                pass

    remaining = sum(1 for item in manifest.get("files", []) if Path(item["abs_path"]).exists())
    print(f"  - Deleted {deleted_count} deployed files (remaining in Data\\: {remaining})")
    return {
        "deleted_count": deleted_count,
        "remaining_deployed_files": remaining,
    }



def sync_vanilla_uk_to_gameroot(
    build_data_dir: Path = BUILD_DATA_DIR,
    game_root_data: Path = GAME_DATA_DIR,
    manifest_path: Path = PROJECT_ROOT / "data" / "gameroot_manifest.json",
) -> Dict[str, Any]:
    """
    Sync compiled Ukrainian Vanilla strings & fonts into Game Root Data directory
    with a manifest backup so Mod Organizer 2 has Ukrainian base strings too.
    """
    if not game_root_data.exists():
        return {"synced": False, "reason": f"{game_root_data} does not exist"}

    backup_dir = PROJECT_ROOT / "data" / "gameroot_backup"
    synced_files: List[Dict[str, Any]] = []

    for root, _dirs, files in os.walk(build_data_dir):
        rel_dir = os.path.relpath(root, build_data_dir)
        for fname in sorted(files):
            src_path = Path(root) / fname
            rel_file = fname if rel_dir == "." else os.path.join(rel_dir, fname)
            dst_path = game_root_data / rel_file

            if dst_path.exists():
                bak_path = backup_dir / rel_file
                if not bak_path.exists():
                    bak_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dst_path, bak_path)

            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path)
            synced_files.append({
                "rel_path": rel_file.replace("\\", "/"),
                "abs_path": str(dst_path),
                "size": dst_path.stat().st_size,
            })

    manifest = {
        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "target_dir": str(game_root_data),
        "backup_dir": str(backup_dir),
        "total_synced_files": len(synced_files),
        "files": synced_files,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest

