"""
Standalone Universal Desktop GUI for Skyrim SE/AE English-to-Ukrainian Mod Translator
(skyrim_translator_gui.py).

Uses standard library `tkinter` / `ttk` (zero external binary GUI dependencies) with a modern
dark-themed interface, RORO configuration models, live progress metrics (strings/sec, TM hit rate),
and direct access to the interactive HTML Dictionary (`INDEX_DICTIONARY.html`).
"""

import os
import queue
import signal
import sys
import threading
import time
import webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
from typing import Any, Dict, Optional

from core.batch_orchestrator import ModpackBatchOrchestrator, ensure_mo2_patch_meta_ini
from core.checkpoint_manager import CheckpointManager
from core.config import (
    PROJECT_ROOT,
    TranslatorConfig,
    load_config_model,
    save_config_model,
)
from core.dictionary_exporter import INDEX_PATH, generate_master_index
from core.mod_translator import (
    ModTranslationRequest,
    ModTranslationResult,
    translate_mod_target,
)
from core.modpack_scanner import scan_all_modpack_mods


BG_DARK = "#12141a"
BG_PANEL = "#1b1e27"
BG_INPUT = "#101218"
FG_TEXT = "#e8ecf4"
FG_MUTED = "#9aa3b5"
ACCENT_GOLD = "#e2b86b"
ACCENT_GREEN = "#7ce3a3"
BORDER_COLOR = "#2c3140"


def apply_dark_theme(root: tk.Tk) -> ttk.Style:
    """Configure a modern dark theme for ttk widgets."""
    root.configure(bg=BG_DARK)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(".", background=BG_DARK, foreground=FG_TEXT, font=("Segoe UI", 10))
    style.configure("TFrame", background=BG_DARK)
    style.configure("Card.TFrame", background=BG_PANEL, relief="flat")
    style.configure("TLabel", background=BG_DARK, foreground=FG_TEXT, font=("Segoe UI", 10))
    style.configure("Card.TLabel", background=BG_PANEL, foreground=FG_TEXT, font=("Segoe UI", 10))
    style.configure("Header.TLabel", background=BG_DARK, foreground=ACCENT_GOLD, font=("Segoe UI", 15, "bold"))
    style.configure("SubHeader.TLabel", background=BG_PANEL, foreground=ACCENT_GOLD, font=("Segoe UI", 11, "bold"))
    style.configure("Muted.TLabel", background=BG_PANEL, foreground=FG_MUTED, font=("Segoe UI", 9))
    style.configure("Metric.TLabel", background=BG_PANEL, foreground=ACCENT_GREEN, font=("Consolas", 11, "bold"))

    style.configure(
        "TButton",
        background="#232734",
        foreground=FG_TEXT,
        bordercolor=BORDER_COLOR,
        padding=(12, 6),
        font=("Segoe UI", 10, "bold"),
    )
    style.map(
        "TButton",
        background=[("active", ACCENT_GOLD), ("disabled", "#181a22")],
        foreground=[("active", BG_DARK), ("disabled", FG_MUTED)],
    )

    style.configure(
        "Accent.TButton",
        background=ACCENT_GOLD,
        foreground=BG_DARK,
        bordercolor=ACCENT_GOLD,
        padding=(16, 8),
        font=("Segoe UI", 10, "bold"),
    )
    style.map(
        "Accent.TButton",
        background=[("active", "#f0c97d"), ("disabled", "#3a3f50")],
        foreground=[("active", BG_DARK), ("disabled", FG_MUTED)],
    )

    style.configure(
        "TCheckbutton",
        background=BG_PANEL,
        foreground=FG_TEXT,
        font=("Segoe UI", 10),
    )
    style.map("TCheckbutton", background=[("active", BG_PANEL)])

    style.configure(
        "TCombobox",
        fieldbackground=BG_INPUT,
        background="#232734",
        foreground=FG_TEXT,
        arrowcolor=ACCENT_GOLD,
    )

    style.configure(
        "Horizontal.TProgressbar",
        troughcolor=BG_INPUT,
        background=ACCENT_GOLD,
        bordercolor=BORDER_COLOR,
        lightcolor=ACCENT_GOLD,
        darkcolor=ACCENT_GOLD,
        thickness=18,
    )
    return style


class SkyrimTranslatorApp:
    """Universal GUI Controller for Skyrim SE/AE English -> Ukrainian Mod Translation."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Skyrim SE/AE EN -> UA Mod Translator (OPN / TPN Standard)")
        self.root.geometry("980x720")
        self.root.minsize(860, 640)
        apply_dark_theme(self.root)

        self.cfg: TranslatorConfig = load_config_model()
        self.log_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.is_running = False
        self.stop_requested = False

        # Tkinter reactive variables
        self.target_path_var = tk.StringVar(value=self.cfg.mo2_mods_dir)
        self.output_patch_var = tk.StringVar(value=self.cfg.output_patch_dir)
        self.mcm_mode_var = tk.StringVar(value=self.cfg.mcm_title_mode)
        self.keep_mcm_en_var = tk.BooleanVar(value=(self.cfg.mcm_title_mode == "keep_english"))
        self.strict_whitelist_var = tk.BooleanVar(value=self.cfg.strict_whitelist_mode)
        self.use_ollama_var = tk.BooleanVar(value=self.cfg.use_local_ollama)
        self.progress_var = tk.DoubleVar(value=0.0)
        self.speed_var = tk.StringVar(value="Speed: 0.0 strings/sec")
        self.tm_rate_var = tk.StringVar(value="TM Cache Hit Rate: 0.0%")
        self.status_var = tk.StringVar(value="Ready.")

        self._build_layout()
        self._setup_sigint()
        self.root.after(100, self._poll_queue)

    def _setup_sigint(self) -> None:
        """Ensure graceful checkpoint flush if terminated via SIGINT."""
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

    def _build_layout(self) -> None:
        container = ttk.Frame(self.root, padding=18)
        container.pack(fill=tk.BOTH, expand=True)

        # Header
        header_lbl = ttk.Label(
            container,
            text="Skyrim SE/AE — Український локалізатор модів (OPN / TPN)",
            style="Header.TLabel",
        )
        header_lbl.pack(anchor=tk.W, pady=(0, 12))

        # Path Selectors Card
        paths_card = ttk.Frame(container, style="Card.TFrame", padding=14)
        paths_card.pack(fill=tk.X, pady=(0, 12))

        ttk.Label(
            paths_card,
            text="1. Вибір цільового моду, плагіна (.esp/.esm/.esl) або теки Mod Organizer 2",
            style="SubHeader.TLabel",
        ).grid(row=0, column=0, columnspan=4, sticky=tk.W, pady=(0, 8))

        ttk.Label(paths_card, text="Target Mod / MO2 Folder:", style="Card.TLabel").grid(
            row=1, column=0, sticky=tk.W, padx=(0, 8), pady=4
        )
        target_entry = tk.Entry(
            paths_card,
            textvariable=self.target_path_var,
            bg=BG_INPUT,
            fg=FG_TEXT,
            insertbackground=FG_TEXT,
            relief="flat",
            font=("Consolas", 10),
        )
        target_entry.grid(row=1, column=1, sticky=tk.EW, padx=4, pady=4, ipady=5)

        ttk.Button(paths_card, text="Browse File (.esp/.txt)", command=self._browse_target_file).grid(
            row=1, column=2, padx=4, pady=4
        )
        ttk.Button(paths_card, text="Browse Folder / MO2", command=self._browse_target_folder).grid(
            row=1, column=3, padx=4, pady=4
        )

        ttk.Label(paths_card, text="Output Patch Folder:", style="Card.TLabel").grid(
            row=2, column=0, sticky=tk.W, padx=(0, 8), pady=4
        )
        output_entry = tk.Entry(
            paths_card,
            textvariable=self.output_patch_var,
            bg=BG_INPUT,
            fg=FG_TEXT,
            insertbackground=FG_TEXT,
            relief="flat",
            font=("Consolas", 10),
        )
        output_entry.grid(row=2, column=1, columnspan=2, sticky=tk.EW, padx=4, pady=4, ipady=5)
        ttk.Button(paths_card, text="Browse Output", command=self._browse_output_folder).grid(
            row=2, column=3, padx=4, pady=4
        )
        paths_card.columnconfigure(1, weight=1)

        # Options & Checkboxes Card
        opts_card = ttk.Frame(container, style="Card.TFrame", padding=14)
        opts_card.pack(fill=tk.X, pady=(0, 12))

        ttk.Label(opts_card, text="2. Параметри безпеки та перекладу", style="SubHeader.TLabel").grid(
            row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 8)
        )

        chk_mcm = ttk.Checkbutton(
            opts_card,
            text="Keep MCM Mod Titles in English (Зберігати англійські назви модів у списку MCM)",
            variable=self.keep_mcm_en_var,
            command=self._on_mcm_checkbox_toggle,
        )
        chk_mcm.grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=3)

        mcm_frame = ttk.Frame(opts_card, style="Card.TFrame")
        mcm_frame.grid(row=1, column=2, sticky=tk.E, pady=3)
        ttk.Label(mcm_frame, text="MCM Title Mode:", style="Card.TLabel").pack(side=tk.LEFT, padx=(0, 6))
        mcm_combo = ttk.Combobox(
            mcm_frame,
            textvariable=self.mcm_mode_var,
            values=["keep_english", "bilingual", "translate"],
            state="readonly",
            width=15,
        )
        mcm_combo.pack(side=tk.LEFT)
        mcm_combo.bind("<<ComboboxSelected>>", self._on_mcm_combo_select)

        chk_whitelist = ttk.Checkbutton(
            opts_card,
            text="Strict Crash-Proof Whitelist Mode (Skip CELL/WRLD/HDPT/RACE — Recommended)",
            variable=self.strict_whitelist_var,
        )
        chk_whitelist.grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=3)

        chk_ollama = ttk.Checkbutton(
            opts_card,
            text="Use Local Ollama LLM for Fallback (Optional — qwen2.5:3b via localhost:11434)",
            variable=self.use_ollama_var,
        )
        chk_ollama.grid(row=3, column=0, columnspan=3, sticky=tk.W, pady=3)
        opts_card.columnconfigure(0, weight=1)

        # Progress & Action Bar Card
        prog_card = ttk.Frame(container, style="Card.TFrame", padding=14)
        prog_card.pack(fill=tk.X, pady=(0, 12))

        metrics_row = ttk.Frame(prog_card, style="Card.TFrame")
        metrics_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(metrics_row, textvariable=self.status_var, style="Card.TLabel").pack(side=tk.LEFT)
        ttk.Label(metrics_row, textvariable=self.tm_rate_var, style="Metric.TLabel").pack(side=tk.RIGHT, padx=(16, 0))
        ttk.Label(metrics_row, textvariable=self.speed_var, style="Metric.TLabel").pack(side=tk.RIGHT)

        self.progress_bar = ttk.Progressbar(
            prog_card,
            variable=self.progress_var,
            maximum=100.0,
            mode="determinate",
            style="Horizontal.TProgressbar",
        )
        self.progress_bar.pack(fill=tk.X, pady=(0, 10))

        btn_row = ttk.Frame(prog_card, style="Card.TFrame")
        btn_row.pack(fill=tk.X)

        self.start_btn = ttk.Button(
            btn_row,
            text="▶ Start Translation",
            style="Accent.TButton",
            command=self._start_translation,
        )
        self.start_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.batch_btn = ttk.Button(
            btn_row,
            text="⚡ Batch Translate MO2 Modlist",
            command=self._start_modlist_batch,
        )
        self.batch_btn.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Button(
            btn_row,
            text="💾 Save Config",
            command=self._save_current_config,
        ).pack(side=tk.LEFT, padx=(0, 8))

        ttk.Button(
            btn_row,
            text="📖 Open HTML Dictionary (INDEX_DICTIONARY.html)",
            command=self._open_html_dictionary,
        ).pack(side=tk.RIGHT)

        # Live Console Log
        log_card = ttk.Frame(container, style="Card.TFrame", padding=12)
        log_card.pack(fill=tk.BOTH, expand=True)

        ttk.Label(log_card, text="3. Консоль виконання (Live Translation Log)", style="SubHeader.TLabel").pack(
            anchor=tk.W, pady=(0, 6)
        )

        self.log_text = tk.Text(
            log_card,
            bg=BG_INPUT,
            fg=FG_TEXT,
            insertbackground=FG_TEXT,
            relief="flat",
            font=("Consolas", 9),
            wrap=tk.WORD,
            state=tk.DISABLED,
        )
        scrollbar = ttk.Scrollbar(log_card, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._append_log("Skyrim SE/AE EN -> UA Mod Translator initialized.")
        self._append_log("Select a plugin (.esp/.esm/.esl), MCM file, Mod Folder, or MO2 mods directory to begin.")

    def _on_mcm_checkbox_toggle(self) -> None:
        if self.keep_mcm_en_var.get():
            self.mcm_mode_var.set("keep_english")
        elif self.mcm_mode_var.get() == "keep_english":
            self.mcm_mode_var.set("translate")

    def _on_mcm_combo_select(self, _event: Any = None) -> None:
        self.keep_mcm_en_var.set(self.mcm_mode_var.get() == "keep_english")

    def _browse_target_file(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Select Skyrim Plugin or MCM File",
            filetypes=[
                ("Skyrim Plugins & MCM", "*.esp *.esm *.esl *.txt"),
                ("Creation Engine Plugins", "*.esp *.esm *.esl"),
                ("MCM Translation Files", "*_english.txt *.txt"),
                ("All Files", "*.*"),
            ],
        )
        if chosen:
            self.target_path_var.set(chosen)

    def _browse_target_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Select Mod Folder or Mod Organizer 2 mods/ Folder")
        if chosen:
            self.target_path_var.set(chosen)

    def _browse_output_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Select Output Patch Folder")
        if chosen:
            self.output_patch_var.set(chosen)

    def _save_current_config(self) -> TranslatorConfig:
        self.cfg.mo2_mods_dir = self.target_path_var.get().strip() or "./mods"
        self.cfg.output_patch_dir = self.output_patch_var.get().strip() or "./output/[UA] Skyrim Translation Patch"
        self.cfg.mcm_title_mode = self.mcm_mode_var.get().strip() or "keep_english"
        self.cfg.strict_whitelist_mode = bool(self.strict_whitelist_var.get())
        self.cfg.use_local_ollama = bool(self.use_ollama_var.get())
        save_config_model(self.cfg)
        self._append_log("Configuration saved to config.json.")
        return self.cfg

    def _open_html_dictionary(self) -> None:
        idx_path = INDEX_PATH
        if not idx_path.exists():
            idx_path = generate_master_index()
        if idx_path.exists():
            webbrowser.open(idx_path.resolve().as_uri())
            self._append_log(f"Opened HTML Dictionary: {idx_path}")
        else:
            messagebox.showinfo("Dictionary Not Found", "No translated mods dictionary exists yet.")

    def _append_log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{ts}] {msg}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _poll_queue(self) -> None:
        while not self.log_queue.empty():
            item = self.log_queue.get_nowait()
            kind = item.get("type")
            if kind == "log":
                self._append_log(item["text"])
            elif kind == "progress":
                self.progress_var.set(float(item.get("pct", 0.0)))
                self.status_var.set(item.get("status", "Translating..."))
                if "speed" in item:
                    self.speed_var.set(f"Speed: {item['speed']:.1f} strings/sec")
                if "tm_rate" in item:
                    self.tm_rate_var.set(f"TM Cache Hit Rate: {item['tm_rate']:.1f}%")
            elif kind == "done":
                self.is_running = False
                self.start_btn.configure(state=tk.NORMAL)
                self.batch_btn.configure(state=tk.NORMAL)
                self.progress_var.set(100.0)
                self.status_var.set("Translation Complete.")
        self.root.after(100, self._poll_queue)

    def _start_translation(self) -> None:
        if self.is_running:
            return
        target_str = self.target_path_var.get().strip()
        if not target_str:
            messagebox.showwarning("Missing Target", "Please select a plugin file or mod folder first.")
            return

        target_path = Path(target_str)
        if not target_path.exists():
            messagebox.showerror("Invalid Path", f"Target path does not exist:\n{target_path}")
            return

        cfg = self._save_current_config()
        self.is_running = True
        self.start_btn.configure(state=tk.DISABLED)
        self.batch_btn.configure(state=tk.DISABLED)
        self.progress_var.set(5.0)
        self.status_var.set("Starting translation...")

        req = ModTranslationRequest(
            target_path=target_path,
            output_patch_dir=Path(cfg.output_patch_dir),
            mcm_title_mode=cfg.mcm_title_mode,
            strict_whitelist_mode=cfg.strict_whitelist_mode,
            use_local_ollama=cfg.use_local_ollama,
            max_workers=cfg.max_workers,
            batch_size=cfg.batch_size,
        )

        threading.Thread(target=self._worker_single_target, args=(req,), daemon=True).start()

    def _worker_single_target(self, req: ModTranslationRequest) -> None:
        t0 = time.perf_counter()
        self.log_queue.put({"type": "log", "text": f"Starting translation for: {req.target_path}"})
        try:
            def _prog(msg: str, pct: float) -> None:
                self.log_queue.put({"type": "log", "text": msg})
                self.log_queue.put({"type": "progress", "status": msg, "pct": pct})

            res: ModTranslationResult = translate_mod_target(req, progress_cb=_prog)
            elapsed = max(0.01, time.perf_counter() - t0)
            speed = res.total_strings / elapsed
            tm_rate = (res.tm_hits / max(1, res.total_strings)) * 100.0

            self.log_queue.put({
                "type": "progress",
                "status": f"Completed {res.mod_name}",
                "pct": 100.0,
                "speed": speed,
                "tm_rate": tm_rate,
            })
            self.log_queue.put({
                "type": "log",
                "text": (
                    f"[SUCCESS] {res.mod_name}: {res.total_strings} strings "
                    f"(TM={res.tm_hits} [{tm_rate:.1f}%], Neural={res.neural_translated}) "
                    f"in {res.elapsed_sec}s ({speed:.1f} str/s)."
                ),
            })
            self.log_queue.put({"type": "log", "text": f"Output Patch: {res.output_patch_dir}"})
            self.log_queue.put({"type": "log", "text": f"HTML Dictionary: {res.html_dictionary_path}"})
        except Exception as exc:
            self.log_queue.put({"type": "log", "text": f"[ERROR] {exc}"})
        finally:
            self.log_queue.put({"type": "done"})

    def _start_modlist_batch(self) -> None:
        if self.is_running:
            return
        target_str = self.target_path_var.get().strip()
        target_path = Path(target_str)
        if not target_path.exists() or not target_path.is_dir():
            messagebox.showwarning(
                "Select MO2 Folder",
                "Please select a valid Mod Organizer 2 directory or mods/ folder first.",
            )
            return

        # Automatically detect `mods/` subfolder if user selected MO2 root folder
        mods_dir = target_path / "mods" if (target_path / "mods").is_dir() else target_path
        cfg = self._save_current_config()
        self.is_running = True
        self.start_btn.configure(state=tk.DISABLED)
        self.batch_btn.configure(state=tk.DISABLED)
        self.progress_var.set(2.0)

        threading.Thread(
            target=self._worker_modlist_batch,
            args=(mods_dir, Path(cfg.output_patch_dir)),
            daemon=True,
        ).start()

    def _worker_modlist_batch(self, mods_dir: Path, patch_dir: Path) -> None:
        t0 = time.perf_counter()
        self.log_queue.put({"type": "log", "text": f"Scanning modlist folder: {mods_dir}..."})
        try:
            scan_res = scan_all_modpack_mods(mods_dir=mods_dir, save_outputs=True)
            total_mods = scan_res.get("summary", {}).get("translatable_mods", 0)
            self.log_queue.put({
                "type": "log",
                "text": f"Cataloged {total_mods} translatable mods. Starting batch waves...",
            })

            orch = ModpackBatchOrchestrator(mods_dir=mods_dir, patch_dir=patch_dir)
            try:
                total_strings_done = 0
                for idx, wave_id in enumerate([1, 2, 3, 4], 1):
                    self.log_queue.put({
                        "type": "progress",
                        "status": f"Executing Wave {wave_id}...",
                        "pct": (idx - 1) * 25.0,
                    })
                    w_res = orch.execute_wave(wave_id, sync_to_mo2=True)
                    total_strings_done += w_res.get("total_strings_processed", 0)
                    elapsed = max(0.01, time.perf_counter() - t0)
                    speed = total_strings_done / elapsed
                    self.log_queue.put({
                        "type": "progress",
                        "status": f"Finished Wave {wave_id}",
                        "pct": idx * 25.0,
                        "speed": speed,
                    })
                    self.log_queue.put({
                        "type": "log",
                        "text": (
                            f"[Wave {wave_id}] Processed {w_res.get('processed_now', 0)} mods "
                            f"({w_res.get('total_strings_processed', 0)} strings) in {w_res.get('elapsed_sec', 0)}s."
                        ),
                    })
                ensure_mo2_patch_meta_ini(patch_dir)
                generate_master_index()
            finally:
                orch.close()
        except Exception as exc:
            self.log_queue.put({"type": "log", "text": f"[ERROR] Batch translation failed: {exc}"})
        finally:
            self.log_queue.put({"type": "done"})


def launch_gui() -> None:
    root = tk.Tk()
    SkyrimTranslatorApp(root)
    root.mainloop()


if __name__ == "__main__":
    launch_gui()
