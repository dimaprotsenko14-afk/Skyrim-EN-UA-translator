"""
Reference Mod & Translation Memory Builder (core/tm_builder.py).

Builds the local data/skyrim_tm.db and data/glossary_uk.json on the user's PC from:
  * a Ukrainian reference localization (downloaded OPN / TPN mod folder: loose Strings/*, Interface/*,
    BSA-packed tables, translated plugins, bilingual dictionaries in .json/.csv/.db/.sql), and
  * the original English resources (the game `Data` folder: loose `Strings/` or tables packed inside the
    official BSAs such as `Skyrim - Interface.bsa`, `ccXXXX.bsa`, ...). If the English path is left blank
    the game directory is auto-detected.

String tables are paired strictly by the 32-bit StringID inside identically named tables
(`skyrim_english.strings`, `dawnguard_english.dlstrings`, `cc*_english.ilstrings`, ...).
The English side is always the candidate with the lowest Cyrillic ratio, so a game folder that already
contains a deployed Ukrainian translation can never be mistaken for the English original.
"""

import csv
import json
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from core.bsa_parser import BSAArchive
from core.config import PROJECT_ROOT, get_game_data_dir
from core.database import (
    CYRILLIC_RE,
    DB_PATH,
    GLOSSARY_PATH,
    SkyrimTranslationDB,
    is_clean_glossary_candidate,
)
from core.esp_parser import parse_plugin_file
from core.scanner import OPN_BASE_MODULES
from core.strings_parser import (
    extract_module_and_ext,
    parse_string_table_bytes,
    parse_translate_txt,
    parse_translate_txt_bytes,
)

STRING_EXTS = (".strings", ".dlstrings", ".ilstrings")
PLUGIN_EXTS = (".esp", ".esm", ".esl")
HEAVY_BSA_TOKENS = ("texture", "mesh", "sound", "voice", "animation", "shader")
REFERENCE_GROUPS = ("OPN", "TPN", "REFERENCE_MOD", "REFERENCE_DICT")

ProgressCb = Optional[Callable[[str, float], None]]
Row = Tuple[str, str, str, Optional[int], Optional[str], str, str, int]


@dataclass
class TMBuildRequest:
    """Request parameters for building local translation memory and glossary."""

    uk_reference_path: Path
    en_source_path: Optional[Path] = None
    extra_en_roots: List[Path] = field(default_factory=list)
    db_path: Path = DB_PATH
    glossary_path: Path = GLOSSARY_PATH
    extract_glossary: bool = True
    reset_db: bool = False


@dataclass
class TMBuildResult:
    """Results from building local translation memory and glossary."""

    strings_pairs_count: int
    plugin_pairs_count: int
    mcm_pairs_count: int
    dictionary_pairs_count: int
    total_tm_rows: int
    glossary_terms_count: int
    db_path: Path
    glossary_path: Path
    elapsed_sec: float
    english_sources: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

def _cyr_ratio(values: Iterable[str], limit: int = 600) -> float:
    """Fraction of (sampled) non-empty strings containing at least one Cyrillic letter."""
    total = 0
    cyr = 0
    for v in islice((x for x in values if x and x.strip()), limit):
        total += 1
        if CYRILLIC_RE.search(v):
            cyr += 1
    return cyr / total if total else 0.0


def _lang_ok(filename: str, languages: Tuple[str, ...]) -> bool:
    """Accept only `<module>_<language>.<ext>` files (BSAs also ship german/russian/... tables!)."""
    stem = Path(filename.lower()).stem
    return any(stem.endswith("_" + lang) for lang in languages)


def _is_heavy_bsa(path: Path) -> bool:
    low = path.name.lower()
    return any(tok in low for tok in HEAVY_BSA_TOKENS)


def auto_detect_game_data_dir() -> Optional[Path]:
    """Locate `Skyrim Special Edition/Data` from config, env, Windows registry, or default Steam paths."""
    cands: List[Path] = []
    env_val = os.environ.get("SKYRIM_DATA_DIR")
    if env_val:
        cands.append(Path(env_val))
    try:
        cands.append(get_game_data_dir())
    except Exception:
        pass
    try:
        import winreg  # type: ignore

        for key_path in (
            r"SOFTWARE\WOW6432Node\Bethesda Softworks\Skyrim Special Edition",
            r"SOFTWARE\Bethesda Softworks\Skyrim Special Edition",
        ):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as k:
                    cands.append(Path(winreg.QueryValueEx(k, "Installed Path")[0]) / "Data")
            except OSError:
                continue
    except ImportError:
        pass
    for base in (
        Path("C:/Program Files (x86)/Steam/steamapps/common/Skyrim Special Edition/Data"),
        Path("C:/Program Files/Steam/steamapps/common/Skyrim Special Edition/Data"),
        Path.home() / ".steam/steam/steamapps/common/Skyrim Special Edition/Data",
    ):
        cands.append(base)
    for c in cands:
        try:
            if c.is_dir():
                return c
        except OSError:
            continue
    return None


def _normalize_root(root: Path) -> Path:
    """If the user selected the game root (containing Data/), descend into Data."""
    if (root / "Data").is_dir() and not any(root.glob("*.bsa")):
        return root / "Data"
    return root


# --------------------------------------------------------------------------------------
# Scanners (loose files + BSA-packed)
# --------------------------------------------------------------------------------------

def _scan_string_tables(
    root: Path, recursive: bool, languages: Tuple[str, ...] = ("english",)
) -> Dict[Tuple[str, str], List[Tuple[str, Dict[int, str]]]]:
    """Return {(module, ext): [(origin_label, {string_id: text}), ...]} for loose and BSA-packed tables."""
    found: Dict[Tuple[str, str], List[Tuple[str, Dict[int, str]]]] = defaultdict(list)
    if not root.is_dir():
        return found

    loose: List[Path] = []
    seen: set = set()
    if recursive:
        iterator: Iterable[Path] = root.rglob("*")
    else:
        iterator = []
        for sub in ("Strings", "strings"):
            d = root / sub
            if d.is_dir():
                iterator = list(iterator) + list(d.iterdir())
        iterator = list(iterator) + list(root.iterdir())
    for f in iterator:
        try:
            if f.is_file() and f.suffix.lower() in STRING_EXTS and _lang_ok(f.name, languages):
                key = str(f.resolve()).lower()
                if key not in seen:
                    seen.add(key)
                    loose.append(f)
        except OSError:
            continue

    for sf in loose:
        try:
            mod, ext = extract_module_and_ext(sf.name)
            tbl = parse_string_table_bytes(sf.read_bytes(), is_length_prefixed=ext in ("dlstrings", "ilstrings"))
            if tbl:
                found[(mod, ext)].append((f"loose:{sf.name}", tbl))
        except Exception:
            continue

    bsas = root.rglob("*.bsa") if recursive else root.glob("*.bsa")
    for bsa_path in sorted(bsas, key=lambda p: p.name.lower()):
        if _is_heavy_bsa(bsa_path):
            continue
        try:
            archive = BSAArchive(bsa_path)
        except Exception:
            continue
        for entry in archive.entries:
            pl = entry["path_lower"]
            if not pl.endswith(STRING_EXTS):
                continue
            if not _lang_ok(Path(pl.replace("\\", "/")).name, languages):
                continue
            try:
                name = Path(pl.replace("\\", "/")).name
                mod, ext = extract_module_and_ext(name)
                raw = archive.extract_entry_bytes(entry)
                tbl = parse_string_table_bytes(raw, is_length_prefixed=ext in ("dlstrings", "ilstrings"))
                if tbl:
                    found[(mod, ext)].append((f"{bsa_path.name}::{name}", tbl))
            except Exception:
                continue
    return found


def _is_ui_txt_name(name_lower: str, parent_lower: str) -> bool:
    return name_lower.endswith(".txt") and (name_lower.startswith("translate_") or parent_lower == "translations")


def _scan_ui_files(
    root: Path, recursive: bool, languages: Tuple[str, ...] = ("english",)
) -> Dict[str, List[Tuple[str, Dict[str, str]]]]:
    """Return {filename_lower: [(origin, {$key: value}), ...]} for translate_*.txt and MCM translation files."""
    found: Dict[str, List[Tuple[str, Dict[str, str]]]] = defaultdict(list)
    if not root.is_dir():
        return found

    if recursive:
        iterator: Iterable[Path] = root.rglob("*.txt")
    else:
        paths: List[Path] = []
        for sub in ("Interface", "interface"):
            d = root / sub
            if d.is_dir():
                paths.extend(d.glob("*.txt"))
                paths.extend((d / "Translations").glob("*.txt") if (d / "Translations").is_dir() else [])
        iterator = paths
    seen: set = set()
    for tf in iterator:
        try:
            key = str(tf.resolve()).lower()
            if (
                key in seen
                or not _is_ui_txt_name(tf.name.lower(), tf.parent.name.lower())
                or not _lang_ok(tf.name, languages)
            ):
                continue
            seen.add(key)
            parsed = parse_translate_txt(tf)
            if parsed:
                found[tf.name.lower()].append((f"loose:{tf.name}", parsed))
        except Exception:
            continue

    bsas = root.rglob("*.bsa") if recursive else root.glob("*.bsa")
    for bsa_path in sorted(bsas, key=lambda p: p.name.lower()):
        if _is_heavy_bsa(bsa_path):
            continue
        try:
            archive = BSAArchive(bsa_path)
        except Exception:
            continue
        for entry in archive.entries:
            pl = entry["path_lower"].replace("\\", "/")
            parts = pl.split("/")
            if not _is_ui_txt_name(parts[-1], parts[-2] if len(parts) > 1 else "") or not _lang_ok(parts[-1], languages):
                continue
            try:
                parsed = parse_translate_txt_bytes(archive.extract_entry_bytes(entry))
                if parsed:
                    found[parts[-1]].append((f"{bsa_path.name}::{parts[-1]}", parsed))
            except Exception:
                continue
    return found


def _scan_plugins(root: Path, recursive: bool) -> Dict[str, List[Path]]:
    found: Dict[str, List[Path]] = defaultdict(list)
    if not root.is_dir():
        return found
    iterator = root.rglob("*") if recursive else root.iterdir()
    for p in iterator:
        try:
            if p.is_file() and p.suffix.lower() in PLUGIN_EXTS:
                found[p.name.lower()].append(p)
        except OSError:
            continue
    return found


def _pick_english(cands: List[Tuple[str, Any]], values_of: Callable[[Any], Iterable[str]]) -> Optional[Tuple[str, Any]]:
    """Choose the English candidate (lowest Cyrillic ratio, then largest)."""
    best = None
    best_score: Tuple[float, int] = (2.0, 0)
    for origin, tbl in cands:
        ratio = _cyr_ratio(values_of(tbl))
        if ratio > 0.3:
            continue
        score = (ratio, -len(tbl))
        if best is None or score < best_score:
            best, best_score = (origin, tbl), score
    return best


def _pick_ukrainian(cands: List[Tuple[str, Any]], values_of: Callable[[Any], Iterable[str]]) -> Optional[Tuple[str, Any]]:
    best = None
    best_score: Tuple[float, int] = (-1.0, 0)
    for origin, tbl in cands:
        ratio = _cyr_ratio(values_of(tbl))
        if ratio < 0.05:
            continue
        score = (ratio, len(tbl))
        if score > best_score:
            best, best_score = (origin, tbl), score
    return best


# --------------------------------------------------------------------------------------
# Standalone dictionary importers (.json / .csv / .tsv / .db / .sqlite / .sql)
# --------------------------------------------------------------------------------------

_EN_COLS = ("en_text", "en", "english", "original", "source", "src", "eng")
_UK_COLS = ("uk_text", "uk", "ukrainian", "translation", "translated", "target", "dest", "ua")


def _pair_ok(en: Any, uk: Any) -> bool:
    if not isinstance(en, str) or not isinstance(uk, str):
        return False
    en_s, uk_s = en.strip(), uk.strip()
    if not en_s or not uk_s or len(en_s) > 20000 or en_s == uk_s:
        return False
    return bool(CYRILLIC_RE.search(uk_s)) and not CYRILLIC_RE.search(en_s)


def _pairs_from_json(path: Path) -> List[Tuple[str, str]]:
    if path.stat().st_size > 300 * 1024 * 1024:
        return []
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    out: List[Tuple[str, str]] = []
    if isinstance(data, dict):
        for k, v in data.items():
            if _pair_ok(k, v):
                out.append((k.strip(), v.strip()))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                low = {str(k).lower(): v for k, v in item.items()}
                en = next((low[c] for c in _EN_COLS if c in low), None)
                uk = next((low[c] for c in _UK_COLS if c in low), None)
                if _pair_ok(en, uk):
                    out.append((en.strip(), uk.strip()))
            elif isinstance(item, (list, tuple)) and len(item) >= 2 and _pair_ok(item[0], item[1]):
                out.append((item[0].strip(), item[1].strip()))
    return out


def _pairs_from_csv(path: Path) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        delim = "\t" if path.suffix.lower() == ".tsv" or sample.count("\t") > sample.count(",") else ","
        for row in csv.reader(fh, delimiter=delim):
            en = next((c for c in row if c.strip() and not CYRILLIC_RE.search(c)), None)
            uk = next((c for c in row if CYRILLIC_RE.search(c)), None)
            if en and uk and _pair_ok(en, uk):
                out.append((en.strip(), uk.strip()))
    return out


def _pairs_from_sqlite(conn: sqlite3.Connection) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE '%_fts%';")]
    for t in tables:
        try:
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}");')]
        except sqlite3.Error:
            continue
        low = {c.lower(): c for c in cols}
        en_c = next((low[c] for c in _EN_COLS if c in low), None)
        uk_c = next((low[c] for c in _UK_COLS if c in low), None)
        if not en_c or not uk_c:
            continue
        try:
            for en, uk in conn.execute(f'SELECT "{en_c}", "{uk_c}" FROM "{t}";'):
                if _pair_ok(en, uk):
                    out.append((en.strip(), uk.strip()))
        except sqlite3.Error:
            continue
    return out


def _import_dictionary_files(uk_root: Path, progress: Callable[[str], None]) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    own_db = DB_PATH.resolve()
    for f in uk_root.rglob("*"):
        try:
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext == ".json":
                got = _pairs_from_json(f)
            elif ext in (".csv", ".tsv"):
                got = _pairs_from_csv(f)
            elif ext in (".db", ".sqlite", ".sqlite3"):
                if f.resolve() == own_db:
                    continue
                conn = sqlite3.connect(f"file:{f.as_posix()}?mode=ro", uri=True)
                try:
                    got = _pairs_from_sqlite(conn)
                finally:
                    conn.close()
            elif ext == ".sql":
                if f.stat().st_size > 300 * 1024 * 1024:
                    continue
                conn = sqlite3.connect(":memory:")
                try:
                    conn.executescript(f.read_text(encoding="utf-8-sig", errors="replace"))
                    got = _pairs_from_sqlite(conn)
                finally:
                    conn.close()
            else:
                continue
        except Exception as exc:  # unreadable / foreign-format dictionaries are skipped, never fatal
            progress(f"  skipped dictionary {f.name}: {exc}")
            continue
        if got:
            progress(f"  dictionary {f.name}: {len(got):,} pairs")
            pairs.extend(got)
    return pairs


# --------------------------------------------------------------------------------------
# Main builder
# --------------------------------------------------------------------------------------

def build_tm_from_reference(
    request: TMBuildRequest,
    progress_callback: ProgressCb = None,
) -> TMBuildResult:
    """Build data/skyrim_tm.db and data/glossary_uk.json from English sources and a Ukrainian reference mod."""
    start = time.time()

    def report(msg: str, pct: float) -> None:
        if progress_callback:
            progress_callback(msg, pct)

    uk_root = Path(request.uk_reference_path).resolve()
    if not uk_root.exists():
        raise FileNotFoundError(f"Ukrainian reference path does not exist: {uk_root}")

    # ---- English roots ------------------------------------------------------------
    en_roots: List[Tuple[Path, bool]] = []  # (root, recursive)
    seen_roots: set = set()

    def add_root(p: Optional[Path], recursive: bool) -> None:
        if not p:
            return
        try:
            rp = _normalize_root(Path(p).resolve())
        except OSError:
            return
        if rp.is_dir() and str(rp).lower() not in seen_roots and rp != uk_root:
            seen_roots.add(str(rp).lower())
            en_roots.append((rp, recursive))

    if request.en_source_path and str(request.en_source_path).strip():
        add_root(Path(request.en_source_path), False)
    else:
        add_root(auto_detect_game_data_dir(), False)
        report("English path not specified: using auto-detected game Data folder.", 2.0)
    add_root(PROJECT_ROOT / "data" / "extracted_en", True)
    for extra in request.extra_en_roots:
        add_root(Path(extra), True)
    if not en_roots:
        raise FileNotFoundError(
            "No English source found. Point 'English Data path' to your Skyrim Special Edition\\Data folder."
        )
    report("English sources: " + "; ".join(str(r) for r, _ in en_roots), 3.0)

    # ---- Scan Ukrainian reference ------------------------------------------------
    report("Scanning Ukrainian reference (strings, BSA, MCM)...", 6.0)
    uk_langs = ("english", "ukrainian")
    uk_tables = _scan_string_tables(uk_root, recursive=True, languages=uk_langs)
    uk_ui = _scan_ui_files(uk_root, recursive=True, languages=uk_langs)
    uk_plugins = _scan_plugins(uk_root, recursive=True)
    report(
        f"Reference: {len(uk_tables)} string tables, {len(uk_ui)} UI/MCM files, {len(uk_plugins)} plugins.", 15.0
    )

    # ---- Scan English sources ----------------------------------------------------
    en_tables: Dict[Tuple[str, str], List[Tuple[str, Dict[int, str]]]] = defaultdict(list)
    en_ui: Dict[str, List[Tuple[str, Dict[str, str]]]] = defaultdict(list)
    en_plugins: Dict[str, List[Path]] = defaultdict(list)
    english_sources: List[str] = []
    for i, (root, rec) in enumerate(en_roots):
        report(f"Scanning English source: {root} ...", 15.0 + 15.0 * i / max(1, len(en_roots)))
        for k, v in _scan_string_tables(root, rec).items():
            en_tables[k].extend(v)
        for k, v in _scan_ui_files(root, rec).items():
            en_ui[k].extend(v)
        for k, v in _scan_plugins(root, rec).items():
            en_plugins[k].extend(v)
        english_sources.append(str(root))
    report(f"English: {len(en_tables)} string tables, {len(en_ui)} UI/MCM files.", 30.0)

    # ---- Prepare DB --------------------------------------------------------------
    request.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = SkyrimTranslationDB(request.db_path, auto_seed=False)
    db.init_schema(reset=request.reset_db)
    cur = db.conn.cursor()
    placeholders = ",".join("?" for _ in REFERENCE_GROUPS)
    cur.execute(f"DELETE FROM translations WHERE source_group IN ({placeholders});", REFERENCE_GROUPS)
    db.conn.commit()

    rows: List[Row] = []
    strings_pairs = plugin_pairs = mcm_pairs = dict_pairs = 0

    # ---- 1. String tables --------------------------------------------------------
    report("Aligning binary string tables by StringID...", 35.0)
    for key in sorted(set(uk_tables) & set(en_tables)):
        mod, ext = key
        uk_pick = _pick_ukrainian(uk_tables[key], lambda t: t.values())
        en_pick = _pick_english(en_tables[key], lambda t: t.values())
        if not uk_pick or not en_pick:
            continue
        en_tbl, uk_tbl = en_pick[1], uk_pick[1]
        group = "OPN" if mod in OPN_BASE_MODULES else "TPN"
        rec_type = ext.upper()
        for sid, en_str in en_tbl.items():
            uk_str = uk_tbl.get(sid)
            if not uk_str:
                continue
            en_c, uk_c = en_str.strip(), uk_str.strip()
            if not en_c or not uk_c or en_c == uk_c or not CYRILLIC_RE.search(uk_c):
                continue
            gloss = 1 if is_clean_glossary_candidate(en_c, uk_c, rec_type) else 0
            rows.append((group, mod, rec_type, sid, None, en_c, uk_c, gloss))
            strings_pairs += 1
    report(f"String-table pairs: {strings_pairs:,}", 55.0)

    # ---- 2. Plugins --------------------------------------------------------------
    report("Pairing translated plugins...", 58.0)
    for name in sorted(set(uk_plugins) & set(en_plugins)):
        try:
            uk_parsed = parse_plugin_file(uk_plugins[name][0])
            best_en: Optional[Dict[Tuple[str, str, str], str]] = None
            for en_path in en_plugins[name]:
                en_parsed = parse_plugin_file(en_path)
                emap = {
                    (r["base_form_id"], r["record_type"], r["subrecord_type"]): r["text"]
                    for r in en_parsed.get("records", [])
                    if r.get("text")
                }
                if emap and _cyr_ratio(emap.values()) <= 0.3:
                    best_en = emap
                    break
            if not best_en:
                continue
            for r in uk_parsed.get("records", []):
                uk_t = r.get("text")
                en_t = best_en.get((r["base_form_id"], r["record_type"], r["subrecord_type"]))
                if not uk_t or not en_t:
                    continue
                en_c, uk_c = en_t.strip(), uk_t.strip()
                if not en_c or not uk_c or en_c == uk_c or not CYRILLIC_RE.search(uk_c):
                    continue
                label = f"{r['record_type']}:{r['subrecord_type']}"
                gloss = 1 if is_clean_glossary_candidate(en_c, uk_c, label) else 0
                rows.append(("REFERENCE_MOD", Path(name).stem, label, None, r["base_form_id"], en_c, uk_c, gloss))
                plugin_pairs += 1
        except Exception:
            continue

    # ---- 3. MCM / UI translation files ------------------------------------------
    report("Pairing MCM / UI translation files...", 70.0)
    for fname in sorted(set(uk_ui) & set(en_ui)):
        uk_pick = _pick_ukrainian(uk_ui[fname], lambda t: t.values())
        en_pick = _pick_english(en_ui[fname], lambda t: t.values())
        if not uk_pick or not en_pick:
            continue
        for k, en_v in en_pick[1].items():
            uk_v = uk_pick[1].get(k)
            if not uk_v:
                continue
            en_c, uk_c = en_v.strip(), uk_v.strip()
            if not en_c or not uk_c or en_c == uk_c or not CYRILLIC_RE.search(uk_c):
                continue
            gloss = 1 if is_clean_glossary_candidate(en_c, uk_c, "MESG") else 0
            rows.append(("REFERENCE_MOD", fname, "MCM_UI", None, None, en_c, uk_c, gloss))
            mcm_pairs += 1

    # ---- 4. Standalone bilingual dictionaries -----------------------------------
    report("Importing bilingual dictionaries (.json/.csv/.db/.sql)...", 78.0)
    for en_c, uk_c in _import_dictionary_files(uk_root, lambda m: report(m, 78.0)):
        gloss = 1 if is_clean_glossary_candidate(en_c, uk_c, "STRINGS") else 0
        rows.append(("REFERENCE_DICT", "dictionary", "DICT", None, None, en_c, uk_c, gloss))
        dict_pairs += 1

    # ---- 5. Write DB -------------------------------------------------------------
    report(f"Writing {len(rows):,} pairs into skyrim_tm.db ...", 85.0)
    cur.executemany(
        "INSERT INTO translations (source_group, module_name, record_type, string_id, form_id, en_text, uk_text, is_glossary_term) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
        rows,
    )
    db.conn.commit()
    db.build_indexes_and_fts()

    # ---- 6. Glossary -------------------------------------------------------------
    glossary_count = 0
    if request.extract_glossary:
        report("Extracting canonical lore glossary...", 94.0)
        votes: Dict[str, Counter] = defaultdict(Counter)
        canon: Dict[str, str] = {}
        cur.execute(
            f"SELECT en_text, uk_text, record_type, source_group FROM translations "
            f"WHERE is_glossary_term = 1 AND source_group IN ({placeholders});",
            REFERENCE_GROUPS,
        )
        for en_t, uk_t, rtype, grp in cur.fetchall():
            low = en_t.lower()
            weight = (3 if grp == "OPN" else 1) + (2 if (rtype or "").endswith("FULL") or rtype == "STRINGS" else 0)
            votes[low][uk_t] += weight
            canon.setdefault(low, en_t)
        glossary = {canon[k]: c.most_common(1)[0][0] for k, c in votes.items()}
        request.glossary_path.parent.mkdir(parents=True, exist_ok=True)
        request.glossary_path.write_text(
            json.dumps(glossary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        glossary_count = len(glossary)

    total_rows = db.get_summary_counts()["total_rows"]
    db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    db.close()
    elapsed = round(time.time() - start, 2)
    report(f"Database ready: {total_rows:,} rows, {glossary_count:,} glossary terms ({elapsed}s)", 100.0)

    return TMBuildResult(
        strings_pairs_count=strings_pairs,
        plugin_pairs_count=plugin_pairs,
        mcm_pairs_count=mcm_pairs,
        dictionary_pairs_count=dict_pairs,
        total_tm_rows=total_rows,
        glossary_terms_count=glossary_count,
        db_path=request.db_path,
        glossary_path=request.glossary_path,
        elapsed_sec=elapsed,
        english_sources=english_sources,
    )
