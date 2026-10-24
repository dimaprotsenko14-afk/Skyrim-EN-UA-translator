"""
Universal Mod Organizer 2 / Vortex Modpack Scanner & Catalog Builder (core/modpack_scanner.py).
Scans a Mod Organizer 2 `mods/` directory, inspects .esp/.esm/.esl plugins, loose/BSA MCM files,
and string tables, calculates Translation Memory (TM) hit rates, and exports data/mods_catalog.json.
"""

import csv
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.bsa_parser import BSAArchive
from core.config import PROJECT_ROOT, get_mo2_mods_dir, get_mo2_profile_dir
from core.database import DB_PATH, SkyrimTranslationDB
from core.esp_parser import is_cosmetic_or_morph_plugin, parse_plugin_file
from core.strings_parser import parse_string_table_bytes, parse_translate_txt_bytes

DEFAULT_MODS_DIR = get_mo2_mods_dir()
DEFAULT_PROFILE_DIR = get_mo2_profile_dir()
CATALOG_JSON_PATH = PROJECT_ROOT / "data" / "mods_catalog.json"
CATALOG_CSV_PATH = PROJECT_ROOT / "data" / "mods_catalog.csv"


def scan_all_modpack_mods(
    mods_dir: Optional[Path] = None,
    save_outputs: bool = True,
) -> Dict[str, Any]:
    """
    Scan all mod folders inside `mods_dir`, inspect whitelisted records, MCMs, and string tables,
    and classify each mod into Wave 1..4 based on string volume and TM coverage.
    """
    target_mods_dir = Path(mods_dir) if mods_dir else get_mo2_mods_dir()
    if not target_mods_dir.exists():
        return {"total_mods_scanned": 0, "translatable_mods": 0, "mods": []}

    t0 = time.perf_counter()
    db = SkyrimTranslationDB(DB_PATH)
    tm_map = db.load_memory_lookup_map()
    db.close()

    catalog_entries: List[Dict[str, Any]] = []
    mod_folders = sorted(
        [d for d in target_mods_dir.iterdir() if d.is_dir() and not d.name.startswith("[UA]")],
        key=lambda x: x.name.lower(),
    )

    for mdir in mod_folders:
        mname = mdir.name
        if is_cosmetic_or_morph_plugin(mname):
            continue

        plugins: List[str] = []
        mcm_files: List[str] = []
        string_files: List[str] = []
        mod_strings: List[str] = []

        for item in sorted(mdir.iterdir(), key=lambda x: x.name.lower()):
            if item.is_file() and item.suffix.lower() in (".esp", ".esm", ".esl"):
                if is_cosmetic_or_morph_plugin(item.name):
                    continue
                plugins.append(item.name)
                try:
                    p_info = parse_plugin_file(item)
                    for r in p_info.get("records", []):
                        txt = r.get("text")
                        if txt and txt.strip():
                            mod_strings.append(txt.strip())
                except Exception:
                    pass
            elif item.is_file() and item.suffix.lower() == ".bsa":
                try:
                    arc = BSAArchive(item)
                    for entry in arc.entries:
                        pl = entry["path_lower"]
                        if pl.startswith("interface/translations/") and pl.endswith("_english.txt"):
                            mcm_files.append(f"{item.name}::{entry['path']}")
                            raw_b = arc.extract_entry_bytes(entry)
                            parsed_txt = parse_translate_txt_bytes(raw_b)
                            mod_strings.extend(v.strip() for v in parsed_txt.values() if v.strip())
                        elif pl.startswith("strings/") and pl.endswith(("_english.strings", "_english.dlstrings", "_english.ilstrings")):
                            string_files.append(f"{item.name}::{entry['path']}")
                            raw_b = arc.extract_entry_bytes(entry)
                            is_lp = pl.endswith((".dlstrings", ".ilstrings"))
                            st = parse_string_table_bytes(raw_b, is_length_prefixed=is_lp)
                            mod_strings.extend(v.strip() for v in st.values() if v.strip())
                except Exception:
                    pass

        trans_dir = mdir / "Interface" / "Translations"
        if trans_dir.exists():
            for tf in sorted(trans_dir.glob("*_english.txt")):
                rel_p = f"Interface/Translations/{tf.name}"
                mcm_files.append(rel_p)
                try:
                    parsed_txt = parse_translate_txt_bytes(tf.read_bytes())
                    mod_strings.extend(v.strip() for v in parsed_txt.values() if v.strip())
                except Exception:
                    pass

        str_dir = mdir / "Strings"
        if str_dir.exists():
            for sf in sorted(str_dir.glob("*_english.*strings")):
                rel_p = f"Strings/{sf.name}"
                string_files.append(rel_p)
                try:
                    is_lp = sf.suffix.lower() in (".dlstrings", ".ilstrings")
                    st = parse_string_table_bytes(sf.read_bytes(), is_length_prefixed=is_lp)
                    mod_strings.extend(v.strip() for v in st.values() if v.strip())
                except Exception:
                    pass

        if not mod_strings:
            continue

        unique_strs = list(dict.fromkeys(mod_strings))
        tm_hits = sum(1 for s in unique_strs if s.lower() in tm_map)
        net_new = len(unique_strs) - tm_hits

        if net_new == 0:
            category = "ALREADY_LOCALIZED_OR_TM"
        elif len(unique_strs) <= 100:
            category = "TRANSLATABLE_SMALL"
        elif len(unique_strs) <= 1000:
            category = "TRANSLATABLE_MEDIUM"
        else:
            category = "TRANSLATABLE_LARGE"

        catalog_entries.append({
            "mod_name": mname,
            "category": category,
            "total_strings": len(mod_strings),
            "unique_strings": len(unique_strs),
            "tm_hits": tm_hits,
            "net_new_strings": net_new,
            "plugins": plugins,
            "mcm_files": mcm_files,
            "string_table_files": string_files,
        })

    summary = {
        "total_mods_scanned": len(mod_folders),
        "translatable_mods": len(catalog_entries),
        "elapsed_sec": round(time.perf_counter() - t0, 2),
    }
    output = {"summary": summary, "mods": catalog_entries}

    if save_outputs:
        CATALOG_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        CATALOG_JSON_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(CATALOG_CSV_PATH, "w", encoding="utf-8", newline="") as cf:
            writer = csv.DictWriter(
                cf,
                fieldnames=["mod_name", "category", "total_strings", "unique_strings", "tm_hits", "net_new_strings"],
            )
            writer.writeheader()
            for entry in catalog_entries:
                writer.writerow({k: entry[k] for k in writer.fieldnames})

    return output


if __name__ == "__main__":
    scan_all_modpack_mods(save_outputs=True)
