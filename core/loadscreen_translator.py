"""
Loading Screen & LoreBox Translator (core/loadscreen_translator.py).
Translates LSCR:DESC (Loading Screens) across active plugins and LoreBox tooltip files,
deploying them into the output translation patch folder.
"""

import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from core.batch_orchestrator import (
    ensure_mo2_modlist_patch_enabled,
    ensure_mo2_patch_meta_ini,
)
from core.config import (
    PROJECT_ROOT,
    ensure_safe_output_path,
    get_game_data_dir,
    get_mo2_mods_dir,
    get_output_patch_dir,
)
from core.dictionary_exporter import export_mod_dictionary, generate_master_index
from core.esp_parser import is_cosmetic_or_morph_plugin, parse_plugin_file
from core.esp_writer import rebuild_plugin_file
from core.hybrid_translator import HybridTranslator
from core.strings_writer import write_translate_txt

CYR_RE = re.compile(r"[\u0400-\u04FF]")
ALPHA_RE = re.compile(r"[A-Za-z]")

TARGET_LSCR_PLUGINS = [
    "LoadScreenCompendium.esp",
    "BSHeartland.esm",
    "unofficial skyrim special edition patch.esp",
    "Inigo.esp",
    "Wyrmstooth.esp",
    "Mundus.esp",
    "Skyrim Unbound.esp",
    "Aetherius.esp",
    "PrvtI_HeavyArmory.esp",
    "PrvtI_LunarArmory.esp",
    "Sacrosanct - Vampires of Skyrim.esp",
    "Apocalypse - Magic of Skyrim.esp",
    "BSAssets.esm",
    "FEC.esp",
    "Skyrim Extended Cut - Saints and Seducers.esp",
    "Crossbow Integration.esp",
    "Adamant.esp",
    "MundusUSSEP.esp",
    "Awesome Artifacts.esp",
]


def find_source_plugin(
    p_name: str,
    mods_dir: Optional[Path] = None,
    patch_dir: Optional[Path] = None,
) -> Tuple[Path, str]:
    """Find source plugin path and mod folder name."""
    target_mods_dir = Path(mods_dir) if mods_dir else get_mo2_mods_dir()
    target_patch_dir = Path(patch_dir) if patch_dir else get_output_patch_dir()

    if (target_patch_dir / p_name).exists():
        mname = target_patch_dir.name
        if target_mods_dir.exists():
            for m in target_mods_dir.iterdir():
                if m.is_dir() and (m / p_name).exists() and m.name != target_patch_dir.name:
                    mname = m.name
                    break
        return target_patch_dir / p_name, mname

    if target_mods_dir.exists():
        for m in target_mods_dir.iterdir():
            if m.is_dir() and (m / p_name).exists():
                return m / p_name, m.name

    game_data = get_game_data_dir()
    if (game_data / p_name).exists():
        return game_data / p_name, "Game Data"

    raise FileNotFoundError(f"Plugin {p_name} not found in mods or Game Data")


def deploy_lorebox_translations(patch_dir: Optional[Path] = None) -> int:
    """Translate and deploy LoreBox tooltip files into the output patch directory."""
    output_root = (Path(patch_dir) if patch_dir else get_output_patch_dir()).resolve()
    lorebox_dir = output_root / "Interface" / "Translations"
    lorebox_dir.mkdir(parents=True, exist_ok=True)

    lorebox_data = {
        "LoreBox_english.txt": {
            "$LOREBOX_NAME": "LoreBox",
            "$LOREBOX_DELAY": "Затримка відображення (мс)",
            "$LOREBOX_DELAYH": "У мілісекундах: 1000 означає 1 секунду.",
        },
        "LoreBox FactionArmor_english.txt": {
            "$LoreBox_FactionArmor": '<font color="#8b0000">Броня фракції</font><font color="#facc96">: Можуть атакувати ворожі фракції.</font>',
        },
        "LoreBox RestricEquip_english.txt": {
            "$LoreBox_RestricEquipFemale": '<font color="#c99562">Лише для жінок</font>',
            "$LoreBox_RestricEquipMale": '<font color="#c99562">Лише для чоловіків</font>',
        },
    }

    count = 0
    for filename, entries in lorebox_data.items():
        out_path = lorebox_dir / filename
        ensure_safe_output_path(out_path, output_root)
        write_translate_txt(entries, out_path)
        count += len(entries)

    return count


def run_loadscreen_translation_and_rebuild(
    mods_dir: Optional[Path] = None,
    patch_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Scan, translate, and rebuild LSCR:DESC loading screens across active plugins."""
    t_start = time.time()
    output_root = (Path(patch_dir) if patch_dir else get_output_patch_dir()).resolve()
    ensure_mo2_modlist_patch_enabled()
    ensure_mo2_patch_meta_ini(output_root)

    plugin_records: Dict[str, Dict[str, Any]] = {}
    all_untranslated_strings: Set[str] = set()

    for p_name in TARGET_LSCR_PLUGINS:
        try:
            src_path, mod_folder = find_source_plugin(p_name, mods_dir=mods_dir, patch_dir=output_root)
            parsed = parse_plugin_file(src_path)
            lscr_recs = [r for r in parsed.get("records", []) if r.get("record_type") == "LSCR"]

            untr_recs = []
            for r in lscr_recs:
                txt = r.get("text")
                if txt and not CYR_RE.search(txt) and ALPHA_RE.search(txt):
                    all_untranslated_strings.add(txt.strip())
                    untr_recs.append(r)

            plugin_records[p_name] = {
                "src_path": src_path,
                "mod_folder": mod_folder,
                "all_records": parsed.get("records", []),
                "untr_recs": untr_recs,
                "total_lscr": len(lscr_recs),
            }
        except Exception:
            continue

    unique_count = len(all_untranslated_strings)
    ht = HybridTranslator()
    try:
        tr_map = ht.translate_batch_deduplicated(
            strings=list(all_untranslated_strings),
            module_name="LOAD_SCREEN",
            record_type="DESC",
            batch_size=35,
            persist_to_db=True,
        )

        staged_root = (PROJECT_ROOT / "staged_translations").resolve()
        rebuilt_plugins = []
        total_rebuilt_records = 0

        for p_name, p_data in plugin_records.items():
            untr = p_data["untr_recs"]
            if not untr:
                continue

            src_path = p_data["src_path"]
            mod_folder = p_data["mod_folder"]
            staged_dir = staged_root / mod_folder
            ensure_safe_output_path(staged_dir, staged_root)
            staged_dir.mkdir(parents=True, exist_ok=True)
            dst_path = staged_dir / p_name

            def _tr_cb(en_str: str, rec_type: str, sub_type: str, form_id: int, editor_id: str) -> str:
                if rec_type != "LSCR" or sub_type != "DESC":
                    return en_str
                if CYR_RE.search(en_str):
                    return en_str
                s_clean = en_str.strip()
                if en_str in tr_map:
                    return tr_map[en_str]["uk_text"]
                if s_clean in tr_map:
                    return tr_map[s_clean]["uk_text"]
                return en_str

            try:
                res = rebuild_plugin_file(
                    input_path=src_path,
                    output_path=dst_path,
                    translation_fn=_tr_cb,
                    skip_npc=is_cosmetic_or_morph_plugin(p_name),
                )
                mod_count = res.get("modified_records", 0)
                if mod_count > 0:
                    target_esp = output_root / p_name
                    ensure_safe_output_path(target_esp, output_root)
                    target_esp.write_bytes(dst_path.read_bytes())
                    rebuilt_plugins.append((p_name, mod_count, p_data["total_lscr"], mod_folder))
                    total_rebuilt_records += mod_count

                    reparsed = parse_plugin_file(target_esp)
                    report_rows = [
                        {
                            "record_subrecord": f"{r['record_type']}:{r['subrecord_type']}",
                            "form_id": f"0x{r['form_id']:08X}",
                            "editor_id": r.get("editor_id", ""),
                            "source": "LOAD_SCREEN",
                            "en_text": r.get("text") or "",
                            "uk_text": r.get("text") or "",
                        }
                        for r in reparsed.get("records", [])
                    ]
                    export_mod_dictionary(
                        mod_folder_name=mod_folder,
                        rows=report_rows,
                        section=mod_folder,
                        mod_title=mod_folder,
                        out_dir=staged_root,
                    )
            except Exception:
                continue

        lorebox_entries = deploy_lorebox_translations(output_root)
        generate_master_index()
    finally:
        ht.close()

    return {
        "status": "COMPLETED",
        "rebuilt_plugins": rebuilt_plugins,
        "total_rebuilt_records": total_rebuilt_records,
        "lorebox_entries": lorebox_entries,
        "unique_strings": unique_count,
        "elapsed_sec": round(time.time() - t_start, 2),
    }


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    run_loadscreen_translation_and_rebuild()
