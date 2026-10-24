"""
Unified Translation Memory & Lore Glossary Builder (core/database.py).
Manages the high-performance SQLite database at data/skyrim_tm.db (with COLLATE NOCASE index
and FTS5 virtual table translations_fts), auto-seeds from data/seed_tm_uk.json.gz on first run,
and exports the curated lore glossary to data/glossary_uk.json.
"""

import gzip
import json
import re
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.config import PROJECT_ROOT
from core.esp_parser import parse_plugin_file
from core.scanner import (
    GAME_DATA_DIR,
    OPN_BASE_MODULES,
    REFERENCE_DIR,
    classify_reference_module,
)
from core.sseat_inspector import run_sseat_inspection
from core.strings_parser import (
    extract_module_and_ext,
    parse_strings_file,
    parse_translate_txt,
)

DB_PATH = PROJECT_ROOT / "data" / "skyrim_tm.db"
SEED_TM_GZ_PATH = PROJECT_ROOT / "data" / "seed_tm_uk.json.gz"
GLOSSARY_PATH = PROJECT_ROOT / "data" / "glossary_uk.json"
EXTRACTED_EN_DIR = PROJECT_ROOT / "data" / "extracted_en"

CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
TAG_OR_CTRL_RE = re.compile(r"(<[^>]+>|\[[^\]]+\]|%[sdufx]|[\r\n\t])")
SENTENCE_PUNCT_RE = re.compile(r"[:?!;…\"«»]|(?:\.\.\.)")


def is_clean_glossary_candidate(en_text: str, uk_text: str, ext_or_rec: str) -> bool:
    """
    Determine if an aligned (en_text, uk_text) pair is a clean Proper Noun / Short Term:
    - 1 <= word_count <= 5
    - From .strings or FULL / SHRT subrecords
    - en_text != uk_text, uk_text contains Cyrillic
    - No newline/tags/format specifiers
    - Starts with a capital letter and is not a conversational sentence.
    """
    is_valid_source = (
        ext_or_rec == "STRINGS"
        or ext_or_rec.endswith(":FULL")
        or ext_or_rec == "FULL"
        or ext_or_rec.endswith(":SHRT")
    )
    if not is_valid_source:
        return False

    en = en_text.strip()
    uk = uk_text.strip()
    if not en or not uk:
        return False
    if len(en) < 2 or len(en) > 65 or len(uk) < 2 or len(uk) > 80:
        return False
    if en.lower() == uk.lower():
        return False
    if not CYRILLIC_RE.search(uk):
        return False
    if TAG_OR_CTRL_RE.search(en) or TAG_OR_CTRL_RE.search(uk):
        return False
    if SENTENCE_PUNCT_RE.search(en) or SENTENCE_PUNCT_RE.search(uk):
        return False
    if en.endswith(".") or uk.endswith(".") or en.endswith(",") or uk.endswith(","):
        return False
    if not en[0].isupper():
        return False

    words = en.split()
    return 1 <= len(words) <= 5


class SkyrimTranslationDB:
    """High-performance SQLite + FTS5 manager for Skyrim EN->UK Translation Memory."""

    def __init__(self, db_path: Path = DB_PATH, auto_seed: bool = True):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        is_new_file = not self.db_path.exists() or self.db_path.stat().st_size == 0
        self.conn = sqlite3.connect(str(self.db_path), timeout=60.0)
        self.conn.row_factory = sqlite3.Row
        self._configure_pragmas()
        if is_new_file and auto_seed:
            self.bootstrap_from_seed()

    def _configure_pragmas(self) -> None:
        cur = self.conn.cursor()
        cur.execute("PRAGMA journal_mode = WAL;")
        cur.execute("PRAGMA synchronous = NORMAL;")
        cur.execute("PRAGMA temp_store = MEMORY;")
        cur.execute("PRAGMA cache_size = -64000;")

    def init_schema(self, reset: bool = False) -> None:
        cur = self.conn.cursor()
        if reset:
            cur.execute("DROP TABLE IF EXISTS translations;")
            cur.execute("DROP TABLE IF EXISTS translations_fts;")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS translations (
                id INTEGER PRIMARY KEY,
                source_group TEXT,
                module_name TEXT,
                record_type TEXT,
                string_id INTEGER,
                form_id TEXT,
                en_text TEXT,
                uk_text TEXT,
                is_glossary_term INTEGER DEFAULT 0
            );
            """
        )
        cur.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS translations_fts USING fts5(
                en_text,
                uk_text,
                content='translations',
                content_rowid='id'
            );
            """
        )
        self.conn.commit()

    def bootstrap_from_seed(self) -> int:
        """
        Automatically initialize schema and populate skyrim_tm.db from data/seed_tm_uk.json.gz
        (or data/glossary_uk.json) when running on a fresh clone where skyrim_tm.db is not yet built.
        """
        self.init_schema(reset=False)
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM translations;")
        existing_count = cur.fetchone()[0]
        if existing_count > 0:
            return existing_count

        rows_to_insert: List[Tuple[str, str, str, Optional[int], Optional[str], str, str, int]] = []
        if SEED_TM_GZ_PATH.exists():
            try:
                raw_bytes = gzip.decompress(SEED_TM_GZ_PATH.read_bytes())
                seed_rows = json.loads(raw_bytes.decode("utf-8"))
                for sr in seed_rows:
                    rows_to_insert.append((sr[0], sr[1], sr[2], None, None, sr[3], sr[4], int(sr[5])))
            except Exception:
                rows_to_insert = []

        if not rows_to_insert and GLOSSARY_PATH.exists():
            try:
                gloss = json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))
                for en_k, uk_v in gloss.items():
                    rows_to_insert.append(("OPN", "glossary", "FULL", None, None, en_k, uk_v, 1))
            except Exception:
                pass

        if rows_to_insert:
            cur.executemany(
                """
                INSERT INTO translations (
                    source_group, module_name, record_type, string_id, form_id, en_text, uk_text, is_glossary_term
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                rows_to_insert,
            )
            self.conn.commit()
            self.build_indexes_and_fts()
        return len(rows_to_insert)

    def build_indexes_and_fts(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_translations_en_nocase ON translations(en_text COLLATE NOCASE);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_translations_mod_sid ON translations(module_name, string_id);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_translations_group ON translations(source_group);"
        )
        cur.execute("INSERT INTO translations_fts(translations_fts) VALUES('rebuild');")
        self.conn.commit()

    def lookup_exact(self, en_text: str, record_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Sub-millisecond exact case-insensitive lookup in translations."""
        if not en_text:
            return None
        cur = self.conn.cursor()
        if record_type:
            cur.execute(
                """
                SELECT id, source_group, module_name, record_type, string_id, form_id, en_text, uk_text, is_glossary_term
                FROM translations
                WHERE en_text = ? COLLATE NOCASE AND record_type LIKE ?
                ORDER BY
                    is_glossary_term DESC,
                    CASE WHEN lower(en_text) != lower(uk_text) THEN 0 ELSE 1 END,
                    CASE source_group WHEN 'OPN' THEN 1 WHEN 'TPN' THEN 2 ELSE 3 END
                LIMIT 1;
                """,
                (en_text, f"%{record_type}%"),
            )
            row = cur.fetchone()
            if row and (row["is_glossary_term"] == 1 or row["en_text"].lower() != row["uk_text"].lower()):
                return dict(row)

        cur.execute(
            """
            SELECT id, source_group, module_name, record_type, string_id, form_id, en_text, uk_text, is_glossary_term
            FROM translations
            WHERE en_text = ? COLLATE NOCASE
            ORDER BY
                is_glossary_term DESC,
                CASE WHEN lower(en_text) != lower(uk_text) THEN 0 ELSE 1 END,
                CASE source_group WHEN 'OPN' THEN 1 WHEN 'TPN' THEN 2 ELSE 3 END
            LIMIT 1;
            """,
            (en_text,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def search_fts(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Fast FTS5 full-text search across English and Ukrainian strings."""
        if not query or not query.strip():
            return []
        cur = self.conn.cursor()
        safe_q = '"' + query.replace('"', '""') + '"'
        cur.execute(
            """
            SELECT t.id, t.source_group, t.module_name, t.record_type, t.string_id, t.form_id, t.en_text, t.uk_text
            FROM translations_fts f
            JOIN translations t ON f.rowid = t.id
            WHERE translations_fts MATCH ?
            ORDER BY
                t.is_glossary_term DESC,
                CASE WHEN lower(t.en_text) != lower(t.uk_text) THEN 0 ELSE 1 END,
                length(t.en_text) ASC
            LIMIT ?;
            """,
            (safe_q, limit),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_summary_counts(self) -> Dict[str, Any]:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM translations;")
        total = cur.fetchone()[0]
        cur.execute("SELECT source_group, COUNT(*) FROM translations GROUP BY source_group;")
        by_group = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute("SELECT COUNT(*) FROM translations WHERE is_glossary_term = 1;")
        glossary_rows = cur.fetchone()[0]
        return {
            "total_rows": total,
            "by_source_group": by_group,
            "glossary_flagged_rows": glossary_rows,
        }

    def load_memory_lookup_map(self) -> Dict[str, Dict[str, Any]]:
        """
        Preload all canonical EN->UK pairs into an in-memory dictionary keyed by en_text.lower()
        for instant O(1) batch deduplication across thousands of strings.
        """
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT source_group, module_name, record_type, en_text, uk_text, is_glossary_term
            FROM translations
            ORDER BY
                CASE source_group
                    WHEN 'CUSTOM_MOD' THEN 4
                    WHEN 'SSE_AT' THEN 3
                    WHEN 'TPN' THEN 2
                    WHEN 'OPN' THEN 1
                    ELSE 5
                END DESC,
                is_glossary_term ASC
            """
        )
        mem: Dict[str, Dict[str, Any]] = {}
        for r in cur.fetchall():
            en = r["en_text"]
            uk = r["uk_text"]
            if not en or not uk:
                continue
            if r["is_glossary_term"] == 1 or en.lower() != uk.lower() or r["source_group"] == "CUSTOM_MOD":
                mem[en.lower()] = {
                    "source_group": r["source_group"],
                    "module_name": r["module_name"],
                    "record_type": r["record_type"],
                    "en_text": en,
                    "uk_text": uk,
                    "is_glossary_term": r["is_glossary_term"],
                }
        return mem

    def cache_custom_translations(
        self,
        entries: List[Tuple[str, str, str, str]],
        source_group: str = "CUSTOM_MOD",
    ) -> int:
        """
        Persist newly translated (module_name, record_type, en_text, uk_text) rows into skyrim_tm.db
        under source_group='CUSTOM_MOD'.
        """
        if not entries:
            return 0
        cur = self.conn.cursor()
        rows_to_insert = []
        for mod_name, rec_type, en_t, uk_t in entries:
            if not en_t or not uk_t:
                continue
            is_gloss = 1 if is_clean_glossary_candidate(en_t, uk_t, rec_type) else 0
            rows_to_insert.append((source_group, mod_name, rec_type, None, None, en_t, uk_t, is_gloss))

        if not rows_to_insert:
            return 0

        cur.executemany(
            """
            INSERT INTO translations (
                source_group, module_name, record_type, string_id, form_id, en_text, uk_text, is_glossary_term
            )
            SELECT ?, ?, ?, ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM translations WHERE source_group = ? AND en_text = ? AND uk_text = ?
            )
            """,
            [r + (r[0], r[5], r[6]) for r in rows_to_insert],
        )
        inserted = cur.rowcount
        self.conn.commit()
        return inserted if inserted is not None and inserted >= 0 else len(rows_to_insert)

    def close(self) -> None:
        self.conn.close()


def parse_all_game_plugins_string_map(data_dir: Path = GAME_DATA_DIR) -> Dict[Tuple[str, int], Tuple[str, str]]:
    """
    Parse all .esm/.esl/.esp files in Skyrim Data directory to map:
      (module_name_lower, string_id) -> (base_form_id, "RECORD:SUBRECORD")
    """
    sid_meta_map: Dict[Tuple[str, int], Tuple[str, str]] = {}
    if not data_dir.exists():
        return sid_meta_map

    plugin_files = sorted(
        [p for p in data_dir.iterdir() if p.is_file() and p.suffix.lower() in (".esm", ".esl", ".esp")],
        key=lambda p: p.name.lower(),
    )
    for pfile in plugin_files:
        mod_name = pfile.stem.lower()
        try:
            parsed = parse_plugin_file(pfile)
            for rec in parsed["records"]:
                sid = rec.get("string_id")
                if sid is not None:
                    key = (mod_name, sid)
                    rec_label = f"{rec['record_type']}:{rec['subrecord_type']}"
                    if key not in sid_meta_map or rec["subrecord_type"] == "FULL":
                        sid_meta_map[key] = (rec["base_form_id"], rec_label)
        except Exception:
            continue

    return sid_meta_map


def build_translation_memory_and_glossary(
    extracted_en_dir: Path = EXTRACTED_EN_DIR,
    ref_dir: Path = REFERENCE_DIR,
    data_dir: Path = GAME_DATA_DIR,
    db_path: Path = DB_PATH,
    glossary_path: Path = GLOSSARY_PATH,
) -> Dict[str, Any]:
    """
    Align extracted English .strings/.dlstrings/.ilstrings against Ukrainian OPN & TPN reference files,
    populate SQLite database data/skyrim_tm.db + FTS5 index, and export data/glossary_uk.json.
    """
    sid_meta_map = parse_all_game_plugins_string_map(data_dir)
    en_strings_dir = extracted_en_dir / "strings"
    en_files_map: Dict[Tuple[str, str], Path] = {}
    if en_strings_dir.exists():
        for f in en_strings_dir.iterdir():
            if f.is_file() and f.suffix.lower() in (".strings", ".dlstrings", ".ilstrings"):
                mod, ext = extract_module_and_ext(f.name)
                en_files_map[(mod, ext)] = f

    ref_strings_dir = ref_dir / "Strings"
    rows_to_insert: List[Tuple[str, str, str, Optional[int], Optional[str], str, str, int]] = []
    known_en_to_uk: Dict[str, str] = {}
    glossary_votes: Dict[str, Counter] = defaultdict(Counter)
    en_canonical_case: Dict[str, str] = {}

    opn_count = 0
    tpn_count = 0

    if ref_strings_dir.exists():
        ref_files = sorted(
            [
                f
                for f in ref_strings_dir.iterdir()
                if f.is_file() and f.suffix.lower() in (".strings", ".dlstrings", ".ilstrings")
            ],
            key=lambda p: p.name.lower(),
        )
        for uk_file in ref_files:
            mod, ext = extract_module_and_ext(uk_file.name)
            en_file = en_files_map.get((mod, ext))
            if not en_file:
                continue

            en_table = parse_strings_file(en_file)
            uk_table = parse_strings_file(uk_file)
            source_group = "OPN" if mod in OPN_BASE_MODULES else "TPN"
            default_rec_type = ext.upper()

            for sid, en_text in en_table.items():
                uk_text = uk_table.get(sid)
                if uk_text is None:
                    continue
                en_clean = en_text.strip()
                uk_clean = uk_text.strip()
                if not en_clean or not uk_clean:
                    continue

                form_id, rec_type = sid_meta_map.get((mod, sid), (None, default_rec_type))
                is_gloss = 1 if is_clean_glossary_candidate(en_clean, uk_clean, rec_type if form_id else default_rec_type) else 0

                rows_to_insert.append((
                    source_group,
                    mod,
                    rec_type,
                    sid,
                    form_id,
                    en_text,
                    uk_text,
                    is_gloss,
                ))
                if source_group == "OPN":
                    opn_count += 1
                else:
                    tpn_count += 1

                if en_clean not in known_en_to_uk or source_group == "OPN":
                    if CYRILLIC_RE.search(uk_clean):
                        known_en_to_uk[en_clean] = uk_clean
                        known_en_to_uk[en_clean.lower()] = uk_clean

                if is_gloss:
                    low_key = en_clean.lower()
                    weight = 3 if source_group == "OPN" else 1
                    if ":FULL" in rec_type:
                        weight += 2
                    glossary_votes[low_key][uk_clean] += weight
                    if low_key not in en_canonical_case or source_group == "OPN":
                        en_canonical_case[low_key] = en_clean

    if ref_dir.exists() and data_dir.exists():
        for ref_plugin in ref_dir.rglob("*"):
            if ref_plugin.is_file() and ref_plugin.suffix.lower() in (".esp", ".esl", ".esm"):
                orig_plugin = data_dir / ref_plugin.name
                if not orig_plugin.exists():
                    continue
                uk_parsed = parse_plugin_file(ref_plugin)
                orig_parsed = parse_plugin_file(orig_plugin)
                orig_map = {
                    (r["base_form_id"], r["record_type"], r["subrecord_type"]): r
                    for r in orig_parsed["records"]
                }
                group = classify_reference_module(ref_plugin.name)
                mod_name = ref_plugin.stem.lower()
                for uk_rec in uk_parsed["records"]:
                    uk_txt = uk_rec.get("text")
                    if not uk_txt:
                        continue
                    key = (uk_rec["base_form_id"], uk_rec["record_type"], uk_rec["subrecord_type"])
                    orig_rec = orig_map.get(key)
                    if not orig_rec:
                        continue
                    en_txt = orig_rec.get("text")
                    if not en_txt:
                        continue
                    rec_label = f"{uk_rec['record_type']}:{uk_rec['subrecord_type']}"
                    is_gloss = 1 if is_clean_glossary_candidate(en_txt, uk_txt, rec_label) else 0
                    rows_to_insert.append((
                        group,
                        mod_name,
                        rec_label,
                        None,
                        uk_rec["base_form_id"],
                        en_txt,
                        uk_txt,
                        is_gloss,
                    ))
                    if group == "OPN":
                        opn_count += 1
                    else:
                        tpn_count += 1

    en_tr_path = extracted_en_dir / "interface" / "translate_english.txt"
    uk_tr_path = ref_dir / "Interface" / "translate_english.txt"
    ui_matched = 0
    if en_tr_path.exists() and uk_tr_path.exists():
        en_ui = parse_translate_txt(en_tr_path)
        uk_ui = parse_translate_txt(uk_tr_path)
        for key, en_val in en_ui.items():
            uk_val = uk_ui.get(key)
            if not uk_val:
                continue
            en_c = en_val.strip()
            uk_c = uk_val.strip()
            if not en_c or not uk_c:
                continue
            is_gloss = 1 if is_clean_glossary_candidate(en_c, uk_c, "STRINGS") else 0
            rows_to_insert.append((
                "OPN",
                "translate_english",
                "UI_KEY",
                None,
                key,
                en_c,
                uk_c,
                is_gloss,
            ))
            opn_count += 1
            ui_matched += 1
            if is_gloss:
                low_key = en_c.lower()
                glossary_votes[low_key][uk_c] += 2
                en_canonical_case.setdefault(low_key, en_c)

    sseat_res = run_sseat_inspection(known_en_to_uk=known_en_to_uk, save_report=True)
    sseat_pairs = sseat_res["sseat_pairs"]
    sseat_count = 0
    for sp in sseat_pairs:
        en_c = sp["en_text"].strip()
        uk_c = sp["uk_text"].strip()
        rec_t = sp["record_type"]
        is_gloss = 1 if is_clean_glossary_candidate(en_c, uk_c, rec_t) else 0
        rows_to_insert.append((
            "SSEAT",
            sp["module_name"],
            rec_t,
            sp["string_id"],
            sp["form_id"],
            en_c,
            uk_c,
            is_gloss,
        ))
        sseat_count += 1

    tm_db = SkyrimTranslationDB(db_path, auto_seed=False)
    if rows_to_insert:
        tm_db.init_schema(reset=True)
        cur = tm_db.conn.cursor()
        cur.executemany(
            """
            INSERT INTO translations (
                source_group, module_name, record_type, string_id, form_id, en_text, uk_text, is_glossary_term
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            rows_to_insert,
        )
        tm_db.conn.commit()
        tm_db.build_indexes_and_fts()
    else:
        tm_db.bootstrap_from_seed()

    summary = tm_db.get_summary_counts()
    tm_db.close()

    if glossary_votes:
        curated_glossary: Dict[str, str] = {}
        for low_key in sorted(glossary_votes.keys()):
            best_uk, _votes = glossary_votes[low_key].most_common(1)[0]
            canon_en = en_canonical_case[low_key]
            curated_glossary[canon_en] = best_uk

        glossary_path.parent.mkdir(parents=True, exist_ok=True)
        with open(glossary_path, "w", encoding="utf-8") as f:
            json.dump(curated_glossary, f, ensure_ascii=False, indent=2, sort_keys=True)
        gloss_len = len(curated_glossary)
    else:
        gloss_len = 0

    return {
        "summary": summary,
        "opn_count": opn_count,
        "tpn_count": tpn_count,
        "sseat_count": sseat_count,
        "ui_matched": ui_matched,
        "glossary_count": gloss_len,
    }


if __name__ == "__main__":
    build_translation_memory_and_glossary()
