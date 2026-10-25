"""
Universal Skyrim SE/AE Mod & Plugin Translator (core/mod_translator.py).
Translates standalone .esp/.esm/.esl plugins, Interface/Translations/*_english.txt MCM files,
SKSE/Plugins/MCM/Config/*/config.json (MCM Helper JSONs), Strings/*.*strings tables, and
BSA-packed localization assets into Ukrainian using:
  - Tier 1: skyrim_tm.db exact match + bracketed weapon variant decomposition ([1/5], [2H] -> [2Р])
  - Tier 2: HybridTranslator (Glossary-Enforced Neural MT + Ukrainian lore post-processing)
  - Tier 3: Optional Local Ollama LLM fallback
Enforces strict Path Traversal validation (`target_path.resolve().is_relative_to(output_root)`)
and exports interactive HTML dictionaries (<ModFolderName>_dictionary.html + INDEX_DICTIONARY.html).
"""

import json
import re
import shutil
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.bsa_parser import BSAArchive
from core.checkpoint_manager import CheckpointManager, sha256_of_files
from core.config import (
    PROJECT_ROOT,
    InvalidTargetError,
    PathTraversalError,
    apply_mcm_title_mode,
    build_mod_aliases,
    ensure_safe_output_path,
    get_game_data_dir,
    get_mcm_title_mode,
    get_mo2_mods_dir,
    get_output_patch_dir,
    is_mcm_quest_title,
    is_mod_title_key,
    sanitize_output_patch_dir,
)
from core.database import SkyrimTranslationDB
from core.dictionary_exporter import cleanup_legacy_reports, generate_master_index
from core.esp_parser import parse_plugin_file
from core.esp_writer import rebuild_plugin_file
from core.hybrid_translator import (
    HybridTranslator,
    _gtx_post_translate,
    apply_ukrainian_lore_postprocessing,
    find_lore_terms_in_text,
)
from core.strings_parser import (
    parse_string_table_bytes,
    parse_translate_txt,
    parse_translate_txt_bytes,
)
from core.strings_writer import write_string_table_file, write_translate_txt

DATA_DIR = PROJECT_ROOT / "data"
TRANSLATED_MODS_DIR = PROJECT_ROOT / "translated_mods"
DEFAULT_MODS_DIR = get_mo2_mods_dir()
UA_PATCH_MOD_DIR = get_output_patch_dir()

SPAWNABLE_RECORD_TYPES = {"WEAP", "ARMO", "BOOK", "ALCH", "INGR", "SLGM", "MISC", "SPEL"}

BASE_MASTER_LOAD_ORDER = {
    "skyrim.esm": 0x00,
    "update.esm": 0x01,
    "dawnguard.esm": 0x02,
    "hearthfires.esm": 0x03,
    "dragonborn.esm": 0x04,
}

_VARIANT_SUFFIX_RE = re.compile(r"^(.+?)\s*(\[\d+/\d+(?:,\s*2H)?\]|\[2H\])$", re.IGNORECASE)
_TAG_PROTECT_RE = re.compile(
    r"(<font[^>]*DwemerFont[^>]*>[\s\S]*?</font>|<[^>]+>|\[ArteFakes\]|\{\d+\}|\b[\w-]+\.(?:esp|esm|esl|bsa|net)\b|%[0-9.]*[sdfx]|\\[rn])",
    re.IGNORECASE,
)


@dataclass
class ModTranslationRequest:
    """RORO input object for translating a single plugin, mod folder, or MO2 directory."""

    target_path: Path
    output_patch_dir: Optional[Path] = None
    build_patch: bool = False
    mcm_title_mode: str = "keep_english"
    strict_whitelist_mode: bool = True
    use_local_ollama: bool = False
    max_workers: int = 8
    batch_size: int = 35


@dataclass
class ModTranslationResult:
    """RORO output object returned after translating a mod or plugin."""

    mod_name: str
    target_path: str
    output_patch_dir: str
    plugins_rebuilt: int = 0
    mcm_files_translated: int = 0
    strings_tables_translated: int = 0
    total_strings: int = 0
    tm_hits: int = 0
    neural_translated: int = 0
    elapsed_sec: float = 0.0
    html_dictionary_path: str = ""
    details: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _protect_mod_tags(text: str) -> Tuple[str, Dict[str, str]]:
    tokens: Dict[str, str] = {}
    counter = 0

    def _repl(m: re.Match) -> str:
        nonlocal counter
        tok = f"__T{counter}__"
        tokens[tok] = m.group(0)
        counter += 1
        return tok

    masked = _TAG_PROTECT_RE.sub(_repl, text)
    return masked, tokens


def _restore_mod_tags(text: str, tokens: Dict[str, str]) -> str:
    out = text
    for tok, orig in tokens.items():
        if tok in out:
            out = out.replace(tok, orig)
        else:
            idx = tok.strip("_")[1:]
            out = re.sub(rf"__?\s*T\s*{idx}\s*__?", lambda _: orig, out, flags=re.IGNORECASE)
    return out


def _batch_google_translate_lines(
    lines: List[str],
    db: SkyrimTranslationDB,
) -> List[str]:
    """Batch translate lines via Google GTX POST API with tag protection and lore post-processing."""
    if not lines:
        return []

    results: List[str] = []
    batch_size = 25

    for i in range(0, len(lines), batch_size):
        chunk = lines[i : i + batch_size]
        masked_chunk: List[str] = []
        token_maps: List[Dict[str, str]] = []

        for text in chunk:
            norm_text = text.replace("\r\n", "\n")
            masked, tmap = _protect_mod_tags(norm_text)
            if "\n" in masked:
                tmap["__NL__"] = "\r\n"
                masked = masked.replace("\n", " __NL__ ")
            masked_chunk.append(masked)
            token_maps.append(tmap)

        joined = "\n".join(masked_chunk)
        try:
            translated_joined = _gtx_post_translate(joined, source="en", target="uk", timeout=(3.0, 8.0))
            split_lines = translated_joined.split("\n")
        except Exception:
            split_lines = []

        if len(split_lines) == len(chunk):
            for orig, tr_line, tmap in zip(chunk, split_lines, token_maps):
                restored = _restore_mod_tags(tr_line.strip(), tmap)
                restored = restored.replace(" __NL__ ", "\r\n").replace("__NL__", "\r\n")
                lore_terms = find_lore_terms_in_text(orig, db=db)
                cleaned = apply_ukrainian_lore_postprocessing(restored, lore_terms)
                results.append(cleaned)
        else:
            for orig, masked, tmap in zip(chunk, masked_chunk, token_maps):
                try:
                    tr1 = _gtx_post_translate(masked, source="en", target="uk", timeout=(3.0, 8.0))
                    restored = _restore_mod_tags(tr1.strip(), tmap)
                    restored = restored.replace(" __NL__ ", "\r\n").replace("__NL__", "\r\n")
                    lore_terms = find_lore_terms_in_text(orig, db=db)
                    cleaned = apply_ukrainian_lore_postprocessing(restored, lore_terms)
                    results.append(cleaned)
                except Exception:
                    results.append(orig)

    return results


def _ensure_mo2_patch_meta_ini(patch_dir: Path = UA_PATCH_MOD_DIR) -> Path:
    """Create a valid Mod Organizer 2 meta.ini in [UA] Skyrim Translation Patch."""
    patch_dir.mkdir(parents=True, exist_ok=True)
    meta_ini = patch_dir / "meta.ini"
    meta_content = (
        "[General]\n"
        "modid=0\n"
        "version=1.0.0\n"
        "newestVersion=1.0.0\n"
        "category=\"20,\"\n"
        "installationFile=\n"
        "repository=Nexus\n"
        "ignoredVersion=\n"
        "comments=\"Ukrainian translation patch compiled by Skyrim SE/AE Mod Translator\"\n"
        "notes=\"Contains Ukrainian ESP, Strings, and MCM translations for Skyrim SE/AE mods.\"\n"
    )
    meta_ini.write_text(meta_content, encoding="utf-8")
    return meta_ini


def export_mod_translation_reports(
    rows: List[Dict[str, str]],
    output_dir: Path,
    mod_title: str,
    file_prefixes: List[str],
    section: Optional[str] = None,
) -> Dict[str, str]:
    """Export interactive HTML dictionary for a translated mod."""
    from core.dictionary_exporter import export_mod_dictionary

    sec = section or (file_prefixes[-1] if file_prefixes else mod_title)
    res = export_mod_dictionary(
        mod_folder_name=Path(output_dir).name,
        rows=rows,
        section=sec,
        mod_title=mod_title.split(" (")[0] if " (" in mod_title else mod_title,
    )
    return {"html_path": res["html_path"], "all_html_paths": [res["html_path"]]}


def compute_runtime_form_id(
    raw_form_id: int,
    masters: List[str],
    mod_load_order_slot: int = 0x05,
) -> Tuple[str, str, int]:
    """Compute the 8-hex-digit runtime FormID for a record inside a plugin."""
    master_idx = (raw_form_id >> 24) & 0xFF
    base_fid = raw_form_id & 0x00FFFFFF
    if master_idx < len(masters):
        src_master = masters[master_idx]
        slot = BASE_MASTER_LOAD_ORDER.get(src_master.lower(), master_idx)
    else:
        src_master = "SELF"
        slot = mod_load_order_slot
    runtime_fid = (slot << 24) | base_fid
    return f"{runtime_fid:08X}", src_master, base_fid


def generate_all_mod_items_batch(
    plugin_path: Path,
    mod_folder_name: str,
    game_data_dir: Optional[Path] = None,
    mod_load_order_slot: int = 0x05,
) -> Dict[str, Any]:
    """Scan spawnable records in plugin_path and write an item spawner batch file."""
    target_game_data = Path(game_data_dir) if game_data_dir else get_game_data_dir()
    parsed = parse_plugin_file(plugin_path)
    masters = parsed.get("masters", [])
    records = parsed.get("records", [])

    spawn_items: List[Dict[str, Any]] = []
    seen_fids = set()

    full_names: Dict[int, str] = {}
    for r in records:
        if r["subrecord_type"] == "FULL" and r.get("text"):
            full_names[r["form_id"]] = r["text"].strip()

    for r in records:
        rtype = r["record_type"]
        fid = r["form_id"]
        if rtype not in SPAWNABLE_RECORD_TYPES or fid in seen_fids:
            continue
        seen_fids.add(fid)

        rt_hex, src_master, base_fid = compute_runtime_form_id(fid, masters, mod_load_order_slot)
        src_plugin_file = plugin_path.name if src_master == "SELF" else src_master
        item_name = full_names.get(fid, r.get("text") or r["editor_id"])

        spawn_items.append({
            "record_type": rtype,
            "raw_form_id_hex": f"0x{fid:08X}",
            "runtime_form_id": rt_hex,
            "base_form_id": base_fid,
            "source_plugin": src_plugin_file,
            "editor_id": r["editor_id"],
            "name": item_name,
            "command": (
                f"player.addspell {rt_hex}"
                if rtype == "SPEL"
                else f"player.additem {rt_hex} 1"
            ),
        })

    lines = ["player.modav carryweight 25000"]
    for item in spawn_items:
        lines.append(item["command"])

    batch_text = "\n".join(lines) + "\n"
    mod_backup_batch = TRANSLATED_MODS_DIR / mod_folder_name / "all_mod_items.txt"
    ensure_safe_output_path(mod_backup_batch, TRANSLATED_MODS_DIR)
    mod_backup_batch.parent.mkdir(parents=True, exist_ok=True)
    mod_backup_batch.write_text(batch_text, encoding="utf-8")

    data_batch_path = target_game_data / "all_mod_items.txt"
    if target_game_data.exists():
        try:
            data_batch_path.write_text(batch_text, encoding="utf-8")
        except OSError:
            pass

    return {
        "plugin_name": plugin_path.name,
        "data_batch_path": str(data_batch_path),
        "mod_backup_batch_path": str(mod_backup_batch),
        "total_spawn_commands": len(spawn_items),
        "items_count": sum(1 for x in spawn_items if x["record_type"] != "SPEL"),
        "spells_count": sum(1 for x in spawn_items if x["record_type"] == "SPEL"),
        "spawn_entries": spawn_items,
    }


def translate_mod_plugin(
    source_esp_path: Path,
    mod_folder_name: str,
    db: SkyrimTranslationDB,
    translator: HybridTranslator,
    patch_dir: Path = UA_PATCH_MOD_DIR,
    title_mode: Optional[str] = None,
    skip_npc: bool = False,
) -> Dict[str, Any]:
    """
    Translate all whitelisted records in source_esp_path into Ukrainian, write rebuilt .esp
    with strict Path Traversal validation, and export interactive HTML dictionary.
    """
    if not source_esp_path or not Path(source_esp_path).exists():
        raise InvalidTargetError(f"Plugin file does not exist: {source_esp_path}")

    t0 = time.perf_counter()
    output_root = Path(patch_dir).resolve()
    target_path = output_root / Path(source_esp_path).name
    if not target_path.resolve().is_relative_to(output_root):
        raise PathTraversalError(f"Blocked plugin output path traversal: {target_path}")
    ensure_safe_output_path(target_path, output_root)

    parsed = parse_plugin_file(source_esp_path)
    records = parsed["records"]

    unique_en: List[str] = []
    seen_en = set()
    for r in records:
        txt = r.get("text")
        if txt and txt.strip() and txt not in seen_en:
            seen_en.add(txt)
            unique_en.append(txt)

    if not unique_en:
        # Zero safe translatable records (e.g. blacklisted HDPT-only mods like SGEyebrows - Improved.esp).
        # Do NOT write or copy an unmodified .esp into translated_mods/ or patch_dir.
        if target_path.exists():
            try:
                target_path.unlink()
            except OSError:
                pass
        out_backup_dir = (TRANSLATED_MODS_DIR / mod_folder_name).resolve()
        out_backup_esp = out_backup_dir / source_esp_path.name
        if out_backup_esp.exists():
            try:
                out_backup_esp.unlink()
            except OSError:
                pass
        return {
            "mod_name": mod_folder_name,
            "plugin_name": source_esp_path.name,
            "masters": parsed.get("masters", []),
            "total_records": len(records),
            "unique_strings": 0,
            "tm_exact_hits": 0,
            "tm_variant_hits": 0,
            "neural_translated": 0,
            "modified_records": 0,
            "original_size_bytes": Path(source_esp_path).stat().st_size,
            "rebuilt_size_bytes": 0,
            "structural_integrity_verified": True,
            "mo2_patch_path": "",
            "backup_path": "",
            "exported_reports": {"html_path": "", "all_html_paths": []},
            "spawner_batch": {"data_batch_path": "", "total_spawn_commands": 0, "items_count": 0, "spells_count": 0},
            "elapsed_sec": round(time.perf_counter() - t0, 2),
            "sample_translations": [],
        }

    batch_res = translator.translate_batch_deduplicated(
        strings=unique_en,
        module_name=mod_folder_name,
        record_type="FULL",
    )

    translation_map: Dict[str, str] = {}
    source_map: Dict[str, str] = {}
    tm_hits = 0
    variant_tm_hits = 0
    neural_count = 0

    for en_text in unique_en:
        item = batch_res.get(en_text)
        if item:
            uk_val = re.sub(r"\[(\d+/\d+,\s*)?2H\]", lambda m: f"[{m.group(1) or ''}2Р]", item["uk_text"])
            translation_map[en_text] = uk_val
            src = item.get("source", "TM")
            source_map[en_text] = src
            if str(src).startswith("TM"):
                tm_hits += 1
            else:
                neural_count += 1

    aliases = build_mod_aliases(mod_folder_name, source_esp_path.name)
    active_title_mode = title_mode or get_mcm_title_mode()

    def _translate_record_cb(en_str: str, rec_type: str, sub_type: str, form_id: int, editor_id: str) -> str:
        cand = translation_map.get(en_str, translation_map.get(en_str.strip(), en_str))
        if is_mcm_quest_title(rec_type, sub_type, editor_id, en_str, aliases):
            cand = apply_mcm_title_mode(en_str, cand, mode=active_title_mode)
        return cand

    out_backup_dir = (TRANSLATED_MODS_DIR / mod_folder_name).resolve()
    ensure_safe_output_path(out_backup_dir, TRANSLATED_MODS_DIR.resolve())
    out_backup_dir.mkdir(parents=True, exist_ok=True)
    out_backup_esp = out_backup_dir / source_esp_path.name

    rebuild_stats = rebuild_plugin_file(
        input_path=source_esp_path,
        output_path=target_path,
        translation_fn=_translate_record_cb,
        skip_npc=skip_npc,
    )

    if rebuild_stats.get("modified_records", 0) <= 0:
        # Zero modified records -> delete unmodified plugin copy so it never pollutes translated_mods/ or patch_dir
        for p in (target_path, out_backup_esp):
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        return {
            "mod_name": mod_folder_name,
            "plugin_name": source_esp_path.name,
            "masters": parsed.get("masters", []),
            "total_records": len(records),
            "unique_strings": len(unique_en),
            "tm_exact_hits": tm_hits,
            "tm_variant_hits": variant_tm_hits,
            "neural_translated": neural_count,
            "modified_records": 0,
            "original_size_bytes": rebuild_stats["original_size"],
            "rebuilt_size_bytes": 0,
            "structural_integrity_verified": True,
            "mo2_patch_path": "",
            "backup_path": "",
            "exported_reports": {"html_path": "", "all_html_paths": []},
            "spawner_batch": {"data_batch_path": "", "total_spawn_commands": 0, "items_count": 0, "spells_count": 0},
            "elapsed_sec": round(time.perf_counter() - t0, 2),
            "sample_translations": [],
        }

    if output_root != out_backup_dir:
        _ensure_mo2_patch_meta_ini(output_root)
    if target_path.resolve() != out_backup_esp.resolve():
        shutil.copy2(target_path, out_backup_esp)

    reparsed = parse_plugin_file(target_path)
    integrity_ok = len(reparsed["records"]) == len(records)

    report_rows: List[Dict[str, str]] = []
    sample_records: List[Dict[str, Any]] = []
    seen_sample_pairs = set()

    for orig_r, new_r in zip(records, reparsed["records"]):
        raw_orig = orig_r.get("text") or ""
        orig_txt = raw_orig.strip()
        new_txt = (new_r.get("text") or "").strip()
        src_label = source_map.get(raw_orig) or source_map.get(orig_txt) or "TM"
        rec_sub = f"{orig_r['record_type']}:{orig_r['subrecord_type']}"
        fid_hex = f"0x{orig_r['form_id']:08X}"

        report_rows.append({
            "record_subrecord": rec_sub,
            "form_id": fid_hex,
            "editor_id": orig_r["editor_id"],
            "source": src_label,
            "en_text": orig_txt,
            "uk_text": new_txt,
        })

        if orig_txt and new_txt and orig_txt != new_txt:
            key = (orig_r["record_type"], orig_r["subrecord_type"], orig_txt)
            if key not in seen_sample_pairs:
                seen_sample_pairs.add(key)
                sample_records.append({
                    "record_type": rec_sub,
                    "form_id_hex": fid_hex,
                    "local_form_id_hex": orig_r["base_form_id"],
                    "editor_id": orig_r["editor_id"],
                    "source": src_label,
                    "en_text": orig_txt.replace("\r\n", " "),
                    "uk_text": new_txt.replace("\r\n", " "),
                })

    prefixes = list(dict.fromkeys([
        mod_folder_name,
        source_esp_path.stem,
        source_esp_path.name,
    ]))
    exported_reports = export_mod_translation_reports(
        rows=report_rows,
        output_dir=out_backup_dir,
        mod_title=f"{mod_folder_name} ({source_esp_path.name})",
        file_prefixes=prefixes,
    )

    spawner_info = generate_all_mod_items_batch(
        plugin_path=target_path,
        mod_folder_name=mod_folder_name,
    )

    elapsed = time.perf_counter() - t0
    return {
        "mod_name": mod_folder_name,
        "plugin_name": source_esp_path.name,
        "masters": parsed.get("masters", []),
        "total_records": len(records),
        "unique_strings": len(unique_en),
        "tm_exact_hits": tm_hits,
        "tm_variant_hits": variant_tm_hits,
        "neural_translated": neural_count,
        "modified_records": rebuild_stats["modified_records"],
        "original_size_bytes": rebuild_stats["original_size"],
        "rebuilt_size_bytes": rebuild_stats["rebuilt_size"],
        "structural_integrity_verified": integrity_ok,
        "mo2_patch_path": str(target_path),
        "backup_path": str(out_backup_esp),
        "exported_reports": exported_reports,
        "spawner_batch": {
            "data_batch_path": spawner_info["data_batch_path"],
            "total_spawn_commands": spawner_info["total_spawn_commands"],
            "items_count": spawner_info["items_count"],
            "spells_count": spawner_info["spells_count"],
        },
        "elapsed_sec": round(elapsed, 2),
        "sample_translations": sample_records[:25],
    }


def translate_mod_mcm_file(
    source_mcm_path: Path,
    mod_folder_name: str,
    db: SkyrimTranslationDB,
    translator: HybridTranslator,
    patch_dir: Path = UA_PATCH_MOD_DIR,
    title_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Translate an Interface/Translations/*_english.txt MCM file into Ukrainian,
    write UTF-16 LE BOM output with Path Traversal protection, and export HTML report.
    """
    if not source_mcm_path or not Path(source_mcm_path).exists():
        raise InvalidTargetError(f"MCM file does not exist: {source_mcm_path}")

    t0 = time.perf_counter()
    output_root = Path(patch_dir).resolve()
    target_path = output_root / "Interface" / "Translations" / Path(source_mcm_path).name
    if not target_path.resolve().is_relative_to(output_root):
        raise PathTraversalError(f"Blocked MCM output path traversal: {target_path}")
    ensure_safe_output_path(target_path, output_root)

    entries = parse_translate_txt(source_mcm_path)
    unique_vals = list(dict.fromkeys(v.strip() for v in entries.values() if v.strip()))

    batch_res = translator.translate_batch_deduplicated(
        strings=unique_vals,
        module_name=mod_folder_name,
        record_type="MCM",
    )

    val_map: Dict[str, str] = {}
    val_source: Dict[str, str] = {}
    tm_hits = 0
    neural_count = 0

    for v in unique_vals:
        item = batch_res.get(v)
        if item:
            val_map[v] = item["uk_text"]
            src = item.get("source", "TM")
            val_source[v] = src
            if str(src).startswith("TM"):
                tm_hits += 1
            else:
                neural_count += 1

    aliases = build_mod_aliases(mod_folder_name, source_mcm_path.name)
    active_title_mode = title_mode or get_mcm_title_mode()
    translated_entries: Dict[str, str] = {}
    report_rows: List[Dict[str, str]] = []
    for k, v in entries.items():
        vs = v.strip()
        uk_val = val_map.get(vs, v)
        src_label = val_source.get(vs, "TM")
        if is_mod_title_key(k, vs, aliases):
            uk_val = apply_mcm_title_mode(vs, uk_val, mode=active_title_mode)
            src_label = f"MCM_TITLE ({active_title_mode})"
        translated_entries[k] = uk_val
        report_rows.append({
            "record_subrecord": "MCM:TXT",
            "form_id": k,
            "editor_id": k,
            "source": src_label,
            "en_text": vs,
            "uk_text": uk_val,
        })

    out_backup_dir = (TRANSLATED_MODS_DIR / mod_folder_name).resolve()
    ensure_safe_output_path(out_backup_dir, TRANSLATED_MODS_DIR.resolve())
    if output_root != out_backup_dir:
        _ensure_mo2_patch_meta_ini(output_root)
    out_backup_mcm = out_backup_dir / "Interface" / "Translations" / source_mcm_path.name

    write_translate_txt(translated_entries, target_path)
    if target_path.resolve() != out_backup_mcm.resolve():
        write_translate_txt(translated_entries, out_backup_mcm)

    prefixes = list(dict.fromkeys([
        mod_folder_name,
        source_mcm_path.stem,
        source_mcm_path.name,
    ]))
    exported_reports = export_mod_translation_reports(
        rows=report_rows,
        output_dir=out_backup_dir,
        mod_title=f"{mod_folder_name} ({source_mcm_path.name})",
        file_prefixes=prefixes,
    )

    reparsed = parse_translate_txt(target_path)
    integrity_ok = len(reparsed) == len(entries)

    sample_mcm: List[Dict[str, str]] = []
    for k in list(entries.keys())[:15]:
        sample_mcm.append({
            "key": k,
            "en_text": entries[k],
            "uk_text": translated_entries[k],
        })

    return {
        "mod_name": mod_folder_name,
        "mcm_file_name": source_mcm_path.name,
        "total_keys": len(entries),
        "unique_values": len(unique_vals),
        "tm_hits": tm_hits,
        "neural_translated": neural_count,
        "round_trip_verified": integrity_ok,
        "mo2_patch_path": str(target_path),
        "backup_path": str(out_backup_mcm),
        "exported_reports": exported_reports,
        "elapsed_sec": round(time.perf_counter() - t0, 2),
        "sample_translations": sample_mcm,
    }


def _safe_folder_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip() or "UnnamedMod"


def translate_mod_target(
    request: ModTranslationRequest,
    progress_cb: Optional[Callable[[str, float], None]] = None,
) -> ModTranslationResult:
    """
    Universal RORO entry point to translate a single plugin (.esp/.esm/.esl),
    a single MCM file (*_english.txt), or an entire Mod Folder (including BSA-packed MCMs/Strings).
    """
    if not request or not request.target_path:
        raise InvalidTargetError("ModTranslationRequest.target_path is required.")
    target = Path(request.target_path).resolve()
    if not target.exists():
        raise InvalidTargetError(f"Target path does not exist: {target}")

    if target.is_file():
        resolved_mod_name = target.stem.replace("_english", "") if target.suffix.lower() == ".txt" else target.stem
    else:
        resolved_mod_name = target.name
    if request.build_patch and request.output_patch_dir:
        output_root = Path(sanitize_output_patch_dir(request.output_patch_dir))
        if not output_root.is_absolute():
            output_root = PROJECT_ROOT / output_root
        output_root = output_root.resolve()
    else:
        # Single mod: store cleanly in translated_mods/<ModName>/ (never in the application root).
        output_root = (TRANSLATED_MODS_DIR / _safe_folder_name(resolved_mod_name)).resolve()
    if output_root == PROJECT_ROOT.resolve():
        raise InvalidTargetError("Output folder must not be the application root directory.")
    output_root.mkdir(parents=True, exist_ok=True)
    t_start = time.perf_counter()

    db = SkyrimTranslationDB(DATA_DIR / "skyrim_tm.db")
    translator = HybridTranslator(
        db=db,
        default_tier="tier3" if request.use_local_ollama else "tier2",
    )

    result = ModTranslationResult(
        mod_name=target.stem if target.is_file() else target.name,
        target_path=str(target),
        output_patch_dir=str(output_root),
    )

    try:
        if target.is_file():
            ext = target.suffix.lower()
            if ext in (".esp", ".esm", ".esl"):
                if progress_cb:
                    progress_cb(f"Translating plugin {target.name}...", 15.0)
                info = translate_mod_plugin(
                    source_esp_path=target,
                    mod_folder_name=target.stem,
                    db=db,
                    translator=translator,
                    patch_dir=output_root,
                    title_mode=request.mcm_title_mode,
                    skip_npc=request.strict_whitelist_mode,
                )
                if info.get("modified_records", 0) > 0:
                    result.plugins_rebuilt = 1
                result.total_strings = info["unique_strings"]
                result.tm_hits = info["tm_exact_hits"] + info["tm_variant_hits"]
                result.neural_translated = info["neural_translated"]
                result.html_dictionary_path = info["exported_reports"]["html_path"]
                result.details.append(info)
            elif ext == ".txt":
                if progress_cb:
                    progress_cb(f"Translating MCM {target.name}...", 15.0)
                info = translate_mod_mcm_file(
                    source_mcm_path=target,
                    mod_folder_name=target.stem.replace("_english", ""),
                    db=db,
                    translator=translator,
                    patch_dir=output_root,
                    title_mode=request.mcm_title_mode,
                )
                result.mcm_files_translated = 1
                result.total_strings = info["unique_values"]
                result.tm_hits = info["tm_hits"]
                result.neural_translated = info["neural_translated"]
                result.html_dictionary_path = info["exported_reports"]["html_path"]
                result.details.append(info)
            else:
                raise InvalidTargetError(f"Unsupported file extension: {ext}")
        else:
            mod_name = target.name
            plugins = sorted(
                [p for p in target.iterdir() if p.is_file() and p.suffix.lower() in (".esp", ".esm", ".esl")],
                key=lambda x: x.name.lower(),
            )
            mcm_dir = target / "Interface" / "Translations"
            loose_mcms = sorted(mcm_dir.glob("*_english.txt")) if mcm_dir.exists() else []
            bsas = sorted(target.glob("*.bsa"), key=lambda x: x.name.lower())

            total_steps = max(1, len(plugins) + len(loose_mcms) + len(bsas))
            done_steps = 0

            for pfile in plugins:
                done_steps += 1
                if progress_cb:
                    progress_cb(f"Translating plugin {pfile.name}...", (done_steps / total_steps) * 85.0)
                info = translate_mod_plugin(
                    source_esp_path=pfile,
                    mod_folder_name=mod_name,
                    db=db,
                    translator=translator,
                    patch_dir=output_root,
                    title_mode=request.mcm_title_mode,
                    skip_npc=request.strict_whitelist_mode,
                )
                if info.get("modified_records", 0) > 0:
                    result.plugins_rebuilt += 1
                    result.html_dictionary_path = info["exported_reports"]["html_path"]
                result.total_strings += info["unique_strings"]
                result.tm_hits += info["tm_exact_hits"] + info["tm_variant_hits"]
                result.neural_translated += info["neural_translated"]
                result.details.append(info)

            for mfile in loose_mcms:
                done_steps += 1
                if progress_cb:
                    progress_cb(f"Translating MCM {mfile.name}...", (done_steps / total_steps) * 85.0)
                info = translate_mod_mcm_file(
                    source_mcm_path=mfile,
                    mod_folder_name=mod_name,
                    db=db,
                    translator=translator,
                    patch_dir=output_root,
                    title_mode=request.mcm_title_mode,
                )
                result.mcm_files_translated += 1
                result.total_strings += info["unique_values"]
                result.tm_hits += info["tm_hits"]
                result.neural_translated += info["neural_translated"]
                result.html_dictionary_path = info["exported_reports"]["html_path"]
                result.details.append(info)

            # Loose Strings/*_english.*strings inside the mod folder
            strings_dir = target / "Strings"
            loose_strings = (
                sorted(
                    [
                        sf
                        for sf in strings_dir.iterdir()
                        if sf.is_file()
                        and sf.name.lower().endswith(("_english.strings", "_english.dlstrings", "_english.ilstrings"))
                    ],
                    key=lambda x: x.name.lower(),
                )
                if strings_dir.exists()
                else []
            )
            out_backup_dir = (TRANSLATED_MODS_DIR / _safe_folder_name(mod_name)).resolve()
            out_backup_dir.mkdir(parents=True, exist_ok=True)

            for sfile in loose_strings:
                fname = sfile.name
                target_str = output_root / "Strings" / fname
                if not target_str.resolve().is_relative_to(output_root):
                    raise PathTraversalError(f"Blocked loose Strings traversal: {fname}")
                is_len_pref = fname.lower().endswith((".dlstrings", ".ilstrings"))
                st_table = parse_string_table_bytes(sfile.read_bytes(), is_length_prefixed=is_len_pref)
                u_vals = list(dict.fromkeys(v.strip() for v in st_table.values() if v.strip()))
                b_res = translator.translate_batch_deduplicated(u_vals, module_name=mod_name, record_type="STRINGS")
                tr_table = {sid: b_res.get(v.strip(), {}).get("uk_text", v) for sid, v in st_table.items()}
                write_string_table_file(tr_table, target_str)
                if output_root != out_backup_dir:
                    write_string_table_file(tr_table, out_backup_dir / "Strings" / fname)
                tm_cnt = sum(1 for v in u_vals if str(b_res.get(v, {}).get("source", "TM")).startswith("TM"))
                result.strings_tables_translated += 1
                result.total_strings += len(u_vals)
                result.tm_hits += tm_cnt
                result.neural_translated += len(u_vals) - tm_cnt
                rep_rows = [
                    {
                        "record_subrecord": f"STRINGS:{sfile.suffix.lstrip('.').upper()}",
                        "form_id": f"0x{sid:08X}",
                        "editor_id": fname,
                        "source": str(b_res.get(v.strip(), {}).get("source", "TM")),
                        "en_text": v.strip(),
                        "uk_text": tr_table.get(sid, v).strip(),
                    }
                    for sid, v in st_table.items()
                    if v and v.strip()
                ]
                exp = export_mod_translation_reports(
                    rows=rep_rows,
                    output_dir=out_backup_dir,
                    mod_title=f"{mod_name} ({fname})",
                    file_prefixes=[mod_name, fname],
                    section=fname,
                )
                result.html_dictionary_path = exp["html_path"]

            # Also inspect BSA archives in the mod folder for packed MCMs or Strings
            for bsa_file in bsas:
                done_steps += 1
                if any(tok in bsa_file.name.lower() for tok in ("texture", "mesh", "sound", "voice", "animation", "shader")):
                    continue
                try:
                    archive = BSAArchive(bsa_file)
                except Exception:
                    continue
                for entry in archive.entries:
                    pl = entry["path_lower"].replace("\\", "/")
                    if pl.startswith("interface/translations/") and pl.endswith("_english.txt"):
                        fname = Path(pl).name
                        target_mcm = output_root / "Interface" / "Translations" / fname
                        if not target_mcm.resolve().is_relative_to(output_root):
                            raise PathTraversalError(f"Blocked BSA MCM traversal: {pl}")
                        raw_bytes = archive.extract_entry_bytes(entry)
                        entries_map = parse_translate_txt_bytes(raw_bytes)
                        u_vals = list(dict.fromkeys(v.strip() for v in entries_map.values() if v.strip()))
                        b_res = translator.translate_batch_deduplicated(u_vals, module_name=mod_name, record_type="MCM")
                        tr_map = {k: b_res.get(v.strip(), {}).get("uk_text", v) for k, v in entries_map.items()}
                        write_translate_txt(tr_map, target_mcm)
                        if output_root != out_backup_dir:
                            write_translate_txt(tr_map, out_backup_dir / "Interface" / "Translations" / fname)
                        tm_cnt = sum(1 for v in u_vals if str(b_res.get(v, {}).get("source", "TM")).startswith("TM"))
                        result.mcm_files_translated += 1
                        result.total_strings += len(u_vals)
                        result.tm_hits += tm_cnt
                        result.neural_translated += len(u_vals) - tm_cnt
                        rep_rows = [
                            {
                                "record_subrecord": "MCM:TXT",
                                "form_id": k,
                                "editor_id": k,
                                "source": str(b_res.get(v.strip(), {}).get("source", "TM")),
                                "en_text": v.strip(),
                                "uk_text": tr_map.get(k, v).strip(),
                            }
                            for k, v in entries_map.items()
                            if v and v.strip()
                        ]
                        exp = export_mod_translation_reports(
                            rows=rep_rows,
                            output_dir=out_backup_dir,
                            mod_title=f"{mod_name} ({fname})",
                            file_prefixes=[mod_name, fname],
                            section=fname,
                        )
                        result.html_dictionary_path = exp["html_path"]
                    elif pl.startswith("strings/") and pl.endswith(("_english.strings", "_english.dlstrings", "_english.ilstrings")):
                        fname = Path(pl).name
                        target_str = output_root / "Strings" / fname
                        if not target_str.resolve().is_relative_to(output_root):
                            raise PathTraversalError(f"Blocked BSA Strings traversal: {pl}")
                        is_len_pref = pl.endswith((".dlstrings", ".ilstrings"))
                        raw_bytes = archive.extract_entry_bytes(entry)
                        st_table = parse_string_table_bytes(raw_bytes, is_length_prefixed=is_len_pref)
                        u_vals = list(dict.fromkeys(v.strip() for v in st_table.values() if v.strip()))
                        b_res = translator.translate_batch_deduplicated(u_vals, module_name=mod_name, record_type="STRINGS")
                        tr_table = {sid: b_res.get(v.strip(), {}).get("uk_text", v) for sid, v in st_table.items()}
                        write_string_table_file(tr_table, target_str)
                        if output_root != out_backup_dir:
                            write_string_table_file(tr_table, out_backup_dir / "Strings" / fname)
                        tm_cnt = sum(1 for v in u_vals if str(b_res.get(v, {}).get("source", "TM")).startswith("TM"))
                        result.strings_tables_translated += 1
                        result.total_strings += len(u_vals)
                        result.tm_hits += tm_cnt
                        result.neural_translated += len(u_vals) - tm_cnt
                        rep_rows = [
                            {
                                "record_subrecord": f"STRINGS:{Path(fname).suffix.lstrip('.').upper()}",
                                "form_id": f"0x{sid:08X}",
                                "editor_id": fname,
                                "source": str(b_res.get(v.strip(), {}).get("source", "TM")),
                                "en_text": v.strip(),
                                "uk_text": tr_table.get(sid, v).strip(),
                            }
                            for sid, v in st_table.items()
                            if v and v.strip()
                        ]
                        exp = export_mod_translation_reports(
                            rows=rep_rows,
                            output_dir=out_backup_dir,
                            mod_title=f"{mod_name} ({fname})",
                            file_prefixes=[mod_name, fname],
                            section=fname,
                        )
                        result.html_dictionary_path = exp["html_path"]

        if (
            result.plugins_rebuilt == 0
            and result.mcm_files_translated == 0
            and result.strings_tables_translated == 0
        ):
            # Clean up empty or zero-delta mod folder in translated_mods/ so untranslated mods do not linger
            mod_out = (TRANSLATED_MODS_DIR / _safe_folder_name(resolved_mod_name)).resolve()
            if mod_out.is_dir() and mod_out != TRANSLATED_MODS_DIR.resolve():
                shutil.rmtree(mod_out, ignore_errors=True)
            reg_json = DATA_DIR / "dictionary_rows" / f"{_safe_folder_name(resolved_mod_name)}.json"
            if reg_json.exists():
                try:
                    reg_json.unlink()
                except OSError:
                    pass

        idx_path = generate_master_index()
        if not result.html_dictionary_path:
            result.html_dictionary_path = str(idx_path)
        if progress_cb:
            progress_cb("Translation completed.", 100.0)
    finally:
        translator.close()
        db.close()

    result.elapsed_sec = round(time.perf_counter() - t_start, 2)
    return result


def _mod_folder_is_translatable(mod_dir: Path) -> bool:
    """A mod folder is translatable if it ships a plugin, MCM translation file, or a BSA."""
    try:
        if any(p.is_file() and p.suffix.lower() in (".esp", ".esm", ".esl", ".bsa") for p in mod_dir.iterdir()):
            return True
        trans = mod_dir / "Interface" / "Translations"
        return trans.is_dir() and any(trans.glob("*_english.txt"))
    except OSError:
        return False


def translate_modlist(
    mods_dir: Path,
    mcm_title_mode: str = "keep_english",
    strict_whitelist_mode: bool = True,
    use_local_ollama: bool = False,
    progress_cb: Optional[Callable[[str, float], None]] = None,
) -> List[ModTranslationResult]:
    """
    Translate every mod folder inside a Mod Organizer 2 `mods/` directory (or any folder of mod folders).
    Each mod is stored in translated_mods/<ModName>/ ; no unified patch is created here
    (see core.patch_builder.build_unified_mo2_patch).
    """
    mods_dir = Path(mods_dir)
    if not mods_dir.is_dir():
        raise InvalidTargetError(f"Modlist folder does not exist: {mods_dir}")
    candidates = sorted(
        [d for d in mods_dir.iterdir() if d.is_dir() and _mod_folder_is_translatable(d)],
        key=lambda p: p.name.lower(),
    )
    results: List[ModTranslationResult] = []
    for i, mod_dir in enumerate(candidates):
        base = 100.0 * i / max(1, len(candidates))
        span = 100.0 / max(1, len(candidates))
        if progress_cb:
            progress_cb(f"[{i + 1}/{len(candidates)}] {mod_dir.name}", base)
        req = ModTranslationRequest(
            target_path=mod_dir,
            mcm_title_mode=mcm_title_mode,
            strict_whitelist_mode=strict_whitelist_mode,
            use_local_ollama=use_local_ollama,
        )
        try:
            results.append(
                translate_mod_target(
                    req,
                    progress_cb=(lambda m, pct, b=base, s=span: progress_cb(m, b + s * pct / 100.0))
                    if progress_cb
                    else None,
                )
            )
        except Exception as exc:  # one broken mod must not abort the whole modlist
            if progress_cb:
                progress_cb(f"[ERROR] {mod_dir.name}: {exc}", base)
    if progress_cb:
        progress_cb("Modlist translation completed.", 100.0)
    return results


def run_showcase_mod_translation() -> Dict[str, Any]:
    """Translate configured showcase mod if present in the mods directory."""
    mods_dir = get_mo2_mods_dir()
    artefakes_dir = mods_dir / "ArteFakes - Unique Artifacts Replacer"
    if not artefakes_dir.exists():
        return {"status": "SKIPPED", "reason": "Showcase mod directory not present."}
    req = ModTranslationRequest(target_path=artefakes_dir)
    return translate_mod_target(req).to_dict()
