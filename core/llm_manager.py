"""
Step 4: Automatic Local LLM Setup & Lore-Aware Translation Engine (core/llm_manager.py)
Checks/installs Ollama, pulls qwen2.5:3b, and implements translate_with_lore_context(en_text, record_type, db)
with <1ms Translation Memory lookup, Creation Engine tag protection ([[TAG_0]]), OPN/TPN glossary injection,
and local Ollama inference.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

from core.config import load_config_model
from core.database import SkyrimTranslationDB, GLOSSARY_PATH, DB_PATH

OLLAMA_BASE_URL = load_config_model().ollama_url.rstrip("/")
OLLAMA_TAGS_URL = f"{OLLAMA_BASE_URL}/api/tags"
OLLAMA_GENERATE_URL = f"{OLLAMA_BASE_URL}/api/generate"
OLLAMA_DOWNLOAD_URL = "https://ollama.com/download/OllamaSetup.exe"
DEFAULT_MODEL = load_config_model().ollama_model

# Regex matching all Creation Engine tags & control tokens to protect with [[T0]] / [[TAG_i]]
ENGINE_TAG_RE = re.compile(
    r"(?:"
    r"<Alias[^>]*>"
    r"|<Global=[^>]+>"
    r"|<BaseName>"
    r"|</?(?:font|br|b|i|u|img|p|div)\b[^>]*>"
    r"|<(?:\d+|mag|dur|area)>"
    r"|\[pagebreak\]"
    r"|\{[A-Za-z0-9_]+\}"
    r"|\\n"
    r"|%(?:\d+\$)?[-+#0 ]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[sdufxXegcp%]"
    r"|\r\n"
    r")",
    re.IGNORECASE,
)

# Regex to match [[T0]] or [[TAG_0]] even if the MT/LLM slightly alters spacing/brackets/case
RESTORE_TAG_RE = re.compile(r"\[\[?\s*T(?:AG)?[_\s-]*(\d+)\s*\]?\]", re.IGNORECASE)


_CACHED_GLOSSARY_LOWER: Optional[Dict[str, Tuple[str, str]]] = None


def get_ollama_executable() -> Optional[str]:
    """Locate ollama.exe in PATH or %LOCALAPPDATA%\\Programs\\Ollama\\ollama.exe."""
    which_path = shutil.which("ollama")
    if which_path and os.path.exists(which_path):
        return which_path
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        candidate = os.path.join(local_app_data, "Programs", "Ollama", "ollama.exe")
        if os.path.exists(candidate):
            return candidate
    return None


def is_ollama_server_running(timeout: float = 2.0) -> Tuple[bool, List[str]]:
    """Check http://localhost:11434/api/tags and return (is_running, list_of_model_names)."""
    try:
        req = urllib.request.Request(OLLAMA_TAGS_URL, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                models = [m.get("name", "") for m in data.get("models", [])]
                return True, models
    except Exception:
        pass
    return False, []


def download_and_install_ollama() -> str:
    """Download OllamaSetup.exe with progress logging to temp dir and run silent installation."""
    temp_dir = Path(tempfile.gettempdir())
    installer_path = temp_dir / "OllamaSetup.exe"

    if not installer_path.exists() or installer_path.stat().st_size < 50_000_000:
        print(f"  - Downloading Ollama installer from {OLLAMA_DOWNLOAD_URL} -> {installer_path}...")
        req = urllib.request.Request(
            OLLAMA_DOWNLOAD_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp, open(installer_path, "wb") as out_f:
            total_size = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 1024 * 1024  # 1 MB chunks
            last_pct = -10
            t0 = time.time()
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                out_f.write(chunk)
                downloaded += len(chunk)
                if total_size > 0:
                    pct = int(downloaded * 100 / total_size)
                    if pct >= last_pct + 10 or downloaded == total_size:
                        last_pct = pct
                        mb_done = downloaded / (1024 * 1024)
                        mb_tot = total_size / (1024 * 1024)
                        speed = mb_done / max(0.1, time.time() - t0)
                        print(f"    [Download] {pct:3d}% ({mb_done:.1f} / {mb_tot:.1f} MB @ {speed:.1f} MB/s)")
    else:
        print(f"  - Using cached OllamaSetup.exe at {installer_path} ({installer_path.stat().st_size / (1024*1024):.1f} MB)")

    print("  - Running silent installation: OllamaSetup.exe /SILENT ...")
    subprocess.run([str(installer_path), "/SILENT", "/NORESTART"], check=False)

    # Wait briefly for ollama.exe to appear
    for _ in range(30):
        exe = get_ollama_executable()
        if exe:
            print(f"  - Ollama installed at: {exe}")
            return exe
        time.sleep(1.0)

    raise RuntimeError("Ollama installation finished, but ollama.exe was not found.")


def ensure_ollama_and_model(model_name: str = DEFAULT_MODEL) -> Dict[str, Any]:
    """
    Ensure Ollama is installed, the server is reachable at http://localhost:11434,
    and model qwen2.5:3b is pulled and ready.
    """
    print("=" * 72)
    print(f"[Step 4] Checking Local LLM Setup (Ollama + {model_name})...")
    running, models = is_ollama_server_running()
    exe_path = get_ollama_executable()

    if not running and not exe_path:
        print("  - Ollama is not installed. Starting automatic download & silent setup...")
        exe_path = download_and_install_ollama()
        running, models = is_ollama_server_running()

    if not running and exe_path:
        print(f"  - Starting Ollama server in background ({exe_path} serve)...")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(
            [exe_path, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        for _ in range(30):
            running, models = is_ollama_server_running(timeout=2.0)
            if running:
                break
            time.sleep(1.0)

    if not running:
        raise RuntimeError(f"Ollama server at {OLLAMA_BASE_URL} is not reachable.")

    print(f"  - Ollama server is online at {OLLAMA_BASE_URL} (available models: {models})")

    if not any(m == model_name or m.startswith(model_name) for m in models):
        exe_to_use = exe_path or "ollama"
        print(f"  - Pulling model '{model_name}' via '{exe_to_use} pull {model_name}'...")
        subprocess.run([exe_to_use, "pull", model_name], check=True)
        _, models = is_ollama_server_running()
        print(f"  - Model '{model_name}' pulled successfully. Active models: {models}")
    else:
        print(f"  - Model '{model_name}' is already present and ready.")

    return {
        "ollama_executable": exe_path,
        "server_url": OLLAMA_BASE_URL,
        "active_models": models,
        "target_model": model_name,
    }


def protect_engine_tags(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    """
    Replace engine tags (<Alias=...>, <Global=...>, [pagebreak], %s, \\r\\n, etc.)
    with sequential placeholders [[TAG_0]], [[TAG_1]], ...
    Returns (protected_text, [(placeholder, original_tag), ...]).
    """
    placeholders: List[Tuple[str, str]] = []

    def _repl(match: re.Match) -> str:
        idx = len(placeholders)
        ph = f"[[TAG_{idx}]]"
        placeholders.append((ph, match.group(0)))
        return ph

    protected = ENGINE_TAG_RE.sub(_repl, text)
    return protected, placeholders


def restore_engine_tags(translated_text: str, placeholders: List[Tuple[str, str]]) -> str:
    """Restore [[TAG_i]] placeholders back to their exact original engine tags."""
    if not placeholders:
        return RESTORE_TAG_RE.sub("", translated_text).strip()

    tag_map = {i: orig for i, (_ph, orig) in enumerate(placeholders)}

    def _restore_match(match: re.Match) -> str:
        idx = int(match.group(1))
        return tag_map.get(idx, "")

    restored = RESTORE_TAG_RE.sub(_restore_match, translated_text)

    # Guarantee that if the LLM omitted any tag placeholder, we preserve it
    for ph, orig in placeholders:
        if orig not in restored:
            restored = f"{restored} {orig}".strip()

    return restored


def _get_glossary_lower_map(glossary_path: Path = GLOSSARY_PATH) -> Dict[str, Tuple[str, str]]:
    """Load and cache glossary_uk.json as {en_lower: (en_canonical, uk_translation)}."""
    global _CACHED_GLOSSARY_LOWER
    if _CACHED_GLOSSARY_LOWER is not None:
        return _CACHED_GLOSSARY_LOWER

    _CACHED_GLOSSARY_LOWER = {}
    if glossary_path.exists():
        with open(glossary_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for en_k, uk_v in raw.items():
            _CACHED_GLOSSARY_LOWER[en_k.lower()] = (en_k, uk_v)
    return _CACHED_GLOSSARY_LOWER


# Common English stop-words and generic UI action verbs that should not be injected as standalone 1-word lore terms
STOP_WORDS = {
    "a", "an", "the", "of", "in", "on", "to", "for", "and", "or", "is", "are", "was",
    "were", "be", "with", "from", "by", "at", "as", "into", "if", "it", "this", "that",
    "you", "your", "my", "our", "his", "her", "their", "we", "they", "i", "me", "him",
    "them", "who", "what", "where", "when", "why", "how", "all", "any", "some", "no",
    "not", "yes", "can", "will", "just", "now", "then", "here", "there", "up", "down",
    "out", "over", "under", "again", "more", "most", "other", "such", "only", "own",
    "same", "so", "than", "too", "very", "have", "has", "had", "do", "does", "did",
    "take", "open", "close", "read", "talk", "search", "activate", "use", "equip",
    "drop", "give", "go", "come", "look", "wait", "sleep", "sit", "steal", "unlock",
    "greetings", "hello", "before", "after", "arrives", "arrive",
}


def find_lore_terms_in_text(
    en_text: str,
    db: Optional[SkyrimTranslationDB] = None,
    max_terms: int = 15,
) -> List[Tuple[str, str]]:
    """
    Search glossary_uk.json (and skyrim_tm.db fallback) for Skyrim lore terms, proper nouns,
    locations, items, and creatures appearing inside en_text.
    Matches longest multi-word phrases first (5 down to 1 word).
    """
    glossary_map = _get_glossary_lower_map()
    clean_text = ENGINE_TAG_RE.sub(" ", en_text)
    tokens = re.findall(r"[A-Za-z']+", clean_text)
    if not tokens:
        return []

    found_terms: List[Tuple[str, str]] = []
    seen_en_lower = set()
    covered_indices = set()

    n_tokens = len(tokens)
    for n in range(min(5, n_tokens), 0, -1):
        for i in range(n_tokens - n + 1):
            if any(idx in covered_indices for idx in range(i, i + n)):
                continue
            phrase_tokens = tokens[i : i + n]
            phrase = " ".join(phrase_tokens)
            low = phrase.lower()

            if n == 1 and low in STOP_WORDS:
                continue

            # Candidate variations (exact, stripped possessive 's, singular without trailing s)
            candidates = [low]
            if low.endswith("'s"):
                candidates.append(low[:-2])
            elif low.endswith("s") and len(low) > 4:
                candidates.append(low[:-1])

            matched_pair: Optional[Tuple[str, str]] = None
            for cand in candidates:
                if cand in STOP_WORDS:
                    continue
                if cand in glossary_map:
                    matched_pair = glossary_map[cand]
                    break

            if matched_pair:
                en_canon, uk_trans = matched_pair
                if en_canon.lower() not in seen_en_lower:
                    seen_en_lower.add(en_canon.lower())
                    found_terms.append((en_canon, uk_trans))
                    for idx in range(i, i + n):
                        covered_indices.add(idx)
                    if len(found_terms) >= max_terms:
                        return found_terms

    # Optional DB fallback if glossary_uk.json had no matches for capitalized words
    if not found_terms and db is not None:
        for tok in tokens:
            if tok[0].isupper() and tok.lower() not in STOP_WORDS:
                hit = db.lookup_exact(tok, record_type="FULL")
                if hit and hit.get("uk_text"):
                    found_terms.append((hit["en_text"], hit["uk_text"]))
                    if len(found_terms) >= max_terms:
                        break

    return found_terms


def translate_with_lore_context(
    en_text: str,
    record_type: str = "FULL",
    db: Optional[SkyrimTranslationDB] = None,
    model: str = DEFAULT_MODEL,
) -> Dict[str, Any]:
    """
    Context-aware Skyrim translation pipeline:
    - Step A: Check skyrim_tm.db for an exact 100% match first (<1 ms).
    - Step B: Protect engine tags (<Alias=...>, <Global=...>, [pagebreak], %s, \\r\\n) with [[TAG_0]].
    - Step C: Search glossary_uk.json / skyrim_tm.db for Skyrim lore terms appearing inside en_text
              and inject them into the LLM system prompt.
    - Step D: Query http://localhost:11434/api/generate (model="qwen2.5:3b", temperature=0.1)
              and restore protected tags.
    """
    t0 = time.perf_counter()
    close_db = False
    if db is None and DB_PATH.exists():
        db = SkyrimTranslationDB(DB_PATH)
        close_db = True

    try:
        # Step A: Exact 100% Translation Memory match
        if db is not None:
            exact_hit = db.lookup_exact(en_text, record_type=record_type)
            if exact_hit:
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                return {
                    "en_text": en_text,
                    "uk_text": exact_hit["uk_text"],
                    "source": f"TM_EXACT ({exact_hit['source_group']}:{exact_hit['module_name']})",
                    "record_type": exact_hit["record_type"],
                    "protected_tags": [],
                    "injected_glossary": [],
                    "latency_ms": round(elapsed_ms, 3),
                }

        # Step B: Protect engine tags with [[TAG_0]] placeholders
        protected_en, tag_placeholders = protect_engine_tags(en_text)

        # Step C: Search glossary_uk.json / skyrim_tm.db for lore terms inside en_text
        lore_terms = find_lore_terms_in_text(en_text, db=db)
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
            "3. Translate every English word into Ukrainian (e.g. 'Ancient' -> 'Стародавній', 'Jarl' -> 'ярл') and apply the official Skyrim Ukrainian glossary (inflecting nouns properly for Ukrainian grammar cases):\n"
            f"{glossary_lines if glossary_lines else '(Use standard Skyrim Ukrainian terminology)'}"
        )

        user_prompt = protected_en

        # Step D: Query Ollama API (http://localhost:11434/api/generate)
        payload = {
            "model": model,
            "system": system_prompt,
            "prompt": user_prompt,
            "stream": False,
            "options": {
                "temperature": 0.1,
                "num_predict": 256,
            },
        }
        req = urllib.request.Request(
            OLLAMA_GENERATE_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp_json = json.loads(resp.read().decode("utf-8"))

        raw_llm_out = (resp_json.get("response") or "").strip()

        # Clean up any accidental prefixes or wrapping quotes added by the model
        for prefix in ("Український переклад:", "Переклад:"):
            if raw_llm_out.startswith(prefix):
                raw_llm_out = raw_llm_out[len(prefix) :].strip()
        if not (en_text.startswith('"') and en_text.endswith('"')):
            if (raw_llm_out.startswith('"') and raw_llm_out.endswith('"')) or (
                raw_llm_out.startswith("«") and raw_llm_out.endswith("»")
            ):
                raw_llm_out = raw_llm_out[1:-1].strip()

        # Restore protected engine tags
        final_uk = restore_engine_tags(raw_llm_out, tag_placeholders)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        return {
            "en_text": en_text,
            "uk_text": final_uk,
            "protected_input": protected_en,
            "raw_llm_output": raw_llm_out,
            "source": f"LLM ({model})",
            "record_type": record_type,
            "protected_tags": tag_placeholders,
            "injected_glossary": lore_terms,
            "latency_ms": round(elapsed_ms, 2),
        }
    finally:
        if close_db and db is not None:
            db.close()
