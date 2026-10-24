"""
Phase 2 - Step 2: 3-Tier Hybrid Translation Engine (core/hybrid_translator.py)
Implements:
- Tier 1: Exact Translation Memory Lookup (<0.3 ms SQLite skyrim_tm.db across 100,184 OPN/TPN/SSE-AT pairs)
- Tier 2: Glossary-Enforced Neural Translator (deep-translator Google Engine / GTX JSON API + glossary_uk.json)
- Tier 3: Local LLM (Ollama qwen2.5:3b + Glossary RAG + Ukrainian morphology & Russianism post-processor)
"""

import json
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Dict, List, Tuple, Optional, Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.config import load_config
from core.database import SkyrimTranslationDB, DB_PATH, GLOSSARY_PATH
from core.llm_manager import (
    DEFAULT_MODEL,
    OLLAMA_GENERATE_URL,
    protect_engine_tags,
    restore_engine_tags,
    find_lore_terms_in_text,
    is_ollama_server_running,
)

# Canonical OPN/TPN phonetic & morphological normalization rules (fixing common MT & LLM Russianisms/transliterations)
CANONICAL_OPN_STEM_FIXES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bСтародавн(ий|им|их|ими)\b", re.IGNORECASE), r"Стародавн\1".replace("и", "і")),
    (re.compile(r"\bстародавн(ий)\b"), "стародавній"),
    (re.compile(r"\bСтародавний\b"), "Стародавній"),
    (re.compile(r"\bДракононароджен(ий|ого|ому|им|ім|і|их|ими)\b"), r"Драконородн\1"),
    (re.compile(r"\bдракононароджен(ий|ого|ому|им|ім|і|их|ими)\b"), r"драконородн\1"),
    (re.compile(r"\bСолітьюд(у|а|і|ом|е)?\b"), r"Солітюд\1"),
    (re.compile(r"\bВіндхельм(у|а|і|ом|е)?\b"), r"Віндгельм\1"),
    (re.compile(r"\bВінтерхолд(у|а|і|ом|е)?\b"), r"Вінтерголд\1"),
    (re.compile(r"\bРіфтен(у|а|і|ом|е)?\b"), r"Рифтен\1"),
    (re.compile(r"\bРівервуд(у|а|і|ом|е)?\b"), r"Ривервуд\1"),
    (re.compile(r"\bХелген(у|а|і|ом|е)?\b"), r"Гелґен\1"),
    (re.compile(r"\bХротгар(у|а|і|ом|е)?\b"), r"Гротґар\1"),
    (re.compile(r"\bДаедра\b"), "Дедра"),
    (re.compile(r"\bДаедричн(ий|ого|ому|им|ім|а|ої|ій|у|ою|е|і|их|ими)\b"), r"Дедричн\1"),
    (re.compile(r"\bдаедричн(ий|ого|ому|им|ім|а|ої|ій|у|ою|е|і|их|ими)\b"), r"дедричн\1"),
    (re.compile(r"\bГномськ(ий|ого|ому|им|ім|а|ої|ій|у|ою|е|і|их|ими)\b"), r"Двемерськ\1"),
    (re.compile(r"\bгномськ(ий|ого|ому|им|ім|а|ої|ій|у|ою|е|і|их|ими)\b"), r"двемерськ\1"),
    (re.compile(r"\bВзіркай\b"), "Віднеси"),
    (re.compile(r"\bвзіркай\b"), "віднеси"),
    (re.compile(r"\bStendarr\b"), "Стендарр"),
    (re.compile(r"\bTalos\b"), "Талос"),
    (re.compile(r"\bJulianos\b"), "Джуліанос"),
    (re.compile(r"\bKynareth\b"), "Кінарет"),
    (re.compile(r"\bAkatosh\b"), "Акатош"),
    (re.compile(r"\bArkay\b"), "Аркей"),
    (re.compile(r"\bDibella\b"), "Дібелла"),
    (re.compile(r"\bZenithar\b"), "Зенітар"),
]



def detect_hardware_capabilities() -> Dict[str, Any]:
    """Check GPU / VRAM and local Ollama status to recommend optimal translation tier."""
    gpu_name = "Unknown GPU"
    vram_mb = 0
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        ).strip()
        if out:
            parts = [p.strip() for p in out.splitlines()[0].split(",")]
            gpu_name = parts[0]
            vram_mb = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    except Exception:
        pass

    ollama_online, models = is_ollama_server_running(timeout=1.5)
    return {
        "gpu_name": gpu_name,
        "vram_mb": vram_mb,
        "ollama_online": ollama_online,
        "ollama_models": models,
        "recommended_tier": "tier2" if vram_mb < 8000 else "tier2+tier3",
    }


def apply_ukrainian_lore_postprocessing(
    uk_text: str,
    lore_terms: List[Tuple[str, str]],
) -> str:
    """
    Post-process translated Ukrainian text to enforce OPN/TPN canonical proper nouns
    and fix common morphological Russianisms (e.g. 'Стародавний' -> 'Стародавній',
    'Солітьюд' -> 'Солітюд', 'Дракононароджений' -> 'Драконородний').
    """
    result = uk_text

    # 1. Apply regex stem fixes for OPN canonical names & Ukrainian adjective endings
    for pattern, repl in CANONICAL_OPN_STEM_FIXES:
        result = pattern.sub(repl, result)

    # 2. Enforce any untriggered English proper nouns left in the output using matched glossary terms
    for en_term, uk_term in lore_terms:
        if en_term in result and en_term.lower() != uk_term.lower():
            result = re.sub(rf"\b{re.escape(en_term)}\b", uk_term, result)

    return result.strip()


class NeuralUnavailable(RuntimeError):
    """Raised when the neural backend timed out / failed or the circuit breaker is open."""


class CircuitBreaker:
    """Opens after `max_failures` consecutive failures; stays open for `cooldown_sec`, then half-opens."""

    def __init__(self, max_failures: int = 3, cooldown_sec: float = 30.0):
        self.max_failures = max_failures
        self.cooldown_sec = cooldown_sec
        self._failures = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()

    def configure(self, max_failures: int, cooldown_sec: float) -> None:
        self.max_failures = max_failures
        self.cooldown_sec = cooldown_sec

    def allow(self) -> bool:
        with self._lock:
            if self._failures < self.max_failures:
                return True
            if time.monotonic() - self._opened_at >= self.cooldown_sec:
                self._failures = self.max_failures - 1  # half-open: allow one probe
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.max_failures:
                self._opened_at = time.monotonic()

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._failures >= self.max_failures and (time.monotonic() - self._opened_at) < self.cooldown_sec


_cfg = load_config()
NEURAL_TIMEOUT_SEC: float = float(_cfg.get("neural_timeout_sec", 12.0))
NEURAL_BREAKER = CircuitBreaker(int(_cfg.get("circuit_breaker_failures", 15)), float(_cfg.get("circuit_breaker_cooldown_sec", 15.0)))

_THREAD_LOCAL = threading.local()


def get_requests_session() -> requests.Session:
    """Thread-local requests.Session with TLS verification, connection pooling, and bounded retries."""
    sess = getattr(_THREAD_LOCAL, "session", None)
    if sess is None:
        sess = requests.Session()
        sess.verify = True
        retries = Retry(total=1, backoff_factor=0.1, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=retries)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _THREAD_LOCAL.session = sess
    return sess


_GOOGLE_CLIENT_PROFILES: List[Tuple[str, str]] = [
    ("at", "GoogleTranslate/6.28 (Linux; U; Android 12; Pixel 6)"),
    ("at", "GoogleTranslate/6.30 (iPhone; iOS 15.4; Scale/3.00)"),
    ("it", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"),
    ("gtx", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"),
]


API_ERROR_SIGNATURES: List[str] = [
    "QUERY LENGTH LIMIT",
    "MAX ALLOWED QUERY",
    "MYMEMORY WARNING",
    "MYMEMORY",
    "завеликий запит",
    "переклад неможливий",
    "перевищено ліміт",
    "400 Bad Request",
    "Error 413",
    "Error 414",
    "Too Many Requests",
]


def _is_api_error_text(text: str) -> bool:
    """Check if the translation response is actually an API error message."""
    if not text or not text.strip():
        return True
    upper = text.upper()
    for sig in API_ERROR_SIGNATURES:
        if sig.upper() in upper:
            return True
    return False


def _gtx_post_translate(
    text: str,
    source: str = "en",
    target: str = "uk",
    timeout: Tuple[float, float] = (3.0, 8.0),
) -> str:
    """
    Send translation request via requests.Session to Google API using HTTP POST with strict socket timeouts.
    Tries desktop 'it', 'gtx', and mobile 'at' profiles.
    Guarantees no URL truncation (using POST body) and verifies no error message is returned.
    """
    if not NEURAL_BREAKER.allow():
        raise NeuralUnavailable("circuit breaker open")

    sess = get_requests_session()
    for client_id, user_agent in _GOOGLE_CLIENT_PROFILES:
        try:
            headers = {
                "User-Agent": user_agent,
                "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            }
            resp = sess.post(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": client_id, "sl": source, "tl": target, "dt": "t"},
                data={"q": text},
                headers=headers,
                timeout=timeout,
            )
            if resp.status_code == 200:
                data = json.loads(resp.content.decode("utf-8", errors="replace"))
                if data and data[0]:
                    translated = "".join(seg[0] for seg in data[0] if seg and seg[0])
                    if translated and not _is_api_error_text(translated):
                        NEURAL_BREAKER.record_success()
                        return translated
        except Exception:
            continue

    NEURAL_BREAKER.record_failure()
    raise NeuralUnavailable("google translation failed across all client tags")


def _chunk_and_translate_paragraphs(
    text: str,
    source: str = "en",
    target: str = "uk",
    chunk_limit: int = 800,
) -> str:
    """Sub-chunks a text by paragraph/newline boundaries into <=chunk_limit segments."""
    newline = "\r\n" if "\r\n" in text else "\n"
    paragraphs = text.split(newline)
    chunks: List[str] = []
    cur_chunk: List[str] = []
    cur_len = 0

    for p in paragraphs:
        p_len = len(p) + len(newline)
        if cur_len + p_len > chunk_limit and cur_chunk:
            chunks.append(newline.join(cur_chunk))
            cur_chunk = [p]
            cur_len = p_len
        else:
            cur_chunk.append(p)
            cur_len += p_len
    if cur_chunk:
        chunks.append(newline.join(cur_chunk))

    translated_chunks: List[str] = []
    for c in chunks:
        if not c.strip():
            translated_chunks.append(c)
            continue
        if len(c) > chunk_limit:
            # Paragraph itself is huge: split by words/characters
            sub_chunks = [c[i : i + chunk_limit] for i in range(0, len(c), chunk_limit)]
            tr_subs = [_gtx_post_translate(sc, source=source, target=target) for sc in sub_chunks]
            translated_chunks.append("".join(tr_subs))
        else:
            tr_c = _gtx_post_translate(c, source=source, target=target)
            translated_chunks.append(tr_c)

    return newline.join(translated_chunks)


def translate_long_book_text(
    en_text: str,
    source: str = "en",
    target: str = "uk",
    chunk_limit: int = 800,
) -> str:
    """
    Translates long Skyrim books (>chunk_limit characters) safely by chunking
    along [pagebreak] or paragraph boundaries, avoiding HTTP payload limits.
    """
    if len(en_text) <= chunk_limit:
        return _gtx_post_translate(en_text, source=source, target=target)

    # Detect pagebreak delimiter
    page_delim = None
    for cand in ("[pagebreak]", "<pagebreak>", "[PAGEBREAK]"):
        if cand in en_text:
            page_delim = cand
            break

    if page_delim:
        pages = en_text.split(page_delim)
        translated_pages = []
        for page in pages:
            if not page.strip():
                translated_pages.append(page)
                continue
            if len(page) <= chunk_limit:
                tr_page = _gtx_post_translate(page, source=source, target=target)
            else:
                tr_page = _chunk_and_translate_paragraphs(page, source=source, target=target, chunk_limit=chunk_limit)
            translated_pages.append(tr_page)
        return page_delim.join(translated_pages)
    else:
        return _chunk_and_translate_paragraphs(en_text, source=source, target=target, chunk_limit=chunk_limit)


def _neural_translate_multi(
    text: str,
    source: str = "en",
    target: str = "uk",
    timeout: Tuple[float, float] = (3.0, 8.0),
) -> str:
    """
    Google Translation API with POST and automatic long-string chunker.
    MyMemory is completely removed to prevent 'QUERY LENGTH LIMIT' error strings.
    """
    if len(text) > 800:
        return translate_long_book_text(text, source=source, target=target, chunk_limit=800)
    return _gtx_post_translate(text, source=source, target=target, timeout=timeout)


def _google_neural_translate(
    text: str,
    source: str = "en",
    target: str = "uk",
    timeout: Optional[Any] = None,
) -> str:
    """
    Translate via Google Neural MT with HTTP POST and a hard socket timeout.
    """
    to = timeout if isinstance(timeout, tuple) else (3.0, 8.0)
    return _neural_translate_multi(text, source=source, target=target, timeout=to)


class HybridTranslator:
    """
    3-Tier Hybrid Skyrim Translation Engine:
      Tier 1: Exact SQLite Translation Memory (skyrim_tm.db, <0.3 ms)
      Tier 2: Glossary-Enforced Neural MT (Google Neural Engine + glossary_uk.json + OPN post-processor)
      Tier 3: Local LLM (Ollama qwen2.5:3b + Glossary RAG + Ukrainian grammar post-processor)
    """

    def __init__(
        self,
        db: Optional[SkyrimTranslationDB] = None,
        default_tier: str = "tier2",
        llm_model: str = DEFAULT_MODEL,
    ):
        self._owns_db = False
        if db is None and DB_PATH.exists():
            self.db = SkyrimTranslationDB(DB_PATH)
            self._owns_db = True
        else:
            self.db = db
        self.default_tier = default_tier
        self.llm_model = llm_model
        self.glossary_path = GLOSSARY_PATH
        self._mem_cache: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    def close(self) -> None:
        if self._owns_db and self.db is not None:
            self.db.close()
            self._owns_db = False

    def translate(
        self,
        en_text: str,
        record_type: str = "FULL",
        tier: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Translate an English Skyrim string to Ukrainian through the 3-Tier pipeline.
        tier:
          - None / "auto" / "tier2": Tier 1 (TM) -> Tier 2 (Google Neural + Glossary) -> Tier 3 (Ollama fallback)
          - "tier3":                 Tier 1 (TM) -> Tier 3 (Ollama Local LLM + Glossary + Grammar Cleanup)
          - "tier1":                 Tier 1 (TM only)
        """
        active_tier = (tier or self.default_tier).lower()
        t0 = time.perf_counter()

        if not en_text or not en_text.strip():
            return {
                "en_text": en_text,
                "uk_text": en_text,
                "tier_used": "EMPTY",
                "source": "PASSTHROUGH",
                "record_type": record_type,
                "protected_tags": [],
                "injected_glossary": [],
                "latency_ms": 0.0,
            }

        cache_key = (en_text, record_type, active_tier)
        if cache_key in self._mem_cache:
            cached = dict(self._mem_cache[cache_key])
            cached["latency_ms"] = round((time.perf_counter() - t0) * 1000.0, 3)
            return cached

        # =====================================================================
        # TIER 1: Exact Translation Memory Lookup (<0.3 ms)
        # =====================================================================
        if self.db is not None:
            exact_hit = self.db.lookup_exact(en_text, record_type=record_type)
            if exact_hit and (
                exact_hit["is_glossary_term"] == 1
                or exact_hit["en_text"].lower() != exact_hit["uk_text"].lower()
            ):
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                res = {
                    "en_text": en_text,
                    "uk_text": exact_hit["uk_text"],
                    "tier_used": "Tier 1 (Exact TM)",
                    "source": f"TM_EXACT ({exact_hit['source_group']}:{exact_hit['module_name']})",
                    "record_type": exact_hit["record_type"],
                    "protected_tags": [],
                    "injected_glossary": [],
                    "latency_ms": round(elapsed_ms, 3),
                }
                self._mem_cache[cache_key] = res
                return res

        if active_tier == "tier1":
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return {
                "en_text": en_text,
                "uk_text": en_text,
                "tier_used": "Tier 1 Miss",
                "source": "UNTRANSLATED",
                "record_type": record_type,
                "protected_tags": [],
                "injected_glossary": [],
                "latency_ms": round(elapsed_ms, 3),
            }

        # Protect Creation Engine tags (<Alias=...>, <Global=...>, [pagebreak], %s, \r\n)
        protected_en, tag_placeholders = protect_engine_tags(en_text)

        # Match Skyrim lore glossary terms in en_text
        lore_terms = find_lore_terms_in_text(en_text, db=self.db)

        # =====================================================================
        # TIER 2: Glossary-Enforced Neural Translator (Google + glossary_uk.json)
        # =====================================================================
        if active_tier in ("tier2", "auto"):
            try:
                raw_neural = _google_neural_translate(protected_en, source="en", target="uk")
                restored_uk = restore_engine_tags(raw_neural, tag_placeholders)
                final_uk = apply_ukrainian_lore_postprocessing(restored_uk, lore_terms)
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                res = {
                    "en_text": en_text,
                    "uk_text": final_uk,
                    "protected_input": protected_en,
                    "tier_used": "Tier 2 (Google Neural + Lore Glossary)",
                    "source": "GOOGLE_NEURAL+GLOSSARY",
                    "record_type": record_type,
                    "protected_tags": tag_placeholders,
                    "injected_glossary": lore_terms,
                    "latency_ms": round(elapsed_ms, 2),
                }
                self._mem_cache[cache_key] = res
                return res
            except Exception:
                # Fall through to Tier 3 local LLM if offline
                pass

        # =====================================================================
        # TIER 3: Local LLM (Ollama qwen2.5:3b + Glossary RAG + Post-Processing)
        # =====================================================================
        glossary_lines = "\n".join(f'- "{en_t}" = "{uk_t}"' for en_t, uk_t in lore_terms)
        tag_rule = (
            "2. Keep every placeholder like [[TAG_0]] EXACTLY as written in its exact position in the sentence "
            "(for example: 'Greetings, [[TAG_0]].' -> 'Вітаю, [[TAG_0]].'). Never delete or replace [[TAG_0]]!\n"
            if tag_placeholders
            else ""
        )
        system_prompt = (
            "You are an expert English-to-Ukrainian game localizer for The Elder Scrolls V: Skyrim (OPN/TPN standard).\n"
            "Translate the user's English text into natural, grammatically correct Ukrainian.\n"
            "CRITICAL RULES:\n"
            "1. Output ONLY the Ukrainian translation. No explanations, no notes, no quotes.\n"
            f"{tag_rule}"
            "3. Translate every English word into Ukrainian (e.g. 'Ancient' -> 'Стародавній', 'Take' -> 'Віднеси', 'Jarl' -> 'ярл') "
            "and apply the official Skyrim Ukrainian glossary (inflecting nouns properly for Ukrainian grammar cases):\n"
            f"{glossary_lines if glossary_lines else '(Use standard Skyrim Ukrainian terminology)'}"
        )

        payload = {
            "model": self.llm_model,
            "system": system_prompt,
            "prompt": protected_en,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 256},
        }
        req = urllib.request.Request(
            OLLAMA_GENERATE_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp_json = json.loads(resp.read().decode("utf-8"))

        raw_llm = (resp_json.get("response") or "").strip()
        for prefix in ("Український переклад:", "Переклад:"):
            if raw_llm.startswith(prefix):
                raw_llm = raw_llm[len(prefix) :].strip()
        if not (en_text.startswith('"') and en_text.endswith('"')):
            if (raw_llm.startswith('"') and raw_llm.endswith('"')) or (
                raw_llm.startswith("«") and raw_llm.endswith("»")
            ):
                raw_llm = raw_llm[1:-1].strip()

        restored_uk = restore_engine_tags(raw_llm, tag_placeholders)
        final_uk = apply_ukrainian_lore_postprocessing(restored_uk, lore_terms)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        res = {
            "en_text": en_text,
            "uk_text": final_uk,
            "protected_input": protected_en,
            "raw_llm_output": raw_llm,
            "tier_used": f"Tier 3 (Ollama {self.llm_model} + Lore Glossary + Post-Processor)",
            "source": f"LLM ({self.llm_model})",
            "record_type": record_type,
            "protected_tags": tag_placeholders,
            "injected_glossary": lore_terms,
            "latency_ms": round(elapsed_ms, 2),
        }
        self._mem_cache[cache_key] = res
        return res

    def _get_bulk_tm_map(self) -> Dict[str, Dict[str, Any]]:
        if not hasattr(self, "_bulk_tm_map") or self._bulk_tm_map is None:
            if self.db is not None:
                self._bulk_tm_map = self.db.load_memory_lookup_map()
            else:
                self._bulk_tm_map = {}
        return self._bulk_tm_map

    def translate_batch_deduplicated(
        self,
        strings: List[str],
        module_name: str = "CUSTOM_MOD",
        record_type: str = "FULL",
        max_workers: int = 8,
        batch_size: int = 35,
        persist_to_db: bool = True,
        on_batch_done: Optional[Callable[[int, int, int, int], None]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """
        High-throughput deduplicated batch translation for Skyrim SE/AE mods:
          1. Deduplicates unique English strings across the entire input list.
          2. Resolves exact hits from skyrim_tm.db and CURATED_MOD_UI_TRANSLATIONS (<1 ms).
          3. Shields all Creation Engine / UI tags (<mag>, <dur>, <area>, <25>, <Alias=...>,
             <Global=...>, [pagebreak], <font ...>, {NAME}, \\n, %s) with [[TAG_i]] placeholders.
          4. Translates remaining net-new unique strings in multi-line batches of 25-40 strings
             across `max_workers` threads using Google GTX Neural API + OPN Lore Glossary post-processing,
             each call bounded by the (3.0, 8.0)s timeout / circuit breaker from config.json.
          5. Commits every batch immediately into skyrim_tm.db (source_group='CUSTOM_MOD', WAL mode);
             strings that timed out are NOT cached so a re-run resumes exactly where it stopped.
        `on_batch_done(batch_idx, total_batches, resolved_unique, untranslated)` is called after every commit.
        """
        t0 = time.perf_counter()
        batch_size = max(25, min(80, batch_size))
        self.last_batch_stats = {"unique": 0, "tm_hits": 0, "net_new": 0, "batches": 0, "committed_rows": 0, "untranslated": 0}
        results: Dict[str, Dict[str, Any]] = {}
        unique_strings: List[str] = []
        seen_set = set()
        for s in strings:
            if s not in seen_set:
                seen_set.add(s)
                unique_strings.append(s)

        bulk_tm = self._get_bulk_tm_map()
        net_new_list: List[str] = []

        for s in unique_strings:
            if not s or not s.strip():
                results[s] = {
                    "en_text": s,
                    "uk_text": s,
                    "tier_used": "EMPTY",
                    "source": "PASSTHROUGH",
                }
                continue

            # 0. Check curated UI/Framework dictionary first
            if s in CURATED_MOD_UI_TRANSLATIONS:
                results[s] = {
                    "en_text": s,
                    "uk_text": CURATED_MOD_UI_TRANSLATIONS[s],
                    "tier_used": "Tier 1 (Curated UI/Lore)",
                    "source": "TM",
                }
                continue

            # 1. Check in-memory skyrim_tm.db map
            s_low = s.lower()
            if s_low in bulk_tm:
                hit = bulk_tm[s_low]
                uk_val = hit["uk_text"]
                src_grp = hit.get("source_group", "OPN")
                if src_grp == "CUSTOM_MOD":
                    uk_val = apply_ukrainian_lore_postprocessing(uk_val, [])
                    uk_val = _apply_skyrim_item_prefix_fixes(s, uk_val)
                results[s] = {
                    "en_text": s,
                    "uk_text": uk_val,
                    "tier_used": "Tier 1 (Exact TM)" if src_grp != "CUSTOM_MOD" else "Tier 2 (Cached Neural + Lore)",
                    "source": "TM" if src_grp != "CUSTOM_MOD" else "Glossary+Neural",
                }
                continue


            # 2. Check if purely numeric/punctuation/internal script token without letters
            if not re.search(r"[A-Za-z]", s):
                results[s] = {
                    "en_text": s,
                    "uk_text": s,
                    "tier_used": "SYMBOL_PASSTHROUGH",
                    "source": "TM",
                }
                continue

            net_new_list.append(s)

        if not net_new_list:
            self.last_batch_stats.update({"unique": len(unique_strings), "tm_hits": len(unique_strings)})
            return results

        # Prepare shielded strings for net_new_list
        prepared: List[Tuple[str, str, List[Tuple[str, str]], List[Tuple[str, str]]]] = []
        for s in net_new_list:
            prot_en, tag_phs = protect_engine_tags(s)
            # Protect raw single newlines inside multi-line descriptions so batch line splitting stays 1:1
            prot_single_line = prot_en.replace("\r\n", "\n").replace("\n", " [[NL]] ")
            lore_terms = find_lore_terms_in_text(s, db=self.db)
            prepared.append((s, prot_single_line, tag_phs, lore_terms))

        # Split into chunks of batch_size (isolating strings > 800 chars into dedicated single-item batches)
        batches: List[List[Tuple[str, str, List[Tuple[str, str]], List[Tuple[str, str]]]]] = []
        cur_batch: List[Tuple[str, str, List[Tuple[str, str]], List[Tuple[str, str]]]] = []
        cur_chars = 0
        for item in prepared:
            item_len = len(item[1]) + 1
            if len(item[0]) > 800:
                if cur_batch:
                    batches.append(cur_batch)
                    cur_batch = []
                    cur_chars = 0
                batches.append([item])
                continue
            if cur_batch and (len(cur_batch) >= batch_size or cur_chars + item_len > 2800):
                batches.append(cur_batch)
                cur_batch = []
                cur_chars = 0
            cur_batch.append(item)
            cur_chars += item_len
        if cur_batch:
            batches.append(cur_batch)

        batch_timeout = (3.0, 8.0)

        def _restore_and_postprocess(
            orig_en: str,
            raw_translated_line: str,
            tag_phs: List[Tuple[str, str]],
            lore_terms: List[Tuple[str, str]],
        ) -> str:
            line_uk = re.sub(r"\s*\[\[?\s*NL\s*\]?\]\s*", "\n", raw_translated_line.strip(), flags=re.IGNORECASE)
            restored = restore_engine_tags(line_uk, tag_phs)
            final = apply_ukrainian_lore_postprocessing(restored, lore_terms)
            final = _apply_skyrim_item_prefix_fixes(orig_en, final)
            return final

        def _translate_batch_safe(
            items: List[Tuple[str, str, List[Tuple[str, str]], List[Tuple[str, str]]]]
        ) -> List[Tuple[str, str, bool]]:
            if not items:
                return []
            if len(items) == 1:
                orig_en, prot_line, tag_phs, lore_terms = items[0]
                try:
                    if len(orig_en) > 800:
                        raw_out = translate_long_book_text(prot_line, source="en", target="uk", chunk_limit=800)
                    else:
                        raw_out = _neural_translate_multi(prot_line, source="en", target="uk", timeout=batch_timeout)
                    final_uk = _restore_and_postprocess(orig_en, raw_out, tag_phs, lore_terms)
                    return [(orig_en, final_uk, True)]
                except Exception:
                    return [(orig_en, orig_en, False)]

            joined = "\n".join(it[1] for it in items)
            try:
                raw_out = _neural_translate_multi(joined, source="en", target="uk", timeout=batch_timeout)
                lines = raw_out.split("\n")
                if len(lines) == len(items):
                    out = []
                    for (orig_en, _, tag_phs, lore_terms), tr_line in zip(items, lines):
                        final_uk = _restore_and_postprocess(orig_en, tr_line, tag_phs, lore_terms)
                        out.append((orig_en, final_uk, True))
                    return out
            except Exception:
                pass

            # Fast 1-level fallback without recursive explosion
            out = []
            for orig_en, prot_line, tag_phs, lore_terms in items:
                try:
                    if len(orig_en) > 800:
                        raw_out = translate_long_book_text(prot_line, source="en", target="uk", chunk_limit=800)
                    else:
                        raw_out = _neural_translate_multi(prot_line, source="en", target="uk", timeout=batch_timeout)
                    final_uk = _restore_and_postprocess(orig_en, raw_out, tag_phs, lore_terms)
                    out.append((orig_en, final_uk, True))
                except Exception:
                    out.append((orig_en, orig_en, False))
            return out

        committed_rows = 0
        resolved_unique = len(unique_strings) - len(net_new_list)
        failed_strings = 0
        total_batches = len(batches)
        done_net_new = 0

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_translate_batch_safe, b): idx for idx, b in enumerate(batches, 1)}
            for b_idx, fut in enumerate(as_completed(futures), 1):
                batch_out = fut.result()
                batch_rows: List[Tuple[str, str, str, str]] = []
                for orig_en, final_uk, ok in batch_out:
                    if not ok:
                        results[orig_en] = {
                            "en_text": orig_en,
                            "uk_text": orig_en,
                            "tier_used": "UNTRANSLATED (timeout / circuit breaker)",
                            "source": "UNTRANSLATED",
                        }
                        failed_strings += 1
                        continue
                    results[orig_en] = {
                        "en_text": orig_en,
                        "uk_text": final_uk,
                        "tier_used": "Tier 2 (Batch Neural + Lore Glossary)",
                        "source": "Glossary+Neural",
                    }
                    bulk_tm[orig_en.lower()] = {
                        "source_group": "CUSTOM_MOD",
                        "module_name": module_name,
                        "record_type": record_type,
                        "en_text": orig_en,
                        "uk_text": final_uk,
                        "is_glossary_term": 0,
                    }
                    batch_rows.append((module_name, record_type, orig_en, final_uk))

                done_net_new += len(batch_out)
                if persist_to_db and self.db is not None and batch_rows:
                    committed_rows += self.db.cache_custom_translations(batch_rows, source_group="CUSTOM_MOD")

                if b_idx % 5 == 0 or b_idx == total_batches:
                    pct = (done_net_new / len(net_new_list)) * 100.0 if net_new_list else 100.0
                    elapsed_so_far = time.perf_counter() - t0
                    speed = done_net_new / elapsed_so_far if elapsed_so_far > 0 else 0
                    print(
                        f"    [Batch {b_idx:3d}/{total_batches:3d}] {done_net_new:5d}/{len(net_new_list):5d} strings ({pct:5.1f}%) "
                        f"| {speed:4.0f} str/s | committed {committed_rows:,} rows to TM",
                        flush=True,
                    )

                if on_batch_done is not None:
                    resolved_unique += len(batch_rows)
                    on_batch_done(b_idx, total_batches, resolved_unique, failed_strings)

        self.last_batch_stats = {
            "unique": len(unique_strings),
            "tm_hits": len(unique_strings) - len(net_new_list),
            "net_new": len(net_new_list),
            "batches": len(batches),
            "committed_rows": committed_rows,
            "untranslated": failed_strings,
        }

        elapsed = time.perf_counter() - t0
        print(
            f"  [BatchTranslator] {module_name}: {len(unique_strings):,} unique strings "
            f"({len(unique_strings) - len(net_new_list):,} TM hits, {len(net_new_list):,} net-new in {len(batches)} batches, "
            f"{failed_strings} untranslated/timeouts) completed in {elapsed:.2f}s",
            flush=True,
        )
        return results


def _apply_skyrim_item_prefix_fixes(orig_en: str, uk_text: str) -> str:
    """Enforce OPN canonical Ukrainian prefixes for Spell Tomes, Scrolls, Potions, and Campfire gear."""
    res = uk_text
    if orig_en.startswith("Spell Tome:"):
        res = re.sub(
            r"^(?:Том заклинань|Книга заклинань|Том чарів|Заклинання Том|Том заклять)\s*:\s*",
            "Том заклять: ",
            res,
            flags=re.IGNORECASE,
        )
        if orig_en.startswith("Spell Tome: Conjure "):
            res = re.sub(
                r"^Том заклять:\s*(?:Заклини|Заклинання|Викликайте|Викликати|Чаклувати)\s+",
                "Том заклять: Виклик ",
                res,
                flags=re.IGNORECASE,
            )
    elif orig_en.startswith("Conjure "):
        res = re.sub(
            r"^(?:Заклини|Викликайте|Викликати)\s+",
            "Виклик ",
            res,
            flags=re.IGNORECASE,
        )
    elif orig_en.startswith("Scroll of "):
        res = re.sub(r"^Сувій з\s+", "Сувій: ", res, flags=re.IGNORECASE)
    elif orig_en.startswith("Draught of "):
        res = re.sub(r"^(?:Чернетка|Тяга| ковток)\s+", "Настій ", res, flags=re.IGNORECASE)
    elif orig_en.startswith("Philter of "):
        res = re.sub(r"^(?:Фільтр|Filter of)\s+", "Питво ", res, flags=re.IGNORECASE)
    elif orig_en.startswith("Potion of "):
        res = re.sub(r"^Зілля з\s+", "Зілля ", res, flags=re.IGNORECASE)

    # Campfire bed roll & divines cleanup
    if "Bed Roll" in orig_en:
        res = res.replace("пов’язки", "спальники").replace("пов'язки", "спальники").replace("Рулет ліжка", "Спальник")
    return res


# Curated canonical Ukrainian translations for SkyUI, MCM Helper, UIExtensions, AddItemMenu, and Campfire UI
CURATED_MOD_UI_TRANSLATIONS: Dict[str, str] = {
    # SkyUI Inventory & Magic Column Headers & Abbreviations
    "ALL": "УСЕ",
    "FILTER": "ФІЛЬТР",
    "STOLEN": "КРАДЕНЕ",
    "EQUIPPED": "СПОРЯДЖЕНО",
    "ENCHANTED": "ЗАЧАРОВАНО",
    "V/W": "Ц/В",
    "VALUE/WEIGHT": "ЦІНА/ВАГА",
    "SPELL COST": "ВАРТІСТЬ ЧАРІВ",
    "FIRST WORD": "ПЕРШЕ СЛОВО",
    "SECOND WORD": "ДРУГЕ СЛОВО",
    "THIRD WORD": "ТРЕТЄ СЛОВО",
    "SCHOOL": "ШКОЛА",
    "SKILL LEVEL": "РІВЕНЬ НАВИЧКИ",
    "TIME LEFT": "ЗАЛИШИЛОСЯ ЧАСУ",
    "SOURCE": "ДЖЕРЕЛО",
    "FAVORITE": "ОБРАНЕ",
    "TYPE": "ТИП",
    "MATERIAL": "МАТЕРІАЛ",
    "MAT": "МАТ",
    "CLASS": "КЛАС",
    "MAGNITUDE": "СИЛА",
    "MAG": "СИЛ",
    "DURATION": "ТРИВАЛІСТЬ",
    "DUR": "ТРИВ",
    "TIME": "ЧАС",
    "DAM": "ШКД",
    "WGT": "ВАГ",
    "VAL": "ЦІН",
    "ARM": "БРН",
    "WEAPONS": "ЗБРОЯ",
    "AMMO": "БОЄПРИПАСИ",
    "EFFECT": "ЕФЕКТ",
    "MOD CONFIGURATION": "НАЛАШТУВАННЯ МОДІВ",
    "Mod Configuration": "Налаштування модів",
    # SkyUI_SE.esp quest records
    "SKI_FavoritesManagerInstance": "SKI_FavoritesManagerInstance (Менеджер обраного SkyUI)",
    "SKI_ConfigManagerInstance": "SKI_ConfigManagerInstance (Менеджер конфігурації MCM)",
    "SKI_WidgetManagerInstance": "SKI_WidgetManagerInstance (Менеджер віджетів SkyUI)",
    "SKI_MainInstance": "SKI_MainInstance (Ядро інтерфейсу SkyUI)",
    "SKI_SettingsManagerInstance": "SKI_SettingsManagerInstance (Менеджер налаштувань SkyUI)",
    "SKI_ConfigMenuInstance": "SKI_ConfigMenuInstance (Меню налаштувань модів MCM)",
    "SKI_ActiveEffectsWidgetInstance": "SKI_ActiveEffectsWidgetInstance (Віджет активних ефектів)",
    # AddItemMenuSE.esp & AddItemMenuSE_english.txt
    "[AddItemMenuSE]": "[Меню предметів модів (AddItemMenuSE)]",
    "[AddItemMenuSE -  Search]": "[AddItemMenuSE — Пошук предметів]",
    "[AddItemMenuSE Pack]": "[Набір предметів AddItemMenuSE]",
    "Spell Tome: AddItemMenuSE": "Том заклять: AddItemMenuSE (Меню предметів)",
    "Spell Tome: AddItemMenuSE -  Search": "Том заклять: AddItemMenuSE — Пошук",
    "<font face'$HandwrittenFont'><font size='40'><p align='center'>\r\nAddItemMenuSE": "<font face'$HandwrittenFont'><font size='40'><p align='center'>\r\nМеню предметів AddItemMenuSE",
    "<font face'$HandwrittenFont'><font size='40'><p align='center'>\r\nAddItemMenuSE - Search": "<font face'$HandwrittenFont'><font size='40'><p align='center'>\r\nAddItemMenuSE — Пошук предметів",
    "AIM": "Кімната AddItemMenu",
    "AIM_ListMenu": "Меню списку AddItemMenu",
    "AIM_TextMenu": "Меню пошуку AddItemMenu",
    "AIM_ContainerScript": "Контейнер AddItemMenu",
    "AIM_Init": "Ініціалізація AddItemMenu",
    "Not found": "Не знайдено",
    "ERROR: AddItemMenu requires skse64 version 2.0.17": "ПОМИЛКА: AddItemMenu потребує SKSE64 версії 2.0.17 або новішої",
    "Potion/Food": "Зілля / Їжа",
    "SoulGem": "Камінь душ",
    "SpellTome": "Том заклять",
    "Ingot/Ore": "Зливок / Руда",
    "Search of {SEARCH_STRING}": "Пошук: {SEARCH_STRING}",
    "Copy:{NAME}": "Копія: {NAME}",
    "[Search Item]": "[Пошук предмета]",
    "Part {PART_NUMBER} : {PART_FIRSTITEM_CHAR} - {PART_LASTITEM_CHAR}": "Частина {PART_NUMBER} : {PART_FIRSTITEM_CHAR} - {PART_LASTITEM_CHAR}",
    # Campfire items
    "Tinder": "Трут",
    "Deadwood": "Сухостій",
    "Kindling": "Хмиз для розпалювання",
    "Hide Lace": "Шкіряний ремінець",
    "Small Fur Tent, Bed Roll": "Малий хутряний намет, спальник",
    "Small Fur Tent, 2x Bed Rolls": "Малий хутряний намет, 2 спальники",
    "Large Fur Tent, Bed Roll": "Великий хутряний намет, спальник",
    "Large Fur Tent, 2x Bed Rolls": "Великий хутряний намет, 2 спальники",
    "Large Fur Tent, 3x Bed Rolls": "Великий хутряний намет, 3 спальники",
    "Large Fur Tent, 4x Bed Rolls": "Великий хутряний намет, 4 спальники",
    "Small Leather Tent, Bed Roll": "Малий шкіряний намет, спальник",
    "Small Leather Tent, 2x Bed Rolls": "Малий шкіряний намет, 2 спальники",
    "Large Leather Tent, Bed Roll": "Великий шкіряний намет, спальник",
    "Large Leather Tent, 2x Bed Rolls": "Великий шкіряний намет, 2 спальники",
    "Large Leather Tent, 3x Bed Rolls": "Великий шкіряний намет, 3 спальники",
    "Large Leather Tent, 4x Bed Rolls": "Великий шкіряний намет, 4 спальники",
}


