"""
Universal Modpack & Mod Organizer 2 Batch Translation Orchestrator (core/batch_orchestrator.py).
Provides ModpackBatchOrchestrator (and BatchOrchestrator alias) with two-stage staging + MO2 patch sync,
strict whitelist enforcement, Path Traversal protection, and graceful SIGINT checkpoint saving.
"""

import json
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.bsa_parser import BSAArchive
from core.checkpoint_manager import CheckpointManager, sha256_of_files
from core.config import (
    PROJECT_ROOT,
    PathTraversalError,
    apply_mcm_title_mode,
    build_mod_aliases,
    ensure_safe_output_path,
    get_mcm_title_mode,
    get_mo2_mods_dir,
    get_mo2_profile_dir,
    get_output_patch_dir,
    is_mcm_quest_title,
    is_mod_title_key,
    load_config,
)
from core.database import DB_PATH, SkyrimTranslationDB
from core.dictionary_exporter import (
    TRANSLATED_MODS_DIR,
    cleanup_legacy_reports,
    export_mod_dictionary,
    generate_master_index,
)
from core.esp_parser import parse_plugin_file
from core.esp_writer import rebuild_plugin_file
from core.hybrid_translator import HybridTranslator
from core.strings_parser import (
    parse_string_table_bytes,
    parse_translate_txt_bytes,
)
from core.strings_writer import (
    write_string_table_file,
    write_translate_txt,
)

CATALOG_PATH = PROJECT_ROOT / "data" / "mods_catalog.json"
DEFAULT_MODS_DIR = get_mo2_mods_dir()
UA_PATCH_DIR = get_output_patch_dir()
DEFAULT_PROFILE_DIR = get_mo2_profile_dir()
DEFAULT_MODLIST_PATH = DEFAULT_PROFILE_DIR / "modlist.txt"

WAVE_CATEGORIES = {
    1: "ALREADY_LOCALIZED_OR_TM",
    2: "TRANSLATABLE_SMALL",
    3: "TRANSLATABLE_MEDIUM",
    4: "TRANSLATABLE_LARGE",
}

BLACKLISTED_MOD_PATTERNS = [
    "synthesis", "conflict_patch", "occlusion", "dyndolod", "texgen", "lux",
    "ks hairdos", "bdor hairs", "hairpack", "eyes of aber", "eyes of beauty",
    "high poly npc", "high poly head", "highpoly", "racemenumorphs",
    "expressive facegen", "3bbb.esp", "cbbe.esp", "caliente's beautiful bodies enhancer",
    "himbo.esp", "obody", "communityoverlays",
]

DUPLICATE_BASE_MODS_TO_SKIP = {
    "vigilant seae",
    "unslaad se",
    "glenmoril se",
    "dac0da",
}


def is_blacklisted_mod_or_plugin(name: str) -> bool:
    nl = name.lower()
    if nl in DUPLICATE_BASE_MODS_TO_SKIP:
        return True
    return any(p in nl for p in BLACKLISTED_MOD_PATTERNS)


def ensure_mo2_modlist_patch_enabled(
    modlist_path: Path = DEFAULT_MODLIST_PATH,
    patch_name: str = "[UA] Skyrim Translation Patch",
) -> bool:
    """Ensure +[UA] Skyrim Translation Patch is enabled at the highest priority in MO2 modlist.txt."""
    if not modlist_path.exists():
        return False
    text = modlist_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    found_idx = -1
    for idx, line in enumerate(lines):
        s = line.strip()
        if s in (f"+{patch_name}", f"-{patch_name}") or s.endswith(patch_name):
            found_idx = idx
            break

    if found_idx >= 0:
        lines[found_idx] = f"+{patch_name}"
    else:
        insert_idx = 1 if len(lines) > 1 and lines[0].startswith("#") else 0
        lines.insert(insert_idx, f"+{patch_name}")

    modlist_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def ensure_mo2_patch_meta_ini(patch_dir: Path = UA_PATCH_DIR) -> Path:
    """Ensure meta.ini exists for MO2 identification."""
    patch_dir.mkdir(parents=True, exist_ok=True)
    meta = patch_dir / "meta.ini"
    if not meta.exists():
        content = (
            "[General]\n"
            "modid=0\n"
            "version=1.0.0\n"
            "category=Translation\n"
            "comments=Automatic Ukrainian Translation Patch for Skyrim SE/AE\n"
        )
        meta.write_text(content, encoding="utf-8")
    return meta


def sync_mod_staging_to_mo2(staged_mod_dir: Path, patch_dir: Path = UA_PATCH_DIR) -> int:
    """
    Sync validated game files (*.esp, *.esm, *.esl, Interface/Translations/*.txt, Strings/*)
    from staged_mod_dir to the MO2 translation patch directory with Path Traversal protection.
    """
    output_root = Path(patch_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    ensure_mo2_patch_meta_ini(output_root)
    synced_count = 0

    for item in staged_mod_dir.iterdir():
        if item.is_file() and item.suffix.lower() in (".esp", ".esm", ".esl"):
            if is_blacklisted_mod_or_plugin(item.name):
                continue
            target_path = output_root / item.name
            if not target_path.resolve().is_relative_to(output_root):
                raise PathTraversalError(f"Blocked plugin sync traversal: {item.name}")
            ensure_safe_output_path(target_path, output_root)
            shutil.copy2(item, target_path)
            synced_count += 1

    staged_trans = staged_mod_dir / "Interface" / "Translations"
    if staged_trans.exists() and staged_trans.is_dir():
        patch_trans = output_root / "Interface" / "Translations"
        patch_trans.mkdir(parents=True, exist_ok=True)
        for tf in staged_trans.iterdir():
            if tf.is_file() and tf.suffix.lower() == ".txt":
                target_path = patch_trans / tf.name
                if not target_path.resolve().is_relative_to(output_root):
                    raise PathTraversalError(f"Blocked MCM sync traversal: {tf.name}")
                shutil.copy2(tf, target_path)
                synced_count += 1

    staged_str = staged_mod_dir / "Strings"
    if staged_str.exists() and staged_str.is_dir():
        patch_str = output_root / "Strings"
        patch_str.mkdir(parents=True, exist_ok=True)
        for sf in staged_str.iterdir():
            if sf.is_file() and any(sf.name.lower().endswith(x) for x in (".strings", ".dlstrings", ".ilstrings")):
                target_path = patch_str / sf.name
                if not target_path.resolve().is_relative_to(output_root):
                    raise PathTraversalError(f"Blocked Strings sync traversal: {sf.name}")
                shutil.copy2(sf, target_path)
                synced_count += 1

    return synced_count


class ModpackBatchOrchestrator:
    """Universal batch orchestrator for Mod Organizer 2 / Vortex modpacks with SIGINT checkpoint safety."""

    def __init__(
        self,
        catalog_path: Path = CATALOG_PATH,
        db_path: Path = DB_PATH,
        patch_dir: Path = UA_PATCH_DIR,
        staged_dir: Path = TRANSLATED_MODS_DIR,
        mods_dir: Path = DEFAULT_MODS_DIR,
    ):
        self.catalog_path = Path(catalog_path)
        self.db_path = Path(db_path)
        self.patch_dir = Path(patch_dir)
        self.staged_dir = Path(staged_dir)
        self.mods_dir = Path(mods_dir)
        self.checkpoint_mgr = CheckpointManager()
        self.db = SkyrimTranslationDB(self.db_path)
        self.translator = HybridTranslator(db=self.db)
        self.title_mode = get_mcm_title_mode()
        self._interrupted = False

        self.catalog: Dict[str, Any] = {"summary": {}, "mods": []}
        if self.catalog_path.exists():
            try:
                self.catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
            except Exception:
                self.catalog = {"summary": {}, "mods": []}

        self._install_sigint_handler()

    def _install_sigint_handler(self) -> None:
        """Install graceful SIGINT handler to flush checkpoints and WAL database on Ctrl+C."""
        try:
            def _on_sigint(signum: int, _frame: Any) -> None:
                self._interrupted = True
                try:
                    self.checkpoint_mgr.save()
                except Exception:
                    pass
            signal.signal(signal.SIGINT, _on_sigint)
        except ValueError:
            # Happens if instantiated in a non-main thread (e.g., GUI worker thread)
            pass

    def close(self) -> None:
        try:
            self.checkpoint_mgr.save()
        except Exception:
            pass
        if hasattr(self, "translator") and self.translator:
            self.translator.close()
        if hasattr(self, "db") and self.db:
            self.db.close()

    def discover_mods_from_directory(self, target_mods_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
        """Dynamically scan a Mod Organizer 2 `mods/` folder if no prebuilt catalog JSON is present."""
        base_dir = Path(target_mods_dir) if target_mods_dir else self.mods_dir
        if not base_dir.exists():
            return []
        discovered: List[Dict[str, Any]] = []
        for entry in sorted(base_dir.iterdir(), key=lambda x: x.name.lower()):
            if not entry.is_dir() or entry.name.startswith("[UA]") or is_blacklisted_mod_or_plugin(entry.name):
                continue
            plugins = [p.name for p in entry.iterdir() if p.is_file() and p.suffix.lower() in (".esp", ".esm", ".esl")]
            mcm_dir = entry / "Interface" / "Translations"
            mcms = [f"Interface/Translations/{m.name}" for m in mcm_dir.glob("*_english.txt")] if mcm_dir.exists() else []
            str_dir = entry / "Strings"
            strs = [f"Strings/{s.name}" for s in str_dir.glob("*_english.*strings")] if str_dir.exists() else []
            if plugins or mcms or strs:
                discovered.append({
                    "mod_name": entry.name,
                    "category": "TRANSLATABLE_SMALL",
                    "total_strings": 100,
                    "plugins": plugins,
                    "mcm_files": mcms,
                    "string_table_files": strs,
                })
        return discovered

    def get_wave_mods(self, wave_id: int | str) -> List[Dict[str, Any]]:
        """Retrieve translatable mods for a specific wave (1, 2, 3, 4, 4A, 4B, 4C)."""
        catalog_mods = self.catalog.get("mods", [])
        if not catalog_mods:
            catalog_mods = self.discover_mods_from_directory()

        w_str = str(wave_id).upper()
        if w_str in ("4A", "4B", "4C"):
            target_cat = "TRANSLATABLE_LARGE"
            mods = [
                m for m in catalog_mods
                if m.get("category") == target_cat
                and m.get("total_strings", 0) > 0
                and not is_blacklisted_mod_or_plugin(m["mod_name"])
            ]
            mods.sort(key=lambda x: x.get("total_strings", 0))
            if w_str == "4A":
                return [m for m in mods if m.get("total_strings", 0) <= 2500]
            if w_str == "4B":
                return [m for m in mods if 2500 < m.get("total_strings", 0) <= 7500]
            return [m for m in mods if m.get("total_strings", 0) > 7500]

        w_int = int(wave_id) if str(wave_id).isdigit() else None
        target_cat = WAVE_CATEGORIES.get(w_int) if w_int else str(wave_id)
        mods = [
            m for m in catalog_mods
            if m.get("category") == target_cat
            and m.get("total_strings", 0) > 0
            and not is_blacklisted_mod_or_plugin(m["mod_name"])
        ]
        if w_int == 4 or w_str == "4":
            mods.sort(key=lambda x: x.get("total_strings", 0))
        return mods

    def collect_mod_assets(self, m: Dict[str, Any]) -> Dict[str, Any]:
        """Inspect and parse a mod's plugins, loose/BSA MCM files, and string tables."""
        mod_name = m["mod_name"]
        if is_blacklisted_mod_or_plugin(mod_name):
            return {"mod_name": mod_name, "exists": False, "plugins": {}, "mcms": {}, "strings": {}, "all_texts": []}

        mod_dir = self.mods_dir / mod_name
        if not mod_dir.exists():
            return {"mod_name": mod_name, "exists": False, "plugins": {}, "mcms": {}, "strings": {}, "all_texts": []}

        plugins_data: Dict[str, Dict[str, Any]] = {}
        mcms_data: Dict[str, Tuple[str, Dict[str, str], bool]] = {}
        strings_data: Dict[str, Tuple[str, Dict[int, str], bool]] = {}
        all_texts: List[str] = []
        input_files_to_hash: List[Path] = []

        for p_name in m.get("plugins", []):
            if is_blacklisted_mod_or_plugin(p_name):
                continue
            p_path = mod_dir / p_name
            if p_path.exists():
                input_files_to_hash.append(p_path)
                try:
                    p_info = parse_plugin_file(p_path)
                    plugins_data[p_name] = p_info
                    for r in p_info.get("records", []):
                        txt = r.get("text")
                        if txt and txt.strip():
                            all_texts.append(txt)
                except Exception:
                    pass

        for mcm_ref in m.get("mcm_files", []):
            if "::" in mcm_ref:
                bsa_name, inner_path = mcm_ref.split("::", 1)
                bsa_path = mod_dir / bsa_name
                if bsa_path.exists():
                    input_files_to_hash.append(bsa_path)
                    try:
                        arc = BSAArchive(bsa_path)
                        matches = [e for e in arc.entries if e["path_lower"] == inner_path.lower()]
                        if matches:
                            raw_b = arc.extract_entry_bytes(matches[0])
                            entries = parse_translate_txt_bytes(raw_b)
                            out_name = Path(inner_path).name
                            mcms_data[mcm_ref] = (out_name, entries, True)
                            for v in entries.values():
                                if v and v.strip():
                                    all_texts.append(v)
                    except Exception:
                        pass
            else:
                loose_p = mod_dir / mcm_ref
                if loose_p.exists():
                    input_files_to_hash.append(loose_p)
                    try:
                        entries = parse_translate_txt_bytes(loose_p.read_bytes())
                        mcms_data[mcm_ref] = (loose_p.name, entries, False)
                        for v in entries.values():
                            if v and v.strip():
                                all_texts.append(v)
                    except Exception:
                        pass

        for str_ref in m.get("string_table_files", []):
            if "::" in str_ref:
                bsa_name, inner_path = str_ref.split("::", 1)
                bsa_path = mod_dir / bsa_name
                if bsa_path.exists():
                    input_files_to_hash.append(bsa_path)
                    try:
                        arc = BSAArchive(bsa_path)
                        matches = [e for e in arc.entries if e["path_lower"] == inner_path.lower()]
                        if matches:
                            raw_b = arc.extract_entry_bytes(matches[0])
                            ext = "." + inner_path.rsplit(".", 1)[-1].lower()
                            entries = parse_string_table_bytes(raw_b, is_length_prefixed=ext in (".dlstrings", ".ilstrings"))
                            out_name = Path(inner_path).name
                            strings_data[str_ref] = (out_name, entries, True)
                            for v in entries.values():
                                if v and v.strip():
                                    all_texts.append(v)
                    except Exception:
                        pass
            else:
                loose_p = mod_dir / str_ref
                if loose_p.exists():
                    input_files_to_hash.append(loose_p)
                    try:
                        ext = loose_p.suffix.lower()
                        entries = parse_string_table_bytes(loose_p.read_bytes(), is_length_prefixed=ext in (".dlstrings", ".ilstrings"))
                        strings_data[str_ref] = (loose_p.name, entries, False)
                        for v in entries.values():
                            if v and v.strip():
                                all_texts.append(v)
                    except Exception:
                        pass

        source_hash = sha256_of_files(list(dict.fromkeys(input_files_to_hash)), extra=self.title_mode)
        return {
            "mod_name": mod_name,
            "exists": True,
            "mod_dir": mod_dir,
            "source_hash": source_hash,
            "plugins": plugins_data,
            "mcms": mcms_data,
            "strings": strings_data,
            "all_texts": all_texts,
        }

    def translate_and_stage_mod(
        self,
        assets: Dict[str, Any],
        batch_translation_cache: Dict[str, Dict[str, Any]],
        wave_name: str,
        sync_to_mo2: bool = True,
    ) -> Dict[str, Any]:
        """Rebuild plugins, write MCMs and string tables, export HTML dictionary, and sync to MO2 patch."""
        mod_name = assets["mod_name"]
        staged_mod_dir = (self.staged_dir / mod_name).resolve()
        ensure_safe_output_path(staged_mod_dir, self.staged_dir.resolve())
        staged_mod_dir.mkdir(parents=True, exist_ok=True)
        cleanup_legacy_reports(staged_mod_dir)

        report_rows: List[Dict[str, str]] = []
        aliases = build_mod_aliases(mod_name)
        tm_hits = 0
        neural_hits = 0
        rebuilt_plugins = 0
        rebuilt_mcms = 0
        rebuilt_strings = 0

        for p_name, p_info in assets["plugins"].items():
            if is_blacklisted_mod_or_plugin(p_name):
                continue
            records = p_info.get("records", [])
            src_esp = assets["mod_dir"] / p_name
            dst_esp = staged_mod_dir / p_name

            if not records:
                if dst_esp.exists():
                    dst_esp.unlink()
                patch_esp = self.patch_dir / p_name
                if patch_esp.exists():
                    patch_esp.unlink()
                continue

            def _tr_cb(en_str: str, rec_type: str, sub_type: str, form_id: int, editor_id: str) -> str:
                cand = en_str
                if en_str in batch_translation_cache:
                    cand = batch_translation_cache[en_str]["uk_text"]
                else:
                    s_str = en_str.strip()
                    if s_str in batch_translation_cache:
                        cand = batch_translation_cache[s_str]["uk_text"]
                if is_mcm_quest_title(rec_type, sub_type, editor_id, en_str, aliases):
                    cand = apply_mcm_title_mode(en_str, cand, mode=self.title_mode)
                return cand

            res = rebuild_plugin_file(
                input_path=src_esp,
                output_path=dst_esp,
                translation_fn=_tr_cb,
            )
            if res.get("modified_records", 0) == 0:
                if dst_esp.exists():
                    dst_esp.unlink()
                patch_esp = self.patch_dir / p_name
                if patch_esp.exists():
                    patch_esp.unlink()
                continue

            rebuilt_plugins += 1
            reparsed = parse_plugin_file(dst_esp)
            for orig_r, new_r in zip(records, reparsed.get("records", [])):
                en_raw = orig_r.get("text") or ""
                uk_raw = new_r.get("text") or ""
                info = batch_translation_cache.get(en_raw) or batch_translation_cache.get(en_raw.strip()) or {"source": "TM"}
                src_label = info.get("source", "TM")
                if is_mcm_quest_title(orig_r["record_type"], orig_r["subrecord_type"], orig_r["editor_id"], en_raw.strip(), aliases):
                    src_label = f"MCM_TITLE ({self.title_mode})"
                elif src_label.startswith("TM"):
                    tm_hits += 1
                else:
                    neural_hits += 1

                report_rows.append({
                    "record_subrecord": f"{orig_r['record_type']}:{orig_r['subrecord_type']}",
                    "form_id": f"0x{orig_r['form_id']:08X}",
                    "editor_id": orig_r.get("editor_id", ""),
                    "source": src_label,
                    "en_text": en_raw.strip(),
                    "uk_text": uk_raw.strip(),
                })

        staged_trans_dir = staged_mod_dir / "Interface" / "Translations"
        for _ref_key, (out_name, entries, _is_bsa) in assets["mcms"].items():
            staged_trans_dir.mkdir(parents=True, exist_ok=True)
            uk_dict: Dict[str, str] = {}
            for k, en_val in entries.items():
                v_clean = en_val.strip()
                info = batch_translation_cache.get(en_val) or batch_translation_cache.get(v_clean) or {"uk_text": en_val, "source": "TM"}
                uk_val = info["uk_text"]
                src_label = info.get("source", "TM")
                if is_mod_title_key(k, v_clean, aliases):
                    uk_val = apply_mcm_title_mode(v_clean, uk_val, mode=self.title_mode)
                    src_label = f"MCM_TITLE ({self.title_mode})"
                elif src_label.startswith("TM"):
                    tm_hits += 1
                else:
                    neural_hits += 1

                uk_dict[k] = uk_val
                report_rows.append({
                    "record_subrecord": "MCM:TXT",
                    "form_id": k,
                    "editor_id": k.lstrip("$"),
                    "source": src_label,
                    "en_text": v_clean,
                    "uk_text": uk_val,
                })

            out_txt = staged_trans_dir / out_name
            write_translate_txt(uk_dict, out_txt)
            rebuilt_mcms += 1

        staged_strings_dir = staged_mod_dir / "Strings"
        for _ref_key, (out_name, entries, _is_bsa) in assets["strings"].items():
            staged_strings_dir.mkdir(parents=True, exist_ok=True)
            uk_str_dict: Dict[int, str] = {}
            for sid, en_val in entries.items():
                v_clean = en_val.strip()
                info = batch_translation_cache.get(en_val) or batch_translation_cache.get(v_clean) or {"uk_text": en_val, "source": "TM"}
                uk_val = info["uk_text"]
                src_label = info.get("source", "TM")
                if src_label.startswith("TM"):
                    tm_hits += 1
                else:
                    neural_hits += 1
                uk_str_dict[sid] = uk_val
                report_rows.append({
                    "record_subrecord": "STR:ENTRY",
                    "form_id": str(sid),
                    "editor_id": f"ID_{sid}",
                    "source": src_label,
                    "en_text": v_clean,
                    "uk_text": uk_val,
                })

            out_str = staged_strings_dir / out_name
            write_string_table_file(uk_str_dict, out_str)
            rebuilt_strings += 1

        dict_info = {}
        if report_rows:
            dict_info = export_mod_dictionary(
                mod_folder_name=mod_name,
                rows=report_rows,
                section=mod_name,
                mod_title=mod_name,
                out_dir=self.staged_dir,
            )

        synced_files = 0
        if sync_to_mo2:
            synced_files = sync_mod_staging_to_mo2(staged_mod_dir, patch_dir=self.patch_dir)
            ensure_mo2_modlist_patch_enabled()

        total_strings = len(report_rows)
        self.checkpoint_mgr.mark_completed(
            mod_name=mod_name,
            source_sha256=assets["source_hash"],
            total_strings=total_strings,
            summary={
                "wave": wave_name,
                "rebuilt_plugins": rebuilt_plugins,
                "rebuilt_mcms": rebuilt_mcms,
                "rebuilt_strings": rebuilt_strings,
                "tm_hits": tm_hits,
                "neural_hits": neural_hits,
                "synced_files": synced_files,
            },
            synced_to_mo2=bool(synced_files > 0 or not sync_to_mo2),
            wave=wave_name,
        )

        return {
            "mod_name": mod_name,
            "status": "COMPLETED",
            "total_strings": total_strings,
            "tm_hits": tm_hits,
            "neural_hits": neural_hits,
            "synced_files": synced_files,
            "dictionary_html": dict_info.get("html_path", ""),
        }

    def execute_wave(
        self,
        wave_id: int | str,
        limit: Optional[int] = None,
        sync_to_mo2: bool = True,
        on_progress: Optional[Callable[[str, float], None]] = None,
    ) -> Dict[str, Any]:
        """Execute full batch translation of a wave with graceful SIGINT checkpoint saving."""
        t0 = time.perf_counter()
        wave_name = f"Wave {wave_id}" if str(wave_id).isdigit() else str(wave_id)
        raw_mods = self.get_wave_mods(wave_id)
        if limit:
            raw_mods = raw_mods[:limit]

        pending_assets: List[Dict[str, Any]] = []
        already_done_count = 0
        total_candidate_strings: List[str] = []

        synced_any = False
        for m in raw_mods:
            if self._interrupted:
                break
            mname = m["mod_name"]
            assets = self.collect_mod_assets(m)
            if not assets["exists"] or not assets["all_texts"]:
                continue

            if self.checkpoint_mgr.is_completed(mname, assets["source_hash"]):
                staged_dir = self.staged_dir / mname
                if staged_dir.exists():
                    already_done_count += 1
                    if sync_to_mo2 and not self.checkpoint_mgr.is_synced(mname):
                        sync_mod_staging_to_mo2(staged_dir, patch_dir=self.patch_dir)
                        self.checkpoint_mgr.mark_synced(mname, True, save_now=False)
                        synced_any = True
                    continue

            pending_assets.append(assets)
            total_candidate_strings.extend(assets["all_texts"])

        if synced_any:
            self.checkpoint_mgr.save()

        unique_candidate_strings = list(dict.fromkeys(total_candidate_strings))
        batch_cache: Dict[str, Dict[str, Any]] = {}
        if unique_candidate_strings and not self._interrupted:
            cfg = load_config()
            workers = int(cfg.get("max_workers", 8))
            bsize = int(cfg.get("batch_size", 35))
            batch_cache = self.translator.translate_batch_deduplicated(
                strings=unique_candidate_strings,
                module_name=f"CUSTOM_{wave_name.replace(' ', '_').upper()}",
                record_type="FULL",
                max_workers=workers,
                batch_size=bsize,
                persist_to_db=True,
            )

        completed_mods: List[str] = []
        failed_mods: List[Tuple[str, str]] = []
        total_strings_processed = 0

        for idx, assets in enumerate(pending_assets, 1):
            if self._interrupted:
                self.checkpoint_mgr.save()
                break
            mname = assets["mod_name"]
            try:
                res = self.translate_and_stage_mod(
                    assets=assets,
                    batch_translation_cache=batch_cache,
                    wave_name=wave_name,
                    sync_to_mo2=sync_to_mo2,
                )
                completed_mods.append(mname)
                total_strings_processed += res["total_strings"]
                if on_progress:
                    on_progress(f"Translated {mname}", (idx / max(1, len(pending_assets))) * 100.0)
            except Exception as ex:
                self.checkpoint_mgr.mark_failed(
                    mod_name=mname,
                    source_sha256=assets.get("source_hash", ""),
                    total_strings=len(assets.get("all_texts", [])),
                    error_msg=str(ex),
                    wave=wave_name,
                )
                failed_mods.append((mname, str(ex)))

        cleanup_legacy_reports(self.staged_dir)
        index_path = generate_master_index()
        ensure_mo2_modlist_patch_enabled()

        elapsed = round(time.perf_counter() - t0, 2)
        return {
            "wave": wave_name,
            "elapsed_sec": elapsed,
            "total_target_mods": len(raw_mods),
            "already_completed": already_done_count,
            "processed_now": len(completed_mods),
            "failed_count": len(failed_mods),
            "total_strings_processed": total_strings_processed,
            "failed_mods": failed_mods[:10],
            "master_index_path": str(index_path),
        }


# Backward compatibility alias
BatchOrchestrator = ModpackBatchOrchestrator


def run_mass_batch_pipeline(
    waves: List[int | str] = (1, 2),
    sync_to_mo2: bool = True,
) -> Dict[str, Any]:
    """Execute mass batch translation for specified waves."""
    orchestrator = ModpackBatchOrchestrator()
    results: Dict[str, Any] = {}
    try:
        for w in waves:
            res = orchestrator.execute_wave(w, sync_to_mo2=sync_to_mo2)
            results[f"wave_{w}"] = res
    finally:
        orchestrator.close()
    return results


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    import argparse

    parser = argparse.ArgumentParser(description="Skyrim SE/AE Mass Batch Translation Pipeline")
    parser.add_argument("--wave", choices=["1", "2", "3", "4", "all"], default="1", help="Wave number to run (1, 2, 3, 4, or all)")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of mods for testing")
    parser.add_argument("--no-sync-mo2", action="store_true", help="Do not sync to MO2 patch")
    args = parser.parse_args()

    target_waves = [1, 2, 3, 4] if args.wave == "all" else [int(args.wave)]
    orch = ModpackBatchOrchestrator()
    try:
        for tw in target_waves:
            orch.execute_wave(tw, limit=args.limit, sync_to_mo2=not args.no_sync_mo2)
    finally:
        orch.close()
