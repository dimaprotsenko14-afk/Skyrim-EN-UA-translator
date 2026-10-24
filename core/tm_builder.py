"""
Reference Mod & Translation Memory Builder (core/tm_builder.py).
Builds local data/skyrim_tm.db and data/glossary_uk.json from a user-specified
English source (e.g., Skyrim Special Edition/Data) and Ukrainian reference mod
(e.g., downloaded OPN/TPN localization pack from Nexus Mods or Steam Workshop).
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from core.bsa_parser import BSAArchive
from core.config import PROJECT_ROOT, ensure_safe_output_path
from core.database import (
    CYRILLIC_RE,
    DB_PATH,
    GLOSSARY_PATH,
    SkyrimTranslationDB,
    is_clean_glossary_candidate,
)
from core.esp_parser import parse_plugin_file
from core.strings_parser import (
    extract_module_and_ext,
    parse_string_table_bytes,
    parse_strings_file,
    parse_translate_txt,
    parse_translate_txt_bytes,
)


@dataclass
class TMBuildRequest:
    """Request parameters for building local translation memory and glossary."""

    en_source_path: Path
    uk_reference_path: Path
    db_path: Path = DB_PATH
    glossary_path: Path = GLOSSARY_PATH
    extract_glossary: bool = True
    reset_db: bool = False


@dataclass
class TMBuildResult:
    """Results from building local translation memory and glossary."""

    strings_pairs_count: int
    plugin_pairs_count: int
    mcm_pairs_count: int
    total_tm_rows: int
    glossary_terms_count: int
    db_path: Path
    glossary_path: Path
    elapsed_sec: float


def _collect_string_tables(
    root_dir: Path, progress_cb: Optional[Callable[[str, float], None]] = None
) -> Dict[Tuple[str, str], Dict[int, str]]:
    """
    Find and parse all string tables (.strings, .dlstrings, .ilstrings)
    either loose on disk or packed inside .bsa archives.
    Returns: {(module_name_lower, ext_lower): {string_id: text}}
    """
    tables: Dict[Tuple[str, str], Dict[int, str]] = {}
    if not root_dir.exists():
        return tables

    # 1. Scan loose string files
    string_exts = {".strings", ".dlstrings", ".ilstrings"}
    loose_files = [f for f in root_dir.rglob("*") if f.is_file() and f.suffix.lower() in string_exts]
    for sf in loose_files:
        try:
            mod_name, ext = extract_module_and_ext(sf.name)
            parsed = parse_strings_file(sf)
            if parsed:
                tables[(mod_name, ext)] = parsed
        except Exception:
            continue

    # 2. Scan BSA archives for string tables
    bsa_files = [b for b in root_dir.rglob("*.bsa") if b.is_file()]
    for bsa_p in bsa_files:
        try:
            archive = BSAArchive(bsa_p)
            for entry in archive.entries:
                p_lower = entry["path_lower"]
                matched_ext = next((ext for ext in string_exts if p_lower.endswith(ext)), None)
                if matched_ext:
                    filename = Path(p_lower).name
                    mod_name, ext = extract_module_and_ext(filename)
                    if (mod_name, ext) not in tables:
                        raw_bytes = archive.extract_entry_bytes(entry)
                        is_len_prefixed = matched_ext in (".dlstrings", ".ilstrings")
                        parsed = parse_string_table_bytes(raw_bytes, is_length_prefixed=is_len_prefixed)
                        if parsed:
                            tables[(mod_name, ext)] = parsed
        except Exception:
            continue

    return tables


def _collect_ui_translation_files(root_dir: Path) -> Dict[str, Dict[str, str]]:
    """
    Find and parse interface translate_*.txt and MCM translations (*_english.txt).
    Returns: {relative_normalized_filename: {$key: value}}
    """
    files: Dict[str, Dict[str, str]] = {}
    if not root_dir.exists():
        return files

    # 1. Loose files
    for tf in root_dir.rglob("*.txt"):
        name_l = tf.name.lower()
        if "translate" in name_l or tf.parent.name.lower() == "translations":
            try:
                parsed = parse_translate_txt(tf)
                if parsed:
                    files[name_l] = parsed
            except Exception:
                continue

    # 2. Inside BSAs
    for bsa_p in root_dir.rglob("*.bsa"):
        try:
            archive = BSAArchive(bsa_p)
            for entry in archive.entries:
                p_lower = entry["path_lower"]
                if p_lower.endswith(".txt") and ("translate" in p_lower or "translations/" in p_lower or "translations\\" in p_lower):
                    name_l = Path(p_lower).name
                    if name_l not in files:
                        raw_bytes = archive.extract_entry_bytes(entry)
                        parsed = parse_translate_txt_bytes(raw_bytes)
                        if parsed:
                            files[name_l] = parsed
        except Exception:
            continue

    return files


def build_tm_from_reference(
    request: TMBuildRequest,
    progress_callback: Optional[Callable[[str, float], None]] = None,
) -> TMBuildResult:
    """
    Build data/skyrim_tm.db and data/glossary_uk.json from an English source directory
    and a Ukrainian reference localization folder.
    """
    start_time = time.time()
    en_root = request.en_source_path.resolve()
    uk_root = request.uk_reference_path.resolve()

    if not en_root.exists():
        raise FileNotFoundError(f"English source path does not exist: {en_root}")
    if not uk_root.exists():
        raise FileNotFoundError(f"Ukrainian reference path does not exist: {uk_root}")

    def report(msg: str, pct: float) -> None:
        if progress_callback:
            progress_callback(msg, pct)

    report("Scanning English string tables and BSA archives...", 5.0)
    en_tables = _collect_string_tables(en_root)
    report(f"Found {len(en_tables)} English string tables.", 15.0)

    report("Scanning Ukrainian reference string tables...", 20.0)
    uk_tables = _collect_string_tables(uk_root)
    report(f"Found {len(uk_tables)} Ukrainian reference string tables.", 30.0)

    # Initialize / open database
    request.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = SkyrimTranslationDB(request.db_path)
    if request.reset_db:
        db.init_schema(reset=True)
    else:
        db.init_schema(reset=False)

    rows_to_insert: List[Tuple[str, str, str, Optional[int], Optional[str], str, str, int]] = []
    strings_pairs_count = 0

    # 1. Pair binary string tables
    report("Aligning binary string tables by StringID...", 35.0)
    common_tables = set(en_tables.keys()).intersection(set(uk_tables.keys()))
    for mod_name, ext in sorted(common_tables):
        en_tbl = en_tables[(mod_name, ext)]
        uk_tbl = uk_tables[(mod_name, ext)]
        source_grp = "REFERENCE_MOD"

        for sid, en_str in en_tbl.items():
            uk_str = uk_tbl.get(sid)
            if not uk_str:
                continue
            en_clean = en_str.strip()
            uk_clean = uk_str.strip()
            if not en_clean or not uk_clean or en_clean == uk_clean:
                continue
            if not CYRILLIC_RE.search(uk_clean):
                continue

            rec_type = ext.upper()
            is_gloss = 1 if is_clean_glossary_candidate(en_clean, uk_clean, rec_type) else 0
            rows_to_insert.append((
                source_grp,
                mod_name,
                rec_type,
                sid,
                None,
                en_clean,
                uk_clean,
                is_gloss,
            ))
            strings_pairs_count += 1

    # 2. Pair plugin records (.esp / .esm / .esl)
    report("Scanning for matching plugins (.esp, .esm, .esl)...", 50.0)
    plugin_pairs_count = 0
    en_plugins: Dict[str, Path] = {
        p.name.lower(): p
        for p in en_root.rglob("*")
        if p.is_file() and p.suffix.lower() in (".esp", ".esm", ".esl")
    }
    uk_plugins: Dict[str, Path] = {
        p.name.lower(): p
        for p in uk_root.rglob("*")
        if p.is_file() and p.suffix.lower() in (".esp", ".esm", ".esl")
    }

    common_plugins = set(en_plugins.keys()).intersection(set(uk_plugins.keys()))
    for pl_name in sorted(common_plugins):
        en_pl = en_plugins[pl_name]
        uk_pl = uk_plugins[pl_name]
        try:
            en_parsed = parse_plugin_file(en_pl)
            uk_parsed = parse_plugin_file(uk_pl)
            en_record_map = {
                (r["base_form_id"], r["record_type"], r["subrecord_type"]): r.get("text")
                for r in en_parsed.get("records", [])
                if r.get("text")
            }
            for uk_rec in uk_parsed.get("records", []):
                uk_txt = uk_rec.get("text")
                if not uk_txt:
                    continue
                k = (uk_rec["base_form_id"], uk_rec["record_type"], uk_rec["subrecord_type"])
                en_txt = en_record_map.get(k)
                if not en_txt:
                    continue
                en_clean = en_txt.strip()
                uk_clean = uk_txt.strip()
                if not en_clean or not uk_clean or en_clean == uk_clean:
                    continue
                if not CYRILLIC_RE.search(uk_clean):
                    continue

                rec_label = f"{uk_rec['record_type']}:{uk_rec['subrecord_type']}"
                is_gloss = 1 if is_clean_glossary_candidate(en_clean, uk_clean, rec_label) else 0
                rows_to_insert.append((
                    "REFERENCE_MOD",
                    en_pl.stem.lower(),
                    rec_label,
                    None,
                    uk_rec["base_form_id"],
                    en_clean,
                    uk_clean,
                    is_gloss,
                ))
                plugin_pairs_count += 1
        except Exception:
            continue

    # 3. Pair UI / MCM translate files
    report("Scanning for MCM and UI translation files...", 70.0)
    mcm_pairs_count = 0
    en_ui = _collect_ui_translation_files(en_root)
    uk_ui = _collect_ui_translation_files(uk_root)
    common_ui = set(en_ui.keys()).intersection(set(uk_ui.keys()))

    for ui_fname in sorted(common_ui):
        en_dict = en_ui[ui_fname]
        uk_dict = uk_ui[ui_fname]
        for key, en_val in en_dict.items():
            uk_val = uk_dict.get(key)
            if not uk_val:
                continue
            en_clean = en_val.strip()
            uk_clean = uk_val.strip()
            if not en_clean or not uk_clean or en_clean == uk_clean:
                continue
            if not CYRILLIC_RE.search(uk_clean):
                continue
            is_gloss = 1 if is_clean_glossary_candidate(en_clean, uk_clean, "MESG") else 0
            rows_to_insert.append((
                "REFERENCE_MOD",
                ui_fname,
                "MCM_UI",
                None,
                None,
                en_clean,
                uk_clean,
                is_gloss,
            ))
            mcm_pairs_count += 1

    # 4. Batch insert into SQLite
    report(f"Writing {len(rows_to_insert):,} pairs to database...", 85.0)
    cur = db.conn.cursor()
    cur.executemany(
        """
        INSERT INTO translations (
            source_group, module_name, record_type, string_id, form_id, en_text, uk_text, is_glossary_term
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """,
        rows_to_insert,
    )
    db.conn.commit()
    db.build_indexes_and_fts()

    # 5. Extract Lore Glossary
    glossary_count = 0
    if request.extract_glossary:
        report("Extracting canonical lore glossary...", 92.0)
        glossary_votes: Dict[str, Counter] = defaultdict(Counter)
        en_canonical_case: Dict[str, str] = {}

        cur.execute(
            """
            SELECT en_text, uk_text, record_type
            FROM translations
            WHERE is_glossary_term = 1;
            """
        )
        for en_t, uk_t, r_type in cur.fetchall():
            en_c = en_t.strip()
            uk_c = uk_t.strip()
            low_k = en_c.lower()
            weight = 2
            if ":FULL" in (r_type or "") or r_type == "STRINGS":
                weight += 2
            glossary_votes[low_k][uk_c] += weight
            if low_k not in en_canonical_case:
                en_canonical_case[low_k] = en_c

        glossary_dict: Dict[str, str] = {}
        for low_k, counter in glossary_votes.items():
            best_uk = counter.most_common(1)[0][0]
            orig_cased = en_canonical_case.get(low_k, low_k)
            glossary_dict[orig_cased] = best_uk

        request.glossary_path.parent.mkdir(parents=True, exist_ok=True)
        request.glossary_path.write_text(
            json.dumps(glossary_dict, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        glossary_count = len(glossary_dict)

    summary = db.get_summary_counts()
    db.close()
    elapsed = round(time.time() - start_time, 2)

    report(
        f"Database initialized: {summary['total_rows']:,} rows, {glossary_count:,} glossary terms ({elapsed}s)",
        100.0,
    )

    return TMBuildResult(
        strings_pairs_count=strings_pairs_count,
        plugin_pairs_count=plugin_pairs_count,
        mcm_pairs_count=mcm_pairs_count,
        total_tm_rows=summary["total_rows"],
        glossary_terms_count=glossary_count,
        db_path=request.db_path,
        glossary_path=request.glossary_path,
        elapsed_sec=elapsed,
    )
