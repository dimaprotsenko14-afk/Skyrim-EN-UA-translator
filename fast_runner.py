"""
Fast Interactive Console Runner with live tqdm progress bars for Skyrim SE/AE Mass Batch Translation
(fast_runner.py).
Usage:
  py fast_runner.py --wave 2
  py fast_runner.py --wave 3
  py fast_runner.py --wave all
  py fast_runner.py --sync
  py fast_runner.py --status
"""

import argparse
import sys
import webbrowser
from typing import Any, Dict, List, Optional

from tqdm import tqdm

from core.batch_orchestrator import (
    ModpackBatchOrchestrator,
    ensure_mo2_modlist_patch_enabled,
    sync_mod_staging_to_mo2,
)
from core.checkpoint_manager import CheckpointManager
from core.config import load_config
from core.database import DB_PATH, SkyrimTranslationDB
from core.dictionary_exporter import TRANSLATED_MODS_DIR, generate_master_index


def print_banner() -> None:
    print(r"""
===============================================================================
   ___  _  ____   ______ _____ __  __   ____ _____ _____  _ _____ ___  ____  
  / _ \| |/ /\ \ / /  _ \_   _|  \/  | / ___| ____|_   _|/ \_   _/ _ \|  _ \ 
 | | | | ' /  \ V /| |_) || | | |\/| | \___ \  _|   | | / _ \| || | | | |_) |
 | |_| | . \   | | |  _ < | | | |  | |  ___) | |___  | |/ ___ \ || |_| |  _ < 
  \__\_|_|\_\  |_| |_| \_\|_| |_|  |_| |____/|_____| |_/_/   \_\_| \___/|_| \_\
  
  UNIVERSAL UKRAINIAN TRANSLATION PIPELINE FOR SKYRIM SE/AE MODS & modpacks
===============================================================================
""", flush=True)


def show_status(cm: Optional[CheckpointManager] = None) -> None:
    if cm is None:
        cm = CheckpointManager()
    stats = cm.get_wave_stats()
    cfg = load_config()

    db = SkyrimTranslationDB(DB_PATH)
    db_rows = db.get_summary_counts()["total_rows"]
    db.close()

    print("\n[TRANSLATION STATUS & CHECKPOINT SUMMARY]", flush=True)
    print(f"  * Database (skyrim_tm.db)  : {db_rows:,} total translated rows (WAL mode)", flush=True)
    print(f"  * MCM Title Preservation   : {cfg.get('mcm_title_mode', 'keep_english')}", flush=True)
    print(f"  * Translation Engine       : Google GTX Neural POST + OPN/TPN Lore Glossary", flush=True)
    print(f"  * Safe Worker Concurrency  : {cfg.get('max_workers', 8)} workers | {cfg.get('batch_size', 35)} batch size", flush=True)
    print("-" * 75, flush=True)
    print(f"  {'Wave Name':<20} | {'Completed':<10} | {'Synced MO2':<10} | {'Failed':<8} | {'Total'}", flush=True)
    print("-" * 75, flush=True)
    for wave_name, s in sorted(stats.items()):
        total = s.get("total", 0)
        comp = s.get("COMPLETED", 0)
        synced = s.get("synced", 0)
        failed = s.get("FAILED", 0)
        print(f"  {wave_name:<20} | {comp:<10} | {synced:<10} | {failed:<8} | {total}", flush=True)
    print("-" * 75, flush=True)
    idx_path = TRANSLATED_MODS_DIR / "INDEX_DICTIONARY.html"
    if idx_path.exists():
        print(f"  * Master HTML Dictionary   : {idx_path}\n", flush=True)


def run_wave_with_tqdm(
    orchestrator: ModpackBatchOrchestrator,
    wave_id: int | str,
    limit: Optional[int] = None,
    sync_to_mo2: bool = True,
) -> Dict[str, Any]:
    wave_name = f"Wave {wave_id}" if str(wave_id).isdigit() else str(wave_id)
    raw_mods = orchestrator.get_wave_mods(wave_id)
    if limit:
        raw_mods = raw_mods[:limit]

    print(f"\n{'='*75}", flush=True)
    print(f"  STARTING [{wave_name.upper()}] ({len(raw_mods)} Target Mods)", flush=True)
    print(f"{'='*75}\n", flush=True)

    pending_assets: List[Dict[str, Any]] = []
    already_done = 0
    total_strings_raw: List[str] = []

    synced_any = False
    for m in raw_mods:
        mname = m["mod_name"]
        assets = orchestrator.collect_mod_assets(m)
        if not assets["exists"] or not assets["all_texts"]:
            continue

        if orchestrator.checkpoint_mgr.is_completed(mname, assets["source_hash"]):
            already_done += 1
            if sync_to_mo2 and not orchestrator.checkpoint_mgr.is_synced(mname):
                staged = orchestrator.staged_dir / mname
                if staged.exists():
                    sync_mod_staging_to_mo2(staged, orchestrator.patch_dir)
                    orchestrator.checkpoint_mgr.mark_synced(mname, True, save_now=False)
                    synced_any = True
            continue

        pending_assets.append(assets)
        total_strings_raw.extend(assets["all_texts"])

    if synced_any:
        orchestrator.checkpoint_mgr.save()

    unique_candidates = list(dict.fromkeys(total_strings_raw))
    print(f"  -> {already_done} already completed | {len(pending_assets)} mods to process", flush=True)
    print(f"  -> {len(total_strings_raw):,} total strings ({len(unique_candidates):,} unique candidate strings)\n", flush=True)

    if not pending_assets:
        print(f"  [OK] All {len(raw_mods)} mods in {wave_name} are already 100% completed!", flush=True)
        return {"status": "ALL_DONE", "wave": wave_name}

    batch_cache: Dict[str, Dict[str, Any]] = {}
    if unique_candidates:
        cfg = load_config()
        workers = int(cfg.get("max_workers", 8))
        bsize = int(cfg.get("batch_size", 35))

        tm = orchestrator.translator._get_bulk_tm_map()
        net_new = [s for s in unique_candidates if s.lower() not in tm and any(c.isalpha() for c in s)]
        tm_hits = len(unique_candidates) - len(net_new)

        print(f"  [2/3] Translating Strings ({tm_hits:,} TM hits, {len(net_new):,} net-new strings)...", flush=True)

        with tqdm(
            total=len(unique_candidates),
            desc="  Translating Strings",
            unit="str",
            ncols=80,
            colour="green",
        ) as pbar:
            pbar.update(tm_hits)

            def _on_batch(_b_idx: int, _total_b: int, done_count: int, failed_count: int) -> None:
                pbar.n = min(len(unique_candidates), tm_hits + done_count)
                pbar.set_postfix({"fails": failed_count})
                pbar.refresh()

            batch_cache = orchestrator.translator.translate_batch_deduplicated(
                strings=unique_candidates,
                module_name=f"CUSTOM_{wave_name.replace(' ', '_').upper()}",
                record_type="FULL",
                max_workers=workers,
                batch_size=bsize,
                persist_to_db=True,
                on_batch_done=_on_batch,
            )

    print("\n  [3/3] Rebuilding Plugins, MCMs & Single HTML Dictionaries...", flush=True)
    completed = 0
    failed = 0

    with tqdm(
        total=len(pending_assets),
        desc="  Staging & Rebuilding",
        unit="mod",
        ncols=80,
        colour="cyan",
    ) as pbar:
        for assets in pending_assets:
            mname = assets["mod_name"]
            pbar.set_postfix_str(mname[:30])
            try:
                orchestrator.translate_and_stage_mod(
                    assets=assets,
                    batch_translation_cache=batch_cache,
                    wave_name=wave_name,
                    sync_to_mo2=sync_to_mo2,
                )
                completed += 1
            except Exception as ex:
                failed += 1
                orchestrator.checkpoint_mgr.mark_failed(
                    mod_name=mname,
                    source_sha256=assets.get("source_hash", ""),
                    total_strings=len(assets.get("all_texts", [])),
                    error_msg=str(ex),
                    wave=wave_name,
                )
            pbar.update(1)

    generate_master_index()
    ensure_mo2_modlist_patch_enabled()

    print(f"\n{'='*75}", flush=True)
    print(f"  [{wave_name.upper()} COMPLETED] {completed} newly staged | {failed} failed | Total: {already_done + completed}/{len(raw_mods)}", flush=True)
    print(f"{'='*75}\n", flush=True)

    return {"status": "SUCCESS", "completed": completed, "failed": failed}


def interactive_menu() -> None:
    print_banner()
    orch = ModpackBatchOrchestrator()
    try:
        while True:
            show_status(orch.checkpoint_mgr)
            print("Select an action:", flush=True)
            print("  [1] Translate Wave 2 (Small & MCM mods)", flush=True)
            print("  [2] Translate Wave 3 (Medium Overhauls)", flush=True)
            print("  [3] Translate Wave 4 (Large & Complex Mods)", flush=True)
            print("  [4] Run ALL Remaining Waves (2, 3, 4)", flush=True)
            print("  [5] Sync All Staged Mods to [UA] Skyrim Translation Patch (MO2)", flush=True)
            print("  [6] Open Master HTML Dictionary in Web Browser", flush=True)
            print("  [0] Exit", flush=True)

            choice = input("\nEnter choice [1-6, 0]: ").strip()
            if choice == "0":
                print("Exiting. Translation state preserved in data/translation_checkpoint.json.")
                break
            elif choice == "1":
                run_wave_with_tqdm(orch, 2)
            elif choice == "2":
                run_wave_with_tqdm(orch, 3)
            elif choice == "3":
                run_wave_with_tqdm(orch, 4)
            elif choice == "4":
                for w in [2, 3, 4]:
                    run_wave_with_tqdm(orch, w)
            elif choice == "5":
                print("\nSyncing all staging mods to MO2 patch...", flush=True)
                total_synced = 0
                if orch.staged_dir.exists():
                    for mod_dir in orch.staged_dir.iterdir():
                        if mod_dir.is_dir() and not mod_dir.name.startswith("."):
                            total_synced += sync_mod_staging_to_mo2(mod_dir, orch.patch_dir)
                            orch.checkpoint_mgr.mark_synced(mod_dir.name, True)
                ensure_mo2_modlist_patch_enabled()
                print(f"[OK] Synced {total_synced} files into {orch.patch_dir}\n", flush=True)
            elif choice == "6":
                idx_path = TRANSLATED_MODS_DIR / "INDEX_DICTIONARY.html"
                if idx_path.exists():
                    webbrowser.open(idx_path.resolve().as_uri())
                    print(f"[OK] Opened {idx_path} in browser.\n", flush=True)
                else:
                    print("[!] Master dictionary does not exist yet. Run a wave first.\n", flush=True)
            else:
                print("Invalid choice, please select 1-6 or 0.\n", flush=True)
    finally:
        orch.close()


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Skyrim SE/AE Fast Interactive Batch Runner")
    parser.add_argument(
        "--wave",
        choices=["1", "2", "3", "4", "4A", "4B", "4C", "4a", "4b", "4c", "all"],
        default=None,
        help="Wave to run (1, 2, 3, 4, 4A, 4B, 4C, all)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Limit number of mods")
    parser.add_argument("--sync", action="store_true", help="Sync staged mods to MO2 patch")
    parser.add_argument("--status", action="store_true", help="Print checkpoint status and exit")
    args = parser.parse_args()

    if args.status:
        print_banner()
        show_status()
        return

    orch = ModpackBatchOrchestrator()
    try:
        if args.sync:
            print_banner()
            total_synced = 0
            if orch.staged_dir.exists():
                for mod_dir in orch.staged_dir.iterdir():
                    if mod_dir.is_dir() and not mod_dir.name.startswith("."):
                        total_synced += sync_mod_staging_to_mo2(mod_dir, orch.patch_dir)
                        orch.checkpoint_mgr.mark_synced(mod_dir.name, True)
            ensure_mo2_modlist_patch_enabled()
            print(f"[OK] Synced {total_synced} files into {orch.patch_dir}", flush=True)
            return

        if args.wave:
            print_banner()
            w_arg = args.wave.upper()
            if w_arg == "ALL":
                waves = [2, 3, "4A", "4B", "4C"]
            elif w_arg in ("4A", "4B", "4C"):
                waves = [w_arg]
            else:
                waves = [int(w_arg)]
            for w in waves:
                run_wave_with_tqdm(orch, w, limit=args.limit)
            return

        interactive_menu()
    finally:
        orch.close()


if __name__ == "__main__":
    main()
