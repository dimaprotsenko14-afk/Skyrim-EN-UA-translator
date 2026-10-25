"""
Standalone Desktop Launcher for the Skyrim SE/AE English-to-Ukrainian Mod Translator (skyrim_translator_gui.py).

Modern responsive dark dashboard built with `customtkinter` (pip install customtkinter):
  * Header: title, version badge, live local-database status, quick actions.
  * Left column: reference donor import (builds skyrim_tm.db) and translation target (single mod / modlist).
  * Right column: safety options and the unified MO2 / Vortex patch builder (modlist + donor merge).
  * Bottom: live log with progress bar, strings/sec and TM hit rate.
"""

import os
import queue
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Dict, Optional

try:
    import customtkinter as ctk
except ImportError:  # pragma: no cover - friendly message instead of a traceback
    sys.stderr.write(
        "customtkinter is required for the GUI.\n"
        "Install it with:  py -m pip install customtkinter\n"
        "(the CLI `py main.py --help` works without it)\n"
    )
    raise SystemExit(1)

from core.checkpoint_manager import CheckpointManager
from core.config import (
    PROJECT_ROOT,
    TranslatorConfig,
    get_translated_mods_dir,
    load_config_model,
    resolve_portable_path,
    sanitize_output_patch_dir,
    save_config_model,
)
from core.database import DB_PATH, GLOSSARY_PATH, SkyrimTranslationDB
from core.dictionary_exporter import generate_master_index
from core.mod_translator import (
    ModTranslationRequest,
    ModTranslationResult,
    translate_mod_target,
    translate_modlist,
)
from core.patch_builder import PatchBuildRequest, build_unified_mo2_patch
from core.tm_builder import TMBuildRequest, build_tm_from_reference

VERSION = "1.1.0"

BG_DARK = "#12141a"
BG_PANEL = "#1b1e27"
BG_INPUT = "#101218"
FG_TEXT = "#e8ecf4"
FG_MUTED = "#9aa3b5"
ACCENT_GOLD = "#e2b86b"
ACCENT_GOLD_HOVER = "#f0c97d"
ACCENT_GREEN = "#7ce3a3"
BORDER_COLOR = "#2c3140"

MODE_SINGLE = "Окремий мод"
MODE_MODLIST = "Модліст"


def _open_path(path: Path) -> None:
    """Open a file/folder with the OS default handler (no shell involved)."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as exc:
        messagebox.showerror("Помилка", f"Не вдалося відкрити:\n{path}\n{exc}")


class TranslatorApp:
    def __init__(self) -> None:
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        self.root = ctk.CTk(fg_color=BG_DARK)
        self.root.title(f"Skyrim SE/AE — Український локалізатор модів v{VERSION}")
        self.root.geometry("1280x820")
        self.root.minsize(1100, 720)
        try:
            self.root.after(50, lambda: self.root.state("zoomed"))  # launch maximized (Windows)
        except Exception:
            pass

        self.cfg: TranslatorConfig = load_config_model()
        self.log_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.is_running = False
        self.stop_requested = False

        ref_default = resolve_portable_path(self.cfg.reference_dir, "./reference")
        self.en_source_var = ctk.StringVar(value=self.cfg.game_data_dir if Path(self.cfg.game_data_dir).is_absolute() else "")
        self.uk_ref_var = ctk.StringVar(value=str(ref_default) if ref_default.exists() else "")
        self.mode_var = ctk.StringVar(value=MODE_MODLIST)
        self.target_path_var = ctk.StringVar(value=self.cfg.mo2_mods_dir if Path(self.cfg.mo2_mods_dir).is_absolute() else "")
        self.output_patch_var = ctk.StringVar(value=sanitize_output_patch_dir(self.cfg.output_patch_dir))
        self.keep_mcm_en_var = ctk.BooleanVar(value=self.cfg.mcm_title_mode == "keep_english")
        self.strict_whitelist_var = ctk.BooleanVar(value=self.cfg.strict_whitelist_mode)
        self.use_ollama_var = ctk.BooleanVar(value=self.cfg.use_local_ollama)
        self.include_donor_var = ctk.BooleanVar(value=True)
        self.db_badge_var = ctk.StringVar(value="База TM: … | Глосарій: …")
        self.status_var = ctk.StringVar(value="Готово до роботи.")
        self.speed_var = ctk.StringVar(value="Рядків/сек: 0.0")
        self.tm_rate_var = ctk.StringVar(value="TM Hit Rate: 0.0%")

        self._action_buttons: list = []
        self._build_layout()
        self._refresh_db_status()
        self._setup_sigint()
        self.root.after(100, self._poll_queue)

    # ------------------------------------------------------------------ layout helpers
    def _card(self, parent: Any, title: str) -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, fg_color=BG_PANEL, corner_radius=12, border_width=1, border_color=BORDER_COLOR)
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card, text=title, text_color=ACCENT_GOLD, font=ctk.CTkFont("Segoe UI", 15, "bold"), anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))
        return card

    def _path_row(self, card: Any, row: int, label: str, var: ctk.StringVar, browse_cmd: Any, btn_text: str = "Огляд…") -> None:
        ctk.CTkLabel(card, text=label, text_color=FG_MUTED, anchor="w", font=ctk.CTkFont("Segoe UI", 12)).grid(
            row=row, column=0, sticky="ew", padx=16, pady=(4, 0)
        )
        line = ctk.CTkFrame(card, fg_color="transparent")
        line.grid(row=row + 1, column=0, sticky="ew", padx=16, pady=(2, 6))
        line.grid_columnconfigure(0, weight=1)
        ctk.CTkEntry(
            line, textvariable=var, fg_color=BG_INPUT, border_color=BORDER_COLOR, text_color=FG_TEXT,
            font=ctk.CTkFont("Consolas", 12), height=34,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(
            line, text=btn_text, width=96, height=34, fg_color="#232734", hover_color="#2e3447",
            text_color=FG_TEXT, command=browse_cmd,
        ).grid(row=0, column=1)

    def _action_button(self, parent: Any, text: str, command: Any, primary: bool = False, **grid: Any) -> ctk.CTkButton:
        btn = ctk.CTkButton(
            parent, text=text, command=command, height=40,
            fg_color=ACCENT_GOLD if primary else "#232734",
            hover_color=ACCENT_GOLD_HOVER if primary else "#2e3447",
            text_color=BG_DARK if primary else FG_TEXT,
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
        )
        btn.grid(**grid)
        self._action_buttons.append(btn)
        return btn

    def _build_layout(self) -> None:
        root = self.root
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(1, weight=1)

        # ---- Header bar
        header = ctk.CTkFrame(root, fg_color=BG_PANEL, corner_radius=0, height=64)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(
            header, text="Skyrim SE/AE — Український локалізатор модів (OPN / TPN)",
            text_color=ACCENT_GOLD, font=ctk.CTkFont("Segoe UI", 20, "bold"),
        ).grid(row=0, column=0, padx=(20, 10), pady=14)
        ctk.CTkLabel(
            header, text=f" v{VERSION} ", fg_color=ACCENT_GOLD, text_color=BG_DARK, corner_radius=8,
            font=ctk.CTkFont("Segoe UI", 12, "bold"),
        ).grid(row=0, column=1, padx=6)
        ctk.CTkLabel(
            header, textvariable=self.db_badge_var, text_color=ACCENT_GREEN, fg_color=BG_INPUT, corner_radius=8,
            font=ctk.CTkFont("Consolas", 12, "bold"), padx=12, pady=4,
        ).grid(row=0, column=2, padx=14, sticky="w")
        ctk.CTkButton(
            header, text="📖 Відкрити Словник HTML", width=190, fg_color="#232734", hover_color="#2e3447",
            command=self._open_html_dictionary,
        ).grid(row=0, column=3, padx=6)
        ctk.CTkButton(
            header, text="📂 Відкрити Папку Патча", width=190, fg_color="#232734", hover_color="#2e3447",
            command=self._open_patch_folder,
        ).grid(row=0, column=4, padx=(6, 20))

        # ---- Body (two responsive columns)
        body = ctk.CTkFrame(root, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=18, pady=(14, 8))
        body.grid_columnconfigure((0, 1), weight=1, uniform="cols")
        body.grid_rowconfigure(0, weight=0)
        body.grid_rowconfigure(1, weight=1)

        left = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 9))
        left.grid_columnconfigure(0, weight=1)
        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(9, 0))
        right.grid_columnconfigure(0, weight=1)

        # Block 1: reference / donor
        c1 = self._card(left, "Блок 1: База перекладів та Мод-Донор (Reference Mod)")
        c1.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        self._path_row(c1, 1, "Англійська гра / BSA (Skyrim Special Edition\\Data) — можна залишити порожнім для автопошуку:",
                       self.en_source_var, self._browse_en_source)
        self._path_row(c1, 3, "Український переклад-еталон / донор (папка мода з Nexus / Steam):",
                       self.uk_ref_var, self._browse_uk_reference)
        self.import_btn = self._action_button(
            c1, "⚡ Імпортувати еталон у локальну базу (skyrim_tm.db)", self._start_build_db,
            primary=True, row=5, column=0, sticky="ew", padx=16, pady=(6, 16),
        )

        # Block 2: target
        c2 = self._card(left, "Блок 2: Ціль перекладу (Окремий мод або Модліст)")
        c2.grid(row=1, column=0, sticky="ew")
        self.mode_switch = ctk.CTkSegmentedButton(
            c2, values=[MODE_SINGLE, MODE_MODLIST], variable=self.mode_var,
            selected_color=ACCENT_GOLD, selected_hover_color=ACCENT_GOLD_HOVER, unselected_color="#232734",
            text_color=BG_DARK, command=lambda _v: None,
        )
        self.mode_switch.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 6))
        self._path_row(c2, 3, "Мод (.esp/.esm/.esl, MCM .txt, папка мода) або тека модліста (Mod Organizer 2 / mods):",
                       self.target_path_var, self._browse_target)
        self._path_row(c2, 5, "Папка єдиного патча (за замовчуванням ./output/[UA] Skyrim Translation Patch):",
                       self.output_patch_var, self._browse_output)
        self.translate_btn = self._action_button(
            c2, "▶ Перекласти ціль (результат → translated_mods/)", self._start_translation,
            primary=True, row=7, column=0, sticky="ew", padx=16, pady=(6, 16),
        )

        # Options card
        c3 = self._card(right, "Параметри безпеки та перекладу")
        c3.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        ctk.CTkCheckBox(
            c3, text="Keep MCM Titles in English (зберігати англійські назви модів у списку MCM)",
            variable=self.keep_mcm_en_var, text_color=FG_TEXT, fg_color=ACCENT_GOLD, hover_color=ACCENT_GOLD_HOVER,
        ).grid(row=1, column=0, sticky="w", padx=16, pady=5)
        ctk.CTkCheckBox(
            c3, text="Strict Crash-Proof Whitelist (пропускати CELL/WRLD/HDPT/RACE — рекомендовано)",
            variable=self.strict_whitelist_var, text_color=FG_TEXT, fg_color=ACCENT_GOLD, hover_color=ACCENT_GOLD_HOVER,
        ).grid(row=2, column=0, sticky="w", padx=16, pady=5)
        ctk.CTkCheckBox(
            c3, text="Ollama Fallback (локальний LLM qwen2.5:3b через localhost:11434)",
            variable=self.use_ollama_var, text_color=FG_TEXT, fg_color=ACCENT_GOLD, hover_color=ACCENT_GOLD_HOVER,
        ).grid(row=3, column=0, sticky="w", padx=16, pady=(5, 14))

        # Patch & donor merger card
        c4 = self._card(right, "Патч-Білдер: Модліст + Донор")
        c4.grid(row=1, column=0, sticky="ew")
        ctk.CTkLabel(
            c4, justify="left", anchor="w", text_color=FG_MUTED, wraplength=520,
            text="Єдиний патч збирається окремо: спочатку шар донора (Strings, Interface, .esp), "
                 "потім зверху — перекладені моди з translated_mods/.",
        ).grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 6))
        ctk.CTkCheckBox(
            c4, text="Додати файли мода-донора (Reference) у фінальний патч",
            variable=self.include_donor_var, text_color=FG_TEXT, fg_color=ACCENT_GOLD, hover_color=ACCENT_GOLD_HOVER,
        ).grid(row=2, column=0, sticky="w", padx=16, pady=5)
        self.patch_btn = self._action_button(
            c4, "📦 Створити єдиний патч для MO2 / Vortex (Модліст + Донор)", self._start_build_patch,
            primary=True, row=3, column=0, sticky="ew", padx=16, pady=(6, 8),
        )
        ctk.CTkButton(
            c4, text="💾 Зберегти конфігурацію", height=34, fg_color="#232734", hover_color="#2e3447",
            command=self._save_current_config,
        ).grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 16))

        # ---- Live log (bottom, spans both columns)
        log_card = ctk.CTkFrame(body, fg_color=BG_PANEL, corner_radius=12, border_width=1, border_color=BORDER_COLOR)
        log_card.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(14, 0))
        log_card.grid_columnconfigure(0, weight=1)
        log_card.grid_rowconfigure(2, weight=1)

        top = ctk.CTkFrame(log_card, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 4))
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            top, text="Журнал виконання (Live Log)", text_color=ACCENT_GOLD, anchor="w",
            font=ctk.CTkFont("Segoe UI", 15, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(top, textvariable=self.speed_var, text_color=ACCENT_GREEN, font=ctk.CTkFont("Consolas", 12, "bold")).grid(
            row=0, column=1, padx=14)
        ctk.CTkLabel(top, textvariable=self.tm_rate_var, text_color=ACCENT_GREEN, font=ctk.CTkFont("Consolas", 12, "bold")).grid(
            row=0, column=2, padx=14)
        ctk.CTkButton(top, text="Очистити лог", width=120, height=30, fg_color="#232734", hover_color="#2e3447",
                      command=self._clear_log).grid(row=0, column=3)

        prog = ctk.CTkFrame(log_card, fg_color="transparent")
        prog.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 6))
        prog.grid_columnconfigure(0, weight=1)
        self.progress_bar = ctk.CTkProgressBar(prog, progress_color=ACCENT_GOLD, fg_color=BG_INPUT, height=14)
        self.progress_bar.grid(row=0, column=0, sticky="ew")
        self.progress_bar.set(0.0)
        ctk.CTkLabel(prog, textvariable=self.status_var, text_color=FG_MUTED, anchor="w").grid(
            row=1, column=0, sticky="ew", pady=(4, 0))

        self.log_text = ctk.CTkTextbox(
            log_card, fg_color=BG_INPUT, text_color=FG_TEXT, font=ctk.CTkFont("Consolas", 12), wrap="word"
        )
        self.log_text.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 14))
        self.log_text.configure(state="disabled")

        self._append_log(f"Skyrim SE/AE EN -> UA Translator v{VERSION} запущено.")
        self._append_log("1) Імпортуйте еталон (донор) у локальну базу, 2) оберіть мод або модліст, 3) за потреби зберіть єдиний патч.")

    # ------------------------------------------------------------------ misc UI actions
    def _setup_sigint(self) -> None:
        def _handler(_sig: int, _frame: Any) -> None:
            self.stop_requested = True
            try:
                CheckpointManager().save()
            except Exception:
                pass
            self.root.quit()

        try:
            signal.signal(signal.SIGINT, _handler)
        except ValueError:
            pass

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _append_log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{ts}] {msg}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _refresh_db_status(self) -> None:
        pairs = 0
        terms = 0
        try:
            if DB_PATH.exists() and DB_PATH.stat().st_size > 0:
                db = SkyrimTranslationDB(DB_PATH, auto_seed=False)
                pairs = db.get_summary_counts().get("total_rows", 0)
                db.close()
            if GLOSSARY_PATH.exists():
                import json

                terms = len(json.loads(GLOSSARY_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
        self.db_badge_var.set(f"База TM: {pairs:,} пар | Глосарій: {terms:,} термінів")

    def _open_html_dictionary(self) -> None:
        idx = generate_master_index()
        webbrowser.open(idx.resolve().as_uri())
        self._append_log(f"Відкрито словник: {idx}")

    def _patch_dir(self) -> Path:
        raw = sanitize_output_patch_dir(self.output_patch_var.get().strip())
        return resolve_portable_path(raw, "./output/[UA] Skyrim Translation Patch")

    def _open_patch_folder(self) -> None:
        p = self._patch_dir()
        named_sub = p / "[UA] Skyrim Translation Patch"
        if named_sub.exists():
            _open_path(named_sub)
            return
        if not p.exists():
            messagebox.showinfo("Папка патча", f"Патч ще не створено:\n{p}")
            return
        _open_path(p)

    def _browse_en_source(self) -> None:
        chosen = filedialog.askdirectory(title="Skyrim Special Edition\\Data (англійські файли / BSA)")
        if chosen:
            self.en_source_var.set(chosen)

    def _browse_uk_reference(self) -> None:
        chosen = filedialog.askdirectory(title="Папка українського перекладу-еталона (ОПН / ТПН)")
        if chosen:
            self.uk_ref_var.set(chosen)

    def _browse_target(self) -> None:
        if self.mode_var.get() == MODE_SINGLE:
            chosen = filedialog.askopenfilename(
                title="Плагін або MCM-файл",
                filetypes=[("Плагіни та MCM", "*.esp *.esm *.esl *.txt"), ("Усі файли", "*.*")],
            ) or filedialog.askdirectory(title="Або оберіть папку мода")
        else:
            chosen = filedialog.askdirectory(title="Теки модліста (Mod Organizer 2 / mods)")
        if chosen:
            self.target_path_var.set(chosen)

    def _browse_output(self) -> None:
        chosen = filedialog.askdirectory(title="Папка єдиного патча")
        if chosen:
            self.output_patch_var.set(chosen)

    def _save_current_config(self) -> TranslatorConfig:
        self.cfg.mo2_mods_dir = self.target_path_var.get().strip() or "./mods"
        self.cfg.output_patch_dir = sanitize_output_patch_dir(self.output_patch_var.get().strip())
        self.output_patch_var.set(self.cfg.output_patch_dir)
        self.cfg.mcm_title_mode = "keep_english" if self.keep_mcm_en_var.get() else "translate"
        self.cfg.strict_whitelist_mode = bool(self.strict_whitelist_var.get())
        self.cfg.use_local_ollama = bool(self.use_ollama_var.get())
        if self.en_source_var.get().strip():
            self.cfg.game_data_dir = self.en_source_var.get().strip()
        if self.uk_ref_var.get().strip():
            self.cfg.reference_dir = self.uk_ref_var.get().strip()
        save_config_model(self.cfg)
        self._append_log("Конфігурацію збережено в config.json.")
        return self.cfg

    # ------------------------------------------------------------------ background jobs
    def _set_busy(self, busy: bool) -> None:
        self.is_running = busy
        for b in self._action_buttons:
            b.configure(state="disabled" if busy else "normal")

    def _run_job(self, name: str, fn: Any) -> None:
        if self.is_running:
            return
        self._set_busy(True)
        self.progress_bar.set(0.02)
        self.status_var.set(f"{name}…")

        def runner() -> None:
            try:
                fn()
            except Exception as exc:
                self.log_queue.put({"type": "log", "text": f"[ПОМИЛКА] {exc}"})
            finally:
                self.log_queue.put({"type": "done"})

        threading.Thread(target=runner, daemon=True).start()

    def _progress_cb(self, msg: str, pct: float) -> None:
        self.log_queue.put({"type": "log", "text": msg})
        self.log_queue.put({"type": "progress", "status": msg, "pct": pct})

    def _start_build_db(self) -> None:
        uk = self.uk_ref_var.get().strip()
        if not uk or not Path(uk).exists():
            messagebox.showwarning("Еталон не вказано", "Вкажіть існуючу папку українського перекладу-еталона (донора).")
            return
        en = self.en_source_var.get().strip()
        extra = []
        tgt = self.target_path_var.get().strip()
        if self.mode_var.get() == MODE_MODLIST and tgt and Path(tgt).is_dir():
            extra.append(Path(tgt))
        self._save_current_config()
        req = TMBuildRequest(
            uk_reference_path=Path(uk),
            en_source_path=Path(en) if en else None,
            extra_en_roots=extra,
        )

        def job() -> None:
            res = build_tm_from_reference(req, progress_callback=self._progress_cb)
            self.log_queue.put({
                "type": "log",
                "text": (
                    f"[УСПІХ] skyrim_tm.db: {res.total_tm_rows:,} пар (StringID: {res.strings_pairs_count:,}, "
                    f"плагіни: {res.plugin_pairs_count:,}, MCM/UI: {res.mcm_pairs_count:,}, "
                    f"словники: {res.dictionary_pairs_count:,}); глосарій: {res.glossary_terms_count:,} термінів."
                ),
            })

        self._run_job("Імпорт еталона", job)

    def _start_translation(self) -> None:
        tgt = self.target_path_var.get().strip()
        if not tgt or not Path(tgt).exists():
            messagebox.showwarning("Ціль не вказано", "Вкажіть існуючий мод або теку модліста.")
            return
        cfg = self._save_current_config()
        target = Path(tgt)
        single = self.mode_var.get() == MODE_SINGLE
        t0 = time.perf_counter()

        def job() -> None:
            if single:
                req = ModTranslationRequest(
                    target_path=target,
                    mcm_title_mode=cfg.mcm_title_mode,
                    strict_whitelist_mode=cfg.strict_whitelist_mode,
                    use_local_ollama=cfg.use_local_ollama,
                    max_workers=cfg.max_workers,
                    batch_size=cfg.batch_size,
                )
                results = [translate_mod_target(req, progress_cb=self._progress_cb)]
            else:
                mods_dir = target / "mods" if (target / "mods").is_dir() else target
                results = translate_modlist(
                    mods_dir,
                    mcm_title_mode=cfg.mcm_title_mode,
                    strict_whitelist_mode=cfg.strict_whitelist_mode,
                    use_local_ollama=cfg.use_local_ollama,
                    progress_cb=self._progress_cb,
                )
            total = sum(r.total_strings for r in results)
            tm = sum(r.tm_hits for r in results)
            elapsed = max(0.01, time.perf_counter() - t0)
            self.log_queue.put({
                "type": "progress", "status": "Переклад завершено", "pct": 100.0,
                "speed": total / elapsed, "tm_rate": 100.0 * tm / max(1, total),
            })
            self.log_queue.put({
                "type": "log",
                "text": f"[УСПІХ] Модів: {len(results)}, рядків: {total:,}, збігів TM: {tm:,}. "
                        f"Результат: {get_translated_mods_dir()}",
            })
            generate_master_index()

        self._run_job("Переклад", job)

    def _start_build_patch(self) -> None:
        self._save_current_config()
        req = PatchBuildRequest(
            output_patch_dir=self._patch_dir(),
            donor_dir=Path(self.uk_ref_var.get().strip()) if self.uk_ref_var.get().strip() else None,
            include_donor=bool(self.include_donor_var.get()),
        )

        def job() -> None:
            res = build_unified_mo2_patch(req, progress_cb=self._progress_cb)
            self.log_queue.put({
                "type": "log",
                "text": (
                    f"[УСПІХ] Єдиний патч: {res.total_files:,} файлів "
                    f"(донор: {res.donor_files:,}, моди: {res.mod_files:,} з {len(res.mods_merged)}) → {res.patch_dir}"
                    + (f" | Архів: {res.zip_path}" if res.zip_path else "")
                ),
            })

        self._run_job("Збірка єдиного патча", job)

    # ------------------------------------------------------------------ queue pump
    def _poll_queue(self) -> None:
        while True:
            try:
                item = self.log_queue.get_nowait()
            except queue.Empty:
                break
            kind = item.get("type")
            if kind == "log":
                self._append_log(item["text"])
            elif kind == "progress":
                self.progress_bar.set(max(0.0, min(1.0, float(item.get("pct", 0.0)) / 100.0)))
                self.status_var.set(item.get("status", "Виконується…"))
                if "speed" in item:
                    self.speed_var.set(f"Рядків/сек: {item['speed']:.1f}")
                if "tm_rate" in item:
                    self.tm_rate_var.set(f"TM Hit Rate: {item['tm_rate']:.1f}%")
            elif kind == "done":
                self._set_busy(False)
                self._refresh_db_status()
                if self.progress_bar.get() < 1.0:
                    self.status_var.set("Завершено (див. журнал).")
        self.root.after(100, self._poll_queue)

    def run(self) -> None:
        self.root.mainloop()


SkyrimTranslatorApp = TranslatorApp


def launch_gui() -> None:
    TranslatorApp().run()


if __name__ == "__main__":
    launch_gui()
