"""
SSE-AT Schema & Tag Protection Inspector (core/sseat_inspector.py).
Loads string_records.json and extracts Creation Engine tag-protection rules.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.config import PROJECT_ROOT, get_sseat_dir

SSEAT_DIR = get_sseat_dir()
REPORT_PATH = PROJECT_ROOT / "data" / "sseat_audit_report.json"
LOCAL_STRING_RECORDS_PATH = PROJECT_ROOT / "string_records.json"

TAG_PROTECTION_RULES = [
    {
        "name": "alias_tag",
        "pattern": r"<Alias=[^>]+>",
        "description": "Creation Engine quest alias replacement tag (e.g. <Alias=Player>)",
    },
    {
        "name": "global_tag",
        "pattern": r"<Global=[^>]+>",
        "description": "Creation Engine global variable tag (e.g. <Global=SpeechEasy>)",
    },
    {
        "name": "font_or_html_tag",
        "pattern": r"</?(?:font|br|b|i|u|img|p)\b[^>]*>",
        "description": "Scaleform / Book HTML formatting tags (<font face='$HandwrittenFont'>, <br>, etc.)",
    },
    {
        "name": "angle_bracket_token",
        "pattern": r"<(?:mag|dur|area|1|2|3|4|5|[^>]{1,40})>",
        "description": "Magic effect magnitude/duration tokens (<mag>, <dur>, <area>) and engine placeholders",
    },
    {
        "name": "pagebreak_tag",
        "pattern": r"\[pagebreak\]",
        "description": "Book page break control tag ([pagebreak])",
    },
    {
        "name": "printf_specifier",
        "pattern": r"%(?:\d+\$)?[-+#0 ]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[sdufxXegcp%]",
        "description": "Printf-style format specifiers (%s, %d, %.0f, %%)",
    },
    {
        "name": "crlf_newline",
        "pattern": r"\r\n|\n",
        "description": "Line break sequences preserved during translation",
    },
]


def load_sseat_string_records(sseat_dir: Optional[Path] = None) -> Dict[str, List[str]]:
    """Load string_records.json from local repository root or configured SSE-AT directory."""
    if LOCAL_STRING_RECORDS_PATH.exists():
        with open(LOCAL_STRING_RECORDS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    target_dir = Path(sseat_dir) if sseat_dir else get_sseat_dir()
    sr_path = target_dir / "res" / "string_records.json"
    if not sr_path.exists():
        return {}
    with open(sr_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_sseat_reference_records(sseat_dir: Optional[Path] = None) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Any]]:
    """Parse prebuilt plugin JSON database if available."""
    target_dir = Path(sseat_dir) if sseat_dir else get_sseat_dir()
    db_root = target_dir / "res" / "app" / "database"
    records_by_plugin: Dict[str, List[Dict[str, Any]]] = {}
    stats: Dict[str, Any] = {
        "languages_found": [],
        "has_native_ukrainian_folder": False,
        "plugins_per_language": {},
        "total_records_per_language": 0,
        "records_by_plugin_counts": {},
    }
    if not db_root.exists():
        return records_by_plugin, stats

    langs = sorted([d.name for d in db_root.iterdir() if d.is_dir()])
    stats["languages_found"] = langs
    stats["has_native_ukrainian_folder"] = any(l.lower() in ("ukrainian", "uk", "ukr") for l in langs)

    for lang in langs:
        lang_dir = db_root / lang
        json_files = sorted([f.name for f in lang_dir.iterdir() if f.is_file() and f.suffix.lower() == ".json"])
        stats["plugins_per_language"][lang] = len(json_files)

    if not langs:
        return records_by_plugin, stats

    ref_lang_dir = db_root / ("russian" if "russian" in langs else langs[0])
    total_recs = 0
    for jpath in sorted(ref_lang_dir.glob("*.json")):
        plugin_name = jpath.name[:-5]
        with open(jpath, "r", encoding="utf-8") as f:
            items = json.load(f)
        records_by_plugin[plugin_name.lower()] = items
        stats["records_by_plugin_counts"][plugin_name] = len(items)
        total_recs += len(items)

    stats["total_records_per_language"] = total_recs
    return records_by_plugin, stats


def extract_sseat_uk_pairs(
    records_by_plugin: Dict[str, List[Dict[str, Any]]],
    known_en_to_uk: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Align plugin database entries with known Ukrainian translations."""
    sseat_pairs: List[Dict[str, Any]] = []
    seen_keys = set()

    for plugin_lower, items in records_by_plugin.items():
        for entry in items:
            en_text = (entry.get("original") or "").strip()
            if not en_text:
                continue
            form_id_raw = entry.get("form_id") or ""
            rec_type = entry.get("type") or "UNKNOWN"
            editor_id = entry.get("editor_id") or ""

            uk_text = known_en_to_uk.get(en_text) or known_en_to_uk.get(en_text.lower())
            if not uk_text:
                continue

            fid_hex = form_id_raw.split("|")[0].strip() if "|" in form_id_raw else form_id_raw
            if len(fid_hex) == 8:
                try:
                    fid_int = int(fid_hex, 16) & 0x00FFFFFF
                    fid_norm = f"0x{fid_int:08X}"
                except ValueError:
                    fid_norm = fid_hex
            else:
                fid_norm = fid_hex

            is_missing_plugin = plugin_lower in ("_resourcepack.esl", "ccasvsse001-almsivi.esm")
            rec_prefix = rec_type.split()[0] if rec_type else ""
            is_core_meta = rec_prefix in ("CLAS", "RACE", "EYES", "AVIF", "SHOU", "WOOP")

            if is_missing_plugin or is_core_meta:
                dedup_key = (plugin_lower, fid_norm, rec_type, en_text)
                if dedup_key not in seen_keys:
                    seen_keys.add(dedup_key)
                    sseat_pairs.append({
                        "source_group": "SSEAT",
                        "module_name": plugin_lower,
                        "record_type": rec_type.replace(" ", ":"),
                        "string_id": None,
                        "form_id": fid_norm,
                        "editor_id": editor_id,
                        "en_text": entry.get("original"),
                        "uk_text": uk_text,
                    })

    return sseat_pairs


def run_sseat_inspection(
    known_en_to_uk: Optional[Dict[str, str]] = None,
    save_report: bool = True,
) -> Dict[str, Any]:
    """Parse string_records.json and optional prebuilt plugin databases."""
    string_records = load_sseat_string_records()
    records_by_plugin, db_stats = load_sseat_reference_records()

    sseat_pairs: List[Dict[str, Any]] = []
    if known_en_to_uk:
        sseat_pairs = extract_sseat_uk_pairs(records_by_plugin, known_en_to_uk)

    total_subrecord_rules = sum(len(v) for v in string_records.values())
    report = {
        "translatable_record_schema": {
            "source_file": "string_records.json",
            "record_types_count": len(string_records),
            "total_record_subrecord_pairs": total_subrecord_rules,
            "string_records": string_records,
        },
        "tag_protection_rules": TAG_PROTECTION_RULES,
        "prebuilt_plugin_database": db_stats,
        "imported_sseat_en_uk_pairs_count": len(sseat_pairs),
    }

    if save_report:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

    return {
        "report": report,
        "string_records": string_records,
        "records_by_plugin": records_by_plugin,
        "sseat_pairs": sseat_pairs,
    }


if __name__ == "__main__":
    run_sseat_inspection()
