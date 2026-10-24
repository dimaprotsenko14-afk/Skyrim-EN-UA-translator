"""
Bethesda BSA v104 / v105 Extractor (core/bsa_extractor.py).
Provides BSAArchive and safe extraction helpers with Path Traversal protection
(`target_path.resolve().is_relative_to(output_root)`).
"""

from pathlib import Path
from typing import Any, Dict

from core.bsa_parser import (
    CYRILLIC_RE,
    EXTRACTED_EN_DIR,
    GAME_DATA_DIR,
    BSAArchive,
    check_loose_data_strings,
    extract_all_english_from_bsas,
    is_target_english_resource,
)
from core.config import PathTraversalError, ensure_safe_output_path


def safe_extract_bsa_entry(
    archive: BSAArchive,
    entry: Dict[str, Any],
    output_root: Path | str,
    fh: Any = None,
) -> Path:
    """
    Extract a single BSA entry into `output_root` with strict Path Traversal validation.
    """
    resolved_root = Path(output_root).resolve()
    target_path = resolved_root / entry["path_lower"].lstrip("/\\")
    if not target_path.resolve().is_relative_to(resolved_root):
        raise PathTraversalError(f"Blocked BSA path traversal attempt: {entry['path_lower']}")
    ensure_safe_output_path(target_path, resolved_root)
    raw_bytes = archive.extract_entry_bytes(entry, fh=fh)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(raw_bytes)
    return target_path


__all__ = [
    "BSAArchive",
    "GAME_DATA_DIR",
    "EXTRACTED_EN_DIR",
    "CYRILLIC_RE",
    "check_loose_data_strings",
    "is_target_english_resource",
    "extract_all_english_from_bsas",
    "safe_extract_bsa_entry",
]
