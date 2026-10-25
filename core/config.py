"""
Project configuration (config.json / config.example.json), RORO data models,
security path-traversal guards, and MCM mod-title preservation helpers (core/config.py).
"""

import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config.example.json"

VALID_TITLE_MODES = ("keep_english", "bilingual", "translate")
DEFAULT_OUTPUT_PATCH_REL = "./output/[UA] Skyrim Translation Patch"


class InvalidTargetError(ValueError):
    """Raised when a target path, plugin, or configuration parameter is invalid."""


class PathTraversalError(SecurityError if "SecurityError" in dir() else ValueError):
    """Raised when an archive entry or output file path attempts to escape output_root."""


class TranslationTimeoutError(TimeoutError):
    """Raised when a neural or LLM translation request exceeds the configured socket timeout."""


@dataclass(frozen=False)
class TranslatorConfig:
    """Structured configuration model following the Receive-an-Object, Return-an-Object (RORO) pattern."""

    game_data_dir: str = "./game_data"
    mo2_mods_dir: str = "./mods"
    mo2_profile_dir: str = "./profiles/Default"
    output_patch_dir: str = "./output/[UA] Skyrim Translation Patch"
    reference_dir: str = "./reference"
    sseat_dir: str = "./tools/sseat"
    mcm_title_mode: str = "keep_english"
    strict_whitelist_mode: bool = True
    use_local_ollama: bool = False
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:3b"
    neural_timeout_sec: float = 8.0
    circuit_breaker_failures: int = 5
    circuit_breaker_cooldown_sec: float = 30.0
    batch_size: int = 35
    max_workers: int = 8

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "TranslatorConfig":
        if not isinstance(raw, dict):
            return cls()
        defaults = asdict(cls())
        merged: Dict[str, Any] = {}
        for k, default_val in defaults.items():
            val = raw.get(k, default_val)
            merged[k] = val
        if merged.get("mcm_title_mode") not in VALID_TITLE_MODES:
            merged["mcm_title_mode"] = "keep_english"
        merged["output_patch_dir"] = sanitize_output_patch_dir(merged.get("output_patch_dir"))
        return cls(**merged)


DEFAULT_CONFIG: Dict[str, Any] = TranslatorConfig().to_dict()


def sanitize_output_patch_dir(raw_path: str | Path | None) -> str:
    """
    Strict output path guard: the unified patch folder must never be the application root directory
    (or one of its parents), otherwise plugins and meta.ini would be written next to main.py.
    Falls back to ./output/[UA] Skyrim Translation Patch.
    """
    if not raw_path or not str(raw_path).strip():
        return DEFAULT_OUTPUT_PATCH_REL
    cand = Path(raw_path)
    resolved = (cand if cand.is_absolute() else PROJECT_ROOT / cand).resolve()
    root = PROJECT_ROOT.resolve()
    if resolved == root or resolved in root.parents:
        return DEFAULT_OUTPUT_PATCH_REL
    return str(raw_path)


def ensure_safe_output_path(target_path: Path | str, output_root: Path | str) -> Path:
    """
    Guard against Zip-Slip / BSA Path Traversal attacks.
    Verifies that `target_path.resolve().is_relative_to(output_root.resolve())`.
    """
    if not target_path or not output_root:
        raise InvalidTargetError("target_path and output_root must be non-empty paths.")
    resolved_root = Path(output_root).resolve()
    resolved_target = Path(target_path).resolve()
    if not resolved_target.is_relative_to(resolved_root):
        raise PathTraversalError(
            f"Blocked path traversal attempt: '{resolved_target}' is outside '{resolved_root}'."
        )
    return resolved_target


def resolve_portable_path(raw_path: str | Path, default_rel: str) -> Path:
    """Resolve a path relative to PROJECT_ROOT if it is not already absolute."""
    cand = Path(raw_path) if raw_path else Path(default_rel)
    if cand.is_absolute():
        return cand
    return (PROJECT_ROOT / cand).resolve()


def load_config_model(config_path: Optional[Path] = None) -> TranslatorConfig:
    """Load configuration as a structured TranslatorConfig RORO object."""
    target_cfg = config_path or CONFIG_PATH
    raw_data: Dict[str, Any] = {}
    if target_cfg.exists():
        try:
            raw_data = json.loads(target_cfg.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raw_data = {}
    elif EXAMPLE_CONFIG_PATH.exists():
        try:
            raw_data = json.loads(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raw_data = {}
    return TranslatorConfig.from_dict(raw_data)


def save_config_model(cfg: TranslatorConfig, config_path: Optional[Path] = None) -> Path:
    """Persist structured TranslatorConfig to config.json."""
    target_cfg = config_path or CONFIG_PATH
    cfg.output_patch_dir = sanitize_output_patch_dir(cfg.output_patch_dir)
    target_cfg.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target_cfg


def load_config() -> Dict[str, Any]:
    """Load configuration dictionary with safe defaults."""
    return load_config_model().to_dict()


def get_mcm_title_mode() -> str:
    return load_config_model().mcm_title_mode


def get_game_data_dir() -> Path:
    return resolve_portable_path(load_config_model().game_data_dir, "./game_data")


def get_mo2_mods_dir() -> Path:
    return resolve_portable_path(load_config_model().mo2_mods_dir, "./mods")


def get_mo2_profile_dir() -> Path:
    return resolve_portable_path(load_config_model().mo2_profile_dir, "./profiles/Default")


def get_output_patch_dir() -> Path:
    return resolve_portable_path(load_config_model().output_patch_dir, "./output/[UA] Skyrim Translation Patch")


def get_translated_mods_dir() -> Path:
    """Folder where every individually translated mod is stored (translated_mods/<ModName>/)."""
    return PROJECT_ROOT / "translated_mods"


def get_reference_dir() -> Path:
    return resolve_portable_path(load_config_model().reference_dir, "./reference")


def get_sseat_dir() -> Path:
    return resolve_portable_path(load_config_model().sseat_dir, "./tools/sseat")


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-zа-яіїєґ]", "", text.lower())


def build_mod_aliases(*names: Optional[str]) -> set:
    """Normalized aliases for a mod: folder name, plugin stem, and prefix before ' - '."""
    aliases = set()
    for n in names:
        if not n:
            continue
        stem = Path(n).stem if n.lower().endswith((".esp", ".esm", ".esl", ".txt")) else n
        stem = re.sub(r"_english$", "", stem, flags=re.IGNORECASE)
        for cand in (stem, stem.split(" - ")[0], re.sub(r"\(.*?\)", "", stem)):
            nc = _norm(cand)
            if len(nc) >= 3:
                aliases.add(nc)
    return aliases


_TITLE_KEY_RE = re.compile(
    r"(modname|mod_name|modtitle|mod_title|pluginname|plugin_name|(^|[_\s])title$)",
    re.IGNORECASE,
)
_MCM_QUEST_EDID_RE = re.compile(r"(mcm|configmenu|ski_?config|menuquest)", re.IGNORECASE)


def is_mod_title_key(key: str, value: str, aliases: Iterable[str]) -> bool:
    """Detect MCM translation entry representing the mod's own header title in SkyUI."""
    if not key:
        return False
    k = key.lstrip("$")
    if _TITLE_KEY_RE.search(k):
        return True
    return bool(value) and len(value) <= 80 and _norm(value) in set(aliases) and bool(_norm(value))


def is_mcm_quest_title(
    record_type: str,
    subrecord_type: str,
    editor_id: str,
    value: str,
    aliases: Iterable[str],
) -> bool:
    """Detect QUST:FULL of an SKI_ConfigBase quest whose display name matches the mod title."""
    if record_type != "QUST" or subrecord_type != "FULL":
        return False
    if _norm(value) in set(aliases) and _norm(value):
        return True
    return bool(_MCM_QUEST_EDID_RE.search(editor_id or "")) and len(value) <= 80


def apply_mcm_title_mode(en_text: str, uk_text: str, mode: Optional[str] = None) -> str:
    """Format MCM mod title according to keep_english, bilingual, or translate mode."""
    active_mode = mode or get_mcm_title_mode()
    if active_mode == "keep_english":
        return en_text
    if active_mode == "bilingual":
        if uk_text and uk_text.strip().lower() != en_text.strip().lower():
            return f"{en_text} ({uk_text})"
        return en_text
    return uk_text
