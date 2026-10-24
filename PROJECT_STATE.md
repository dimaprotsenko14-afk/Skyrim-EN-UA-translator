# Project State & Architecture: Skyrim SE/AE Ukrainian Localization Pipeline

> **Target Skyrim Version:** Skyrim Special Edition / Anniversary Edition 1.6.1170+ (x64)  
> **Architecture Standard:** RORO (Receive an Object, Return an Object), Crash-Proof Raw GRUP Pass-Through, Path Traversal Guards  
> **Status:** Production Ready (GUI + CLI + 100,184 OPN/TPN/SSE-AT Seed Translation Memory)

---

## 1. High-Level Architecture & Data Flow

```
[Vanilla Skyrim SE/AE + CC BSA/ESM]      [OPN & TPN Ukrainian Reference]
                 │                                      │
                 ▼                                      ▼
    [core/bsa_parser.py]                   [core/strings_parser.py]
    [core/esp_parser.py]                   [core/database.py]
                 │                                      │
                 └──────────────┬───────────────────────┘
                                ▼
                   [SQLite Translation Memory]
                     (data/skyrim_tm.db: WAL Mode)
                     (Auto-seeded from data/seed_tm_uk.json.gz)
                                │
   ┌────────────────────────────┼────────────────────────────┐
   ▼                            ▼                            ▼
[Tier 1: O(1) Memory TM]   [Tier 2: Glossary+Neural]    [Tier 3: Local LLM]
(Exact Hits + Variants)     (Tag Shield + POST Chunker   (qwen2.5:3b via
                             + Circuit Breaker)           Ollama Fallback)
   │                            │                            │
   └────────────────────────────┼────────────────────────────┘
                                ▼
                   [core/checkpoint_manager.py]
                   (data/translation_checkpoint.json)
                                │
        ┌───────────────────────┴───────────────────────┐
        ▼                                               ▼
[Binary Plugin / String Writers]           [core/dictionary_exporter.py]
  - core/esp_writer.py                       - Single <Mod>_dictionary.html
  - core/strings_writer.py                   - Master INDEX_DICTIONARY.html
        │                                               │
        └───────────────────────┬───────────────────────┘
                                ▼
             [MO2 Non-Destructive Translation Patch]
               output/[UA] Skyrim Translation Patch
```

---

## 2. Core Modules

| Module / Path | Description |
|---|---|
| `skyrim_translator_gui.py` | Standalone zero-dependency `tkinter`/`ttk` dark-themed desktop GUI. |
| `main.py` | Unified CLI entry point supporting single-mod translation, MO2 batch waves, and DB management. |
| `fast_runner.py` | Interactive console batch runner with `tqdm` progress bars. |
| `core/config.py` | RORO configuration dataclasses (`TranslatorConfig`), `PathTraversalError` guards, and MCM title helpers. |
| `core/esp_parser.py` | Binary `.esp`/`.esm`/`.esl` parser with `zlib` decompression and `XXXX` extended subrecord support. |
| `core/esp_writer.py` | Structural binary plugin rebuilder with strict raw `GRUP` byte-slice pass-through. |
| `core/bsa_parser.py` | Pure-Python BSA v104 (`zlib`) and v105 (`LZ4`) parser with `is_relative_to` Path Traversal protection. |
| `core/strings_parser.py` | Binary reader for `.strings`, `.dlstrings`, `.ilstrings`, and UTF-16 LE `translate_*.txt`. |
| `core/strings_writer.py` | Binary string table compiler with offset deduplication and UTF-16 LE BOM writer. |
| `core/database.py` | SQLite TM (`skyrim_tm.db`) with WAL mode, FTS5 full-text search, and automatic seed bootstrapper. |
| `core/hybrid_translator.py` | 3-tier translation engine with HTTP POST book pagebreak chunker, circuit breaker, and OPN lore rules. |
| `core/llm_manager.py` | Optional local Ollama (`qwen2.5:3b`) manager and Creation Engine tag shield. |
| `core/mod_translator.py` | Universal single-mod / plugin / MCM / BSA translator (`ModTranslationRequest` -> `ModTranslationResult`). |
| `core/batch_orchestrator.py` | `ModpackBatchOrchestrator` for multi-wave Mod Organizer 2 modlist translation with `SIGINT` safety. |
| `core/modpack_scanner.py` | Scans Mod Organizer 2 `mods/` folders and classifies mods into translation waves. |
| `core/dictionary_exporter.py` | Exports interactive dark-themed HTML dictionaries and `INDEX_DICTIONARY.html`. |
