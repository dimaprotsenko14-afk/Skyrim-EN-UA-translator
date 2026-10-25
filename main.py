"""
Universal CLI Entry Point for Skyrim SE/AE English-to-Ukrainian Mod Translator (main.py).

Examples:
  py main.py --gui
  py main.py --target "./mods/SomeMod/SomeMod.esp" --output "./output/[UA] Skyrim Translation Patch"
  py main.py --scan-mods "./mods"
  py main.py --batch-wave all
  py main.py --status
"""

import argparse
import sys
from pathlib import Path

from core.batch_orchestrator import (
    ModpackBatchOrchestrator,
    ensure_mo2_modlist_patch_enabled,
    sync_mod_staging_to_mo2,
)
from core.bsa_parser import extract_all_english_from_bsas
from core.checkpoint_manager import CheckpointManager
from core.config import (
    get_mcm_title_mode,
    get_output_patch_dir,
    load_config_model,
)
from core.database import DB_PATH, SkyrimTranslationDB, build_translation_memory_and_glossary
from core.dictionary_exporter import cleanup_legacy_reports, generate_master_index
from core.llm_manager import ensure_ollama_and_model
from core.mod_translator import ModTranslationRequest, translate_mod_target
from core.modpack_scanner import scan_all_modpack_mods
from core.scanner import run_directory_audit
from core.sseat_inspector import run_sseat_inspection
from core.mod_translator import translate_modlist
from core.patch_builder import PatchBuildRequest, build_unified_mo2_patch
from core.tm_builder import TMBuildRequest, build_tm_from_reference
from core.vanilla_deployer import (
    build_vanilla_uk_from_extracted_en,
    deploy_to_game,
    rollback_from_game,
)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="Skyrim SE/AE Universal English-to-Ukrainian Mod Translator (CLI & GUI)"
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Launch the standalone Dark-Themed Desktop GUI (skyrim_translator_gui.py)",
    )
    parser.add_argument(
        "--target",
        type=str,
        default=None,
        help="Path to a single plugin (.esp/.esm/.esl), MCM file (*_english.txt), or Mod Folder to translate",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output patch directory (defaults to config.json -> output_patch_dir)",
    )
    parser.add_argument(
        "--mcm-title-mode",
        choices=["keep_english", "bilingual", "translate"],
        default=None,
        help="MCM mod title handling mode",
    )
    parser.add_argument(
        "--use-ollama",
        action="store_true",
        help="Enable local Ollama LLM fallback (Tier 3)",
    )
    parser.add_argument(
        "--scan-mods",
        type=str,
        nargs="?",
        const="",
        default=None,
        help="Scan a Mod Organizer 2 mods/ directory and generate data/mods_catalog.json",
    )
    parser.add_argument(
        "--batch-wave",
        choices=["1", "2", "3", "4", "all"],
        default=None,
        help="Run mass batch translation for Wave 1 (TM instant), 2 (Small), 3 (Medium), 4 (Large), or all",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume pending or failed mods across all waves",
    )
    parser.add_argument(
        "--sync-mo2",
        action="store_true",
        help="Sync all completed staging mods into [UA] Skyrim Translation Patch",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of mods to process in a wave",
    )
    parser.add_argument(
        "--build-db",
        action="store_true",
        help="Build local skyrim_tm.db and glossary_uk.json from reference localization",
    )
    parser.add_argument(
        "--en-source",
        type=str,
        default=None,
        help="Path to English source directory (e.g. Skyrim Special Edition/Data)",
    )
    parser.add_argument(
        "--uk-reference",
        type=str,
        default=None,
        help="Path to downloaded Ukrainian reference mod folder (e.g. OPN/TPN from Nexus/Steam)",
    )
    parser.add_argument(
        "--translate-modlist",
        type=str,
        default=None,
        help="Translate every mod folder inside a Mod Organizer 2 mods/ directory into translated_mods/<ModName>/",
    )
    parser.add_argument(
        "--build-patch",
        action="store_true",
        help="Build the unified [UA] Skyrim Translation Patch (translated mods + optional donor reference layer)",
    )
    parser.add_argument(
        "--no-donor",
        action="store_true",
        help="With --build-patch: do not include the donor/reference translation as the base layer",
    )
    parser.add_argument(
        "--donor",
        type=str,
        default=None,
        help="Donor/reference translation folder for --build-patch (defaults to config reference_dir)",
    )
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help="Recreate database from scratch during build-db",
    )
    parser.add_argument(
        "--setup-llm",
        action="store_true",
        help="Verify/install Ollama and pull qwen2.5:3b model",
    )
    parser.add_argument(
        "--compile-vanilla-uk",
        action="store_true",
        help="Compile extracted English files into Ukrainian inside build_vanilla_uk/Data/",
    )
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="Deploy build_vanilla_uk/Data/* into Skyrim Special Edition/Data/ with manifest tracking",
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="Roll back deployed Ukrainian files from Skyrim Special Edition/Data/ using deployed_manifest.json",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print translation checkpoint status, database statistics, and dictionary catalog summary",
    )
    parser.add_argument(
        "--cleanup-reports",
        action="store_true",
        help="Clean up any legacy .csv/.md reports and rebuild master INDEX_DICTIONARY.html",
    )
    args = parser.parse_args()

    if args.gui:
        from skyrim_translator_gui import launch_gui
        launch_gui()
        return

    if args.target:
        cfg = load_config_model()
        # Without --output the mod is stored in translated_mods/<ModName>/ ; with --output it is written to that patch folder.
        out_dir = Path(args.output) if args.output else None
        req = ModTranslationRequest(
            target_path=Path(args.target),
            output_patch_dir=out_dir,
            build_patch=out_dir is not None,
            mcm_title_mode=args.mcm_title_mode or cfg.mcm_title_mode,
            strict_whitelist_mode=cfg.strict_whitelist_mode,
            use_local_ollama=args.use_ollama or cfg.use_local_ollama,
            max_workers=cfg.max_workers,
            batch_size=cfg.batch_size,
        )
        res = translate_mod_target(req, progress_cb=lambda msg, pct: print(f"[{pct:5.1f}%] {msg}"))
        print("=" * 72)
        print(f"[COMPLETED] {res.mod_name}")
        print(f"  * Total Strings      : {res.total_strings:,} (TM: {res.tm_hits:,}, Neural: {res.neural_translated:,})")
        print(f"  * Output Patch Dir   : {res.output_patch_dir}")
        print(f"  * HTML Dictionary    : {res.html_dictionary_path}")
        print(f"  * Elapsed Time       : {res.elapsed_sec}s")
        print("=" * 72)
        return

    if args.translate_modlist:
        results = translate_modlist(
            Path(args.translate_modlist),
            mcm_title_mode=args.mcm_title_mode or load_config_model().mcm_title_mode,
            use_local_ollama=args.use_ollama,
            progress_cb=lambda msg, pct: print(f"[{pct:5.1f}%] {msg}"),
        )
        tot = sum(r.total_strings for r in results)
        tm = sum(r.tm_hits for r in results)
        print("=" * 72)
        print(f"[DONE] Modlist translated: {len(results)} mods, {tot:,} strings (TM hits: {tm:,})")
        print("=" * 72)
        return

    if args.build_patch:
        res = build_unified_mo2_patch(
            PatchBuildRequest(
                output_patch_dir=Path(args.output) if args.output else None,
                donor_dir=Path(args.donor) if args.donor else None,
                include_donor=not args.no_donor,
            ),
            progress_cb=lambda msg, pct: print(f"[{pct:5.1f}%] {msg}"),
        )
        print("=" * 72)
        print(f"[DONE] Unified patch: {res.patch_dir}")
        print(f"  * Donor files : {res.donor_files:,}")
        print(f"  * Mod files   : {res.mod_files:,} ({len(res.mods_merged)} mods)")
        print(f"  * Total       : {res.total_files:,} {res.by_kind}")
        print("=" * 72)
        return

    if args.status:
        ckpt = CheckpointManager()
        mode = get_mcm_title_mode()
        db = SkyrimTranslationDB(DB_PATH)
        summary = db.get_summary_counts()
        db.close()
        print("=" * 72)
        print(f"[STATUS] Translation Memory Rows : {summary['total_rows']:,}")
        print(f"[STATUS] MCM Mod Title Mode      : {mode}")
        mods = ckpt.state.get("mods", {})
        print(f"[STATUS] Tracked Mods in Ckpt    : {len(mods)}")
        idx = generate_master_index()
        print(f"[STATUS] Master Index            : {idx}")
        print("=" * 72)
        return

    if args.cleanup_reports:
        cleanup_legacy_reports()
        idx = generate_master_index()
        print(f"[DONE] Legacy reports cleaned up. Master index refreshed at {idx}")
        return

    if args.scan_mods is not None:
        custom_dir = Path(args.scan_mods) if args.scan_mods else None
        res = scan_all_modpack_mods(mods_dir=custom_dir, save_outputs=True)
        print(f"[DONE] Scanned modpack directory: {res['summary']}")
        return

    if args.batch_wave or args.resume:
        orch = ModpackBatchOrchestrator()
        try:
            if args.batch_wave == "all" or (args.resume and not args.batch_wave):
                target_waves = [1, 2, 3, 4]
            else:
                target_waves = [int(args.batch_wave)]
            for tw in target_waves:
                orch.execute_wave(tw, limit=args.limit, sync_to_mo2=True)
        finally:
            orch.close()
        return

    if args.sync_mo2:
        orch = ModpackBatchOrchestrator()
        try:
            synced_total = 0
            if orch.staged_dir.exists():
                for mod_dir in orch.staged_dir.iterdir():
                    if mod_dir.is_dir() and not mod_dir.name.startswith("."):
                        synced_total += sync_mod_staging_to_mo2(mod_dir, orch.patch_dir)
                        orch.checkpoint_mgr.mark_synced(mod_dir.name, True)
            ensure_mo2_modlist_patch_enabled()
            print(f"[DONE] Synced {synced_total} files from staging into {orch.patch_dir}")
        finally:
            orch.close()
        return

    if args.build_db:
        if args.uk_reference:
            req = TMBuildRequest(
                en_source_path=Path(args.en_source) if args.en_source else None,
                uk_reference_path=Path(args.uk_reference),
                reset_db=args.reset_db,
            )
            print("=" * 72)
            print("[BUILD-DB] Building Translation Memory from Reference Mod...")
            print(f"  * EN Source     : {req.en_source_path or '(auto-detect game Data folder)'}")
            print(f"  * UK Reference  : {req.uk_reference_path}")
            res = build_tm_from_reference(
                req, progress_callback=lambda msg, pct: print(f"[{pct:5.1f}%] {msg}")
            )
            print("=" * 72)
            print(f"[SUCCESS] Database Built Successfully!")
            print(f"  * String Table Pairs : {res.strings_pairs_count:,}")
            print(f"  * Plugin Pairs       : {res.plugin_pairs_count:,}")
            print(f"  * MCM UI Pairs       : {res.mcm_pairs_count:,}")
            print(f"  * Dictionary Pairs   : {res.dictionary_pairs_count:,}")
            print(f"  * Total TM Rows      : {res.total_tm_rows:,}")
            print(f"  * Glossary Terms     : {res.glossary_terms_count:,}")
            print(f"  * Database Path      : {res.db_path}")
            print(f"  * Glossary Path      : {res.glossary_path}")
            print(f"  * Elapsed Time       : {res.elapsed_sec}s")
            print("=" * 72)
            return
        elif args.en_source:
            print("[ERROR] --uk-reference (the downloaded Ukrainian reference mod) is required with --en-source.")
            return

        run_directory_audit(save_json=True)
        run_sseat_inspection(save_report=True)
        extract_all_english_from_bsas()
        build_translation_memory_and_glossary()
        return

    if args.setup_llm:
        ensure_ollama_and_model()
        return

    if args.compile_vanilla_uk:
        build_vanilla_uk_from_extracted_en()
        return

    if args.deploy:
        deploy_to_game()
        return

    if args.rollback:
        rollback_from_game()
        return

    parser.print_help()


if __name__ == "__main__":
    main()
