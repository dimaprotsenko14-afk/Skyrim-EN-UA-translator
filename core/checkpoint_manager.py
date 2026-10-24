"""
Checkpoint / auto-resume manager (core/checkpoint_manager.py).

Tracks per-mod translation state in data/translation_checkpoint.json:
    mod_name, source_sha256, status (COMPLETED | IN_PROGRESS | PENDING),
    total_strings, translated_strings, timestamp, [summary]

Resume model:
  * Every translated batch is committed immediately into skyrim_tm.db (WAL mode) by
    HybridTranslator.translate_batch_deduplicated, so a re-run resolves all finished strings
    through O(1) in-memory TM lookups and only sends the untranslated remainder to the model.
  * A mod whose status is COMPLETED and whose source hash (plus config fingerprint) is unchanged
    is skipped entirely.
"""

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHECKPOINT_PATH = PROJECT_ROOT / "data" / "translation_checkpoint.json"

COMPLETED = "COMPLETED"
IN_PROGRESS = "IN_PROGRESS"
PENDING = "PENDING"
FAILED = "FAILED"


def sha256_of_files(paths: Iterable[Path], extra: str = "") -> str:
    """Stable SHA-256 over file contents (+ an optional config fingerprint string)."""
    h = hashlib.sha256()
    for p in sorted(Path(x) for x in paths if x):
        h.update(p.name.encode("utf-8"))
        if p.exists():
            st = p.stat()
            if st.st_size > 10 * 1024 * 1024:
                # Fast path for multi-gigabyte BSA archives
                h.update(f"{st.st_size}_{st.st_mtime_ns}".encode("utf-8"))
            else:
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
    h.update(extra.encode("utf-8"))
    return h.hexdigest()


def sha256_of_text_items(items: Iterable[str], extra: str = "") -> str:
    h = hashlib.sha256()
    for s in items:
        h.update(s.encode("utf-8"))
        h.update(b"\x00")
    h.update(extra.encode("utf-8"))
    return h.hexdigest()


class CheckpointManager:
    def __init__(self, path: Path = CHECKPOINT_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.state: Dict[str, Any] = {"version": 1, "mods": {}}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self.state = json.loads(self.path.read_text(encoding="utf-8"))
                self.state.setdefault("mods", {})
            except (OSError, ValueError):
                self.state = {"version": 1, "mods": {}}

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    def get(self, mod_name: str) -> Optional[Dict[str, Any]]:
        return self.state["mods"].get(mod_name)

    def is_completed(self, mod_name: str, source_sha256: str) -> bool:
        e = self.get(mod_name)
        return bool(e and e.get("status") == COMPLETED and e.get("source_sha256") == source_sha256)

    def is_synced(self, mod_name: str) -> bool:
        e = self.get(mod_name)
        return bool(e and e.get("synced_to_mo2", False))

    def mark_pending(self, mod_name: str, source_sha256: str, total_strings: int, wave: Optional[Any] = None) -> None:
        e = self.get(mod_name)
        if e and e.get("source_sha256") == source_sha256 and e.get("status") in (COMPLETED, IN_PROGRESS):
            return
        self._set(mod_name, source_sha256, PENDING, total_strings, 0, wave=wave)

    def mark_in_progress(self, mod_name: str, source_sha256: str, total_strings: int, translated: int = 0, wave: Optional[Any] = None) -> None:
        self._set(mod_name, source_sha256, IN_PROGRESS, total_strings, translated, wave=wave)

    def mark_completed(
        self,
        mod_name: str,
        source_sha256: str,
        total_strings: int,
        summary: Optional[Dict[str, Any]] = None,
        synced_to_mo2: bool = True,
        wave: Optional[Any] = None,
    ) -> None:
        self._set(mod_name, source_sha256, COMPLETED, total_strings, total_strings, summary=summary, synced_to_mo2=synced_to_mo2, wave=wave)

    def mark_failed(
        self,
        mod_name: str,
        source_sha256: str,
        total_strings: int,
        error_msg: str,
        wave: Optional[Any] = None,
    ) -> None:
        self._set(mod_name, source_sha256, FAILED, total_strings, 0, summary={"error": error_msg}, synced_to_mo2=False, wave=wave)

    def mark_synced(self, mod_name: str, synced: bool = True, save_now: bool = True) -> None:
        with self._lock:
            if mod_name in self.state["mods"]:
                self.state["mods"][mod_name]["synced_to_mo2"] = synced
                if save_now:
                    self.save()

    def _set(
        self,
        mod_name: str,
        sha: str,
        status: str,
        total: int,
        translated: int,
        summary: Optional[Dict[str, Any]] = None,
        synced_to_mo2: Optional[bool] = None,
        wave: Optional[Any] = None,
    ) -> None:
        prev = self.state["mods"].get(mod_name, {})
        entry = {
            "mod_name": mod_name,
            "source_sha256": sha,
            "status": status,
            "total_strings": total,
            "translated_strings": translated,
            "synced_to_mo2": synced_to_mo2 if synced_to_mo2 is not None else prev.get("synced_to_mo2", False),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        if wave is not None:
            entry["wave"] = wave
        elif "wave" in prev:
            entry["wave"] = prev["wave"]

        if summary is not None:
            entry["summary"] = summary
        elif prev.get("source_sha256") == sha and "summary" in prev:
            entry["summary"] = prev["summary"]
        self.state["mods"][mod_name] = entry
        self.save()

    def counts(self) -> Dict[str, int]:
        c = {COMPLETED: 0, IN_PROGRESS: 0, PENDING: 0, FAILED: 0}
        for e in self.state["mods"].values():
            st = e.get("status", PENDING)
            c[st] = c.get(st, 0) + 1
        return c

    def get_wave_stats(self) -> Dict[str, Dict[str, int]]:
        waves: Dict[str, Dict[str, int]] = {}
        for e in self.state["mods"].values():
            w = str(e.get("wave", "Unknown"))
            st = e.get("status", PENDING)
            waves.setdefault(w, {"total": 0, COMPLETED: 0, IN_PROGRESS: 0, PENDING: 0, FAILED: 0, "synced": 0})
            waves[w]["total"] += 1
            waves[w][st] = waves[w].get(st, 0) + 1
            if e.get("synced_to_mo2"):
                waves[w]["synced"] += 1
        return waves
