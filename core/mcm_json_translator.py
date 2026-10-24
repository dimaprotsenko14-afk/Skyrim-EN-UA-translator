"""
MCM Helper JSON Translator (core/mcm_json_translator.py).
Translates UI display strings in MCM Helper config.json files into Ukrainian.
Preserves technical keys (id, type, source, value, action) and root modName/displayName.
"""

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.config import (
    PROJECT_ROOT,
    ensure_safe_output_path,
    get_mo2_mods_dir,
    get_output_patch_dir,
)
from core.hybrid_translator import HybridTranslator

UA_PATCH_DIR = get_output_patch_dir()
DEFAULT_MODS_DIR = get_mo2_mods_dir()


def translate_mcm_json_obj(
    obj: Any,
    translator: HybridTranslator,
    is_root: bool = True,
) -> Tuple[Any, int]:
    """
    Recursively translate UI display fields in MCM Helper JSON structures.
    Returns (translated_obj, translated_count).
    """
    translated_count = 0

    if isinstance(obj, dict):
        new_dict: Dict[str, Any] = {}
        for k, v in obj.items():
            if is_root and k in ("modName", "displayName"):
                new_dict[k] = v
                continue

            if k in ("id", "type", "source", "sourceType", "action", "actionType", "keybind", "default"):
                new_dict[k] = v
                continue

            if k in ("text", "description", "help", "pageDisplayName", "groupDisplayName") and isinstance(v, str):
                s = v.strip()
                if s and not s.startswith("$") and re.search(r"[A-Za-z]", s):
                    res = translator.translate(s, record_type="MCM")
                    uk = res.get("uk_text", s)
                    new_dict[k] = uk
                    translated_count += 1
                else:
                    new_dict[k] = v
                continue

            if k == "valueOptions" and isinstance(v, dict):
                sub_v, sub_cnt = translate_mcm_json_obj(v, translator, is_root=False)
                translated_count += sub_cnt
                new_dict[k] = sub_v
                continue

            if k == "options" and isinstance(v, list):
                new_opts = []
                for opt in v:
                    if isinstance(opt, str) and opt.strip() and not opt.startswith("$") and re.search(r"[A-Za-z]", opt):
                        res = translator.translate(opt, record_type="MCM")
                        new_opts.append(res.get("uk_text", opt))
                        translated_count += 1
                    else:
                        new_opts.append(opt)
                new_dict[k] = new_opts
                continue

            sub_res, sub_cnt = translate_mcm_json_obj(v, translator, is_root=False)
            translated_count += sub_cnt
            new_dict[k] = sub_res

        return new_dict, translated_count

    if isinstance(obj, list):
        new_list = []
        for item in obj:
            sub_res, sub_cnt = translate_mcm_json_obj(item, translator, is_root=False)
            translated_count += sub_cnt
            new_list.append(sub_res)
        return new_list, translated_count

    return obj, 0


def process_all_mcm_helper_configs(
    mods_dir: Optional[Path] = None,
    patch_dir: Optional[Path] = None,
) -> Dict[str, int]:
    """Discover and translate MCM Helper config.json files across the mods directory."""
    target_mods_dir = Path(mods_dir) if mods_dir else get_mo2_mods_dir()
    output_root = (Path(patch_dir) if patch_dir else get_output_patch_dir()).resolve()
    if not target_mods_dir.exists():
        return {}

    ht = HybridTranslator()
    results: Dict[str, int] = {}

    try:
        config_files: List[Path] = []
        for p in target_mods_dir.glob("*/MCM/Config/*/config.json"):
            config_files.append(p)
        for p in target_mods_dir.glob("*/SKSE/Plugins/MCM/Config/*/config.json"):
            config_files.append(p)

        unique_configs: Dict[str, Path] = {}
        for p in config_files:
            rel_key = f"{p.parent.name}/{p.name}"
            if rel_key not in unique_configs:
                unique_configs[rel_key] = p

        for _rel_key, cfg_path in unique_configs.items():
            try:
                raw_text = cfg_path.read_text(encoding="utf-8", errors="replace")
                data = json.loads(raw_text)
                translated_data, count = translate_mcm_json_obj(data, ht, is_root=True)

                if count > 0:
                    mod_subfolder = cfg_path.parent.name
                    out_p1 = output_root / "MCM" / "Config" / mod_subfolder / "config.json"
                    ensure_safe_output_path(out_p1, output_root)
                    out_p1.parent.mkdir(parents=True, exist_ok=True)
                    out_p1.write_text(json.dumps(translated_data, ensure_ascii=False, indent=2), encoding="utf-8")

                    out_p2 = output_root / "SKSE" / "Plugins" / "MCM" / "Config" / mod_subfolder / "config.json"
                    ensure_safe_output_path(out_p2, output_root)
                    out_p2.parent.mkdir(parents=True, exist_ok=True)
                    out_p2.write_text(json.dumps(translated_data, ensure_ascii=False, indent=2), encoding="utf-8")

                    results[mod_subfolder] = count
            except Exception:
                continue
    finally:
        ht.close()

    return results


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    process_all_mcm_helper_configs()
