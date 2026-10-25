# Changelog

## v1.1.1
### Fixed
- **Zero-Delta Plugin Guard (`core/mod_translator.py` & `core/patch_builder.py`)**:
  - Plugins that contain only blacklisted non-translatable records (such as `HDPT`-only appearance mods) or `0` modified safe records are no longer written into `translated_mods/` or copied into the final patch.
  - Only plugins where `modified_records_count > 0` are retained and merged into `[UA] Skyrim Translation Patch`.

### Added
- **Auto-Named `[UA] Skyrim Translation Patch\` Subfolder & Ready `.zip` Archive (`core/patch_builder.py`)**:
  - When building the unified patch into any user-selected directory, the builder automatically places all output files inside a clearly named `[UA] Skyrim Translation Patch\` subfolder and generates `[UA] Skyrim Translation Patch.zip` alongside it for 1-click Mod Organizer 2 / Vortex archive installation.
- `VERSION = "1.1.1"`.

## v1.1.0
### Fixed
- **Reference database builder (`core/tm_builder.py`)** produced an empty `skyrim_tm.db` for donor localizations:
  - recursively scans the donor folder for `.strings/.dlstrings/.ilstrings` (loose or inside `.bsa`), translated plugins,
    `translate_*.txt` / MCM files and bilingual dictionaries (`.json`, `.csv`, `.tsv`, `.db`, `.sqlite`, `.sql`);
  - the English side is auto-located (game `Data\Strings` or the official BSAs such as `Skyrim - Interface.bsa`) when the
    English path is blank; only `*_english.*` tables are used (BSAs also ship german/russian/... tables);
  - the English candidate is always the table with the lowest Cyrillic ratio, so an already deployed Ukrainian
    `Data\Strings` can never be mistaken for the English original.
- **Output pollution**: plugins and `meta.ini` were written into the application root. `output_patch_dir` can no longer be
  the application root (strict guard in `core/config.py`); a single translated mod goes to `translated_mods/<ModName>/`.
- `write_string_table_file()` keyword error when translating BSA-packed string tables.

### Added
- **Unified MO2 / Vortex patch builder** (`core/patch_builder.py`, `build_unified_mo2_patch(include_donor=True)`):
  donor translation as the base layer + translated mods on top. CLI: `--build-patch [--donor DIR] [--no-donor]`.
- `translate_modlist()` and CLI `--translate-modlist DIR` (no hard-coded waves).
- **Modern full-screen dashboard UI** (`customtkinter`): header with version badge and live DB status, two-column
  layout, donor import, single mod / modlist selector, patch + donor merger, live log with strings/sec and TM hit rate.
- `VERSION = "1.1.0"`.

### Changed
- `INDEX_DICTIONARY.html` no longer contains hard-coded wave / mod-count statistics: dashboard cards are computed from
  the mods really present in `translated_mods/` (translated mods, translated strings, TM hits, local TM size);
  the wave column/filter was replaced with **Тип ресурсу** (ESP/ESM/ESL, MCM TXT/JSON, Strings) and **Статус**.

## v1.0.0
- Initial public release: binary ESP/ESM/ESL parser and rebuilder with crash-proof GRUP pass-through, BSA v104/v105
  extractor, string-table and MCM translators, 3-tier hybrid translation pipeline (TM, glossary-enforced NMT, Ollama),
  HTML dictionaries, standalone GUI and CLI. Code-only distribution (no databases, no game assets).
