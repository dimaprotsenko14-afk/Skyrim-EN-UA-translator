"""
UI Translation Builder (core/ui_translator.py).
Translates and deploys Interface/translate_english.txt and SkyUI interface translation files
into the output translation patch folder.
"""

import sys
from pathlib import Path
from typing import Dict, Optional

from core.config import (
    PROJECT_ROOT,
    ensure_safe_output_path,
    get_mo2_mods_dir,
    get_output_patch_dir,
)
from core.hybrid_translator import HybridTranslator
from core.strings_parser import parse_translate_txt_bytes
from core.strings_writer import write_translate_txt

UA_PATCH_DIR = get_output_patch_dir()
DEFAULT_MODS_DIR = get_mo2_mods_dir()

UI_CANONICAL_OVERRIDES: Dict[str, str] = {
    "$QUESTS": "КВЕСТИ",
    "$GENERAL STATS": "ЗАГАЛЬНА СТАТИСТИКА",
    "$SYSTEM": "СИСТЕМА",
    "$SAVE": "ЗБЕРЕГТИ",
    "$LOAD": "ЗАВАНТАЖИТИ",
    "$SETTINGS": "НАЛАШТУВАННЯ",
    "$CONTROLS": "КЕРУВАННЯ",
    "$QUIT": "ВИЙТИ",
    "$QUICKSAVE": "ШВИДКЕ ЗБЕРЕЖЕННЯ",
    "$Quicksave": "Швидке збереження",
    "$Quickload": "Швидке завантаження",
    "$Locations Discovered": "Відкрито локацій",
    "$Dungeons Cleared": "Зачищено підземель",
    "$Days Passed": "Пройшло днів",
    "$Hours Slept": "Годин сну",
    "$Hours Waiting": "Годин очікування",
    "$Standing Stones Found": "Знайдено каменів долі",
    "$Gold Found": "Знайдено золота",
    "$Most Gold Carried": "Найбільше золота з собою",
    "$Chests Looted": "Очищено скринь",
    "$Skill Increases": "Підвищень навичок",
    "$Skill Books Read": "Прочитано книг навичок",
    "$Food Eaten": "З'їдено їжі",
    "$Training Sessions": "Тренувань пройдено",
    "$Spells Learned": "Вивчено заклять",
    "$Dragon Souls Collected": "Зібрано душ драконів",
    "$Words Of Power Unlocked": "Розкрито слів сили",
    "$Words Of Power Learned": "Вивчено слів сили",
    "$Shouts Unlocked": "Розкрито криків",
    "$Shouts Learned": "Вивчено криків",
    "$Shouts Mastered": "Опановано криків",
    "$Times Shouted": "Використано криків",
    "$Day of": "День",
    "$PHOTO MODE": "ФОТОРЕЖИМ",
    "$PhotoMode": "Фоторежим",
    "$Photo Mode": "Фоторежим",
    "$ALL": "УСЕ",
    "$FILTER": "ФІЛЬТР",
    "$STOLEN": "КРАДЕНЕ",
    "$EQUIPPED": "СПОРЯДЖЕНО",
    "$ENCHANTED": "ЗАЧАРОВАНО",
    "$VALUE/WEIGHT": "ЦІНА/ВАГА",
    "$SPELL COST": "ВАРТІСТЬ ЧАРІВ",
    "$SHOUTS": "КРИКИ",
    "$EXPPROT": "ТЕПЛОТА",
    "$EXPPROTLOW": "Теплота",
    "$RAINPROT": "ЗАХИСТ ВІД ДОЩУ",
    "$RAINPROTLOW": "Захист від дощу",
    "$Javelin": "Дротик",
    "$Pike": "Піка",
    "$Halberd": "Алебарда",
    "$Rapier": "Рапіра",
    "$Quarterstaff": "Жердина",
    "$Whip": "Батіг",
    "$Katana": "Катана",
    "$Scythe": "Коса",
    "$Gun": "Пістоль",
    "$MCMMenuName": "SkyUI",
}


def build_and_deploy_ui_translations(
    mods_dir: Optional[Path] = None,
    patch_dir: Optional[Path] = None,
) -> Dict[str, int]:
    """Translate and deploy core Skyrim and SkyUI interface translation files."""
    target_mods_dir = Path(mods_dir) if mods_dir else get_mo2_mods_dir()
    output_root = (Path(patch_dir) if patch_dir else get_output_patch_dir()).resolve()
    ht = HybridTranslator()
    results: Dict[str, int] = {}

    try:
        p_ref = PROJECT_ROOT / "reference" / "Interface" / "translate_english.txt"
        d_ref = parse_translate_txt_bytes(p_ref.read_bytes()) if p_ref.exists() else {}

        # 1. Translate custom Interface/translate_english.txt if present in mods_dir
        custom_ui_files = list(target_mods_dir.glob("*/Interface/translate_english.txt")) if target_mods_dir.exists() else []
        if custom_ui_files:
            d_src = parse_translate_txt_bytes(custom_ui_files[0].read_bytes())
            out_ui: Dict[str, str] = {}
            for k, v in d_src.items():
                clean_k = k if k.startswith("$") else f"${k}"
                if clean_k in UI_CANONICAL_OVERRIDES:
                    out_ui[k] = UI_CANONICAL_OVERRIDES[clean_k]
                elif k in d_ref and d_ref[k].strip():
                    out_ui[k] = d_ref[k]
                else:
                    res = ht.translate(v, record_type="UI")
                    out_ui[k] = res.get("uk_text", v)

            patch_out1 = output_root / "Interface" / "translate_english.txt"
            ensure_safe_output_path(patch_out1, output_root)
            patch_out1.parent.mkdir(parents=True, exist_ok=True)
            write_translate_txt(out_ui, patch_out1)
            results["translate_english.txt"] = len(out_ui)

        # 2. Translate SkyUI custom overrides (e.g. Dear Diary Dark Mode)
        p_dd = target_mods_dir / "Dear Diary Dark Mode (warm text)" / "Interface" / "Translations" / "skyui_se_english.txt"
        p_skyui = PROJECT_ROOT / "translated_mods" / "SkyUI" / "skyui_se_english.txt"
        if p_dd.exists():
            d_dd = parse_translate_txt_bytes(p_dd.read_bytes())
            d_skyui = parse_translate_txt_bytes(p_skyui.read_bytes()) if p_skyui.exists() else {}
            out_dd: Dict[str, str] = {}
            for k, v in d_dd.items():
                clean_k = k if k.startswith("$") else f"${k}"
                if clean_k in UI_CANONICAL_OVERRIDES:
                    out_dd[k] = UI_CANONICAL_OVERRIDES[clean_k]
                elif k in d_skyui and d_skyui[k].strip():
                    out_dd[k] = d_skyui[k]
                elif k in d_ref and d_ref[k].strip():
                    out_dd[k] = d_ref[k]
                else:
                    res = ht.translate(v, record_type="UI")
                    out_dd[k] = res.get("uk_text", v)

            patch_out2 = output_root / "Interface" / "Translations" / "skyui_se_english.txt"
            ensure_safe_output_path(patch_out2, output_root)
            patch_out2.parent.mkdir(parents=True, exist_ok=True)
            write_translate_txt(out_dd, patch_out2)
            results["skyui_se_english.txt"] = len(out_dd)
    finally:
        ht.close()

    return results


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    build_and_deploy_ui_translations()
