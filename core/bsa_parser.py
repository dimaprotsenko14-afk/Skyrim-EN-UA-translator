"""
Bethesda BSA v104 / v105 Parser & Safe Extractor (core/bsa_parser.py).
Pure-Python BSA archive reader supporting version 104 (zlib) and version 105 (SSE LZ4)
with strict Path Traversal protection (`target_path.resolve().is_relative_to(output_root)`).
"""

import re
import struct
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import lz4.block
import lz4.frame

from core.config import (
    PROJECT_ROOT,
    InvalidTargetError,
    PathTraversalError,
    ensure_safe_output_path,
    get_game_data_dir,
)

GAME_DATA_DIR = get_game_data_dir()
EXTRACTED_EN_DIR = PROJECT_ROOT / "data" / "extracted_en"

CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")


class BSAArchive:
    """Pure-Python reader for Bethesda BSA v104 (TES5) and v105 (SSE/AE) archives."""

    def __init__(self, bsa_path: Path | str):
        if not bsa_path:
            raise InvalidTargetError("bsa_path must not be empty.")
        self.bsa_path = Path(bsa_path)
        if not self.bsa_path.exists():
            raise InvalidTargetError(f"BSA archive does not exist: {self.bsa_path}")
        self.version = 0
        self.archive_flags = 0
        self.folder_count = 0
        self.file_count = 0
        self.entries: List[Dict[str, Any]] = []
        self._parse_directory()

    def _parse_directory(self) -> None:
        with open(self.bsa_path, "rb") as f:
            hdr = f.read(36)
            if len(hdr) < 36:
                raise InvalidTargetError(f"Invalid BSA header in {self.bsa_path}")
            (
                magic,
                self.version,
                _folder_offset,
                self.archive_flags,
                self.folder_count,
                self.file_count,
                _total_folder_name_len,
                total_file_name_len,
                _file_flags,
            ) = struct.unpack("<4sIIIIIIII", hdr)

            if magic != b"BSA\x00" or self.version not in (104, 105):
                raise InvalidTargetError(
                    f"Unsupported BSA format in {self.bsa_path}: magic={magic!r}, version={self.version}"
                )

            folder_records: List[Tuple[int, int, int]] = []
            for _ in range(self.folder_count):
                if self.version == 105:
                    name_hash, count, _unk, offset = struct.unpack("<QIIQ", f.read(24))
                else:
                    name_hash, count, offset = struct.unpack("<QII", f.read(16))
                folder_records.append((name_hash, count, offset))

            has_dir_names = bool(self.archive_flags & 0x0001)
            has_file_names = bool(self.archive_flags & 0x0002)

            folders_files: List[Tuple[str, List[Tuple[int, int, int]]]] = []
            for _nh, count, _off in folder_records:
                if has_dir_names:
                    name_len = struct.unpack("<B", f.read(1))[0]
                    folder_name = f.read(name_len).rstrip(b"\x00").decode("latin1").replace("\\", "/")
                else:
                    folder_name = ""

                f_recs: List[Tuple[int, int, int]] = []
                for _ in range(count):
                    f_nh, f_sz, f_off = struct.unpack("<QII", f.read(16))
                    f_recs.append((f_nh, f_sz, f_off))
                folders_files.append((folder_name, f_recs))

            file_names: List[str] = []
            if has_file_names and total_file_name_len > 0:
                blob = f.read(total_file_name_len)
                file_names = [n.decode("latin1") for n in blob.split(b"\x00") if n]

            idx = 0
            default_compressed = bool(self.archive_flags & 0x0004)
            embed_file_names = bool(self.archive_flags & 0x0100)

            for folder_name, f_recs in folders_files:
                for f_nh, f_sz, f_off in f_recs:
                    fname = file_names[idx] if idx < len(file_names) else f"{f_nh:016x}.bin"
                    idx += 1
                    rel_path = f"{folder_name}/{fname}" if folder_name else fname
                    toggle_comp = bool(f_sz & 0x40000000)
                    is_compressed = default_compressed ^ toggle_comp
                    raw_size = f_sz & 0x3FFFFFFF
                    self.entries.append({
                        "path": rel_path,
                        "path_lower": rel_path.lower(),
                        "raw_size": raw_size,
                        "offset": f_off,
                        "is_compressed": is_compressed,
                        "embed_file_names": embed_file_names,
                    })

    def extract_entry_bytes(self, entry: Dict[str, Any], fh: Optional[Any] = None) -> bytes:
        """Read and decompress (if compressed) a single file entry from the BSA archive."""
        if not entry:
            raise InvalidTargetError("BSA entry dictionary must not be empty.")
        close_after = False
        if fh is None:
            fh = open(self.bsa_path, "rb")
            close_after = True
        try:
            fh.seek(entry["offset"])
            remaining = entry["raw_size"]

            if entry["embed_file_names"]:
                b_len = struct.unpack("<B", fh.read(1))[0]
                fh.read(b_len)
                remaining -= (1 + b_len)

            if not entry["is_compressed"]:
                return fh.read(remaining)

            uncomp_size = struct.unpack("<I", fh.read(4))[0]
            remaining -= 4
            comp_data = fh.read(remaining)
            if uncomp_size == 0:
                return b""

            if self.version == 104:
                return zlib.decompress(comp_data)
            try:
                return lz4.frame.decompress(comp_data)
            except Exception:
                return lz4.block.decompress(comp_data, uncompressed_size=uncomp_size)
        finally:
            if close_after and fh is not None:
                fh.close()

    def extract_entry_to_root(self, entry: Dict[str, Any], output_root: Path | str, fh: Optional[Any] = None) -> Path:
        """
        Safely extract an archive entry into `output_root` while enforcing
        Path Traversal protection (`target_path.resolve().is_relative_to(output_root)`).
        """
        root_resolved = Path(output_root).resolve()
        rel_clean = entry["path_lower"].lstrip("/\\")
        target_path = root_resolved / rel_clean
        if not target_path.resolve().is_relative_to(root_resolved):
            raise PathTraversalError(f"Blocked BSA path traversal attempt: {entry['path']}")
        ensure_safe_output_path(target_path, root_resolved)
        raw_bytes = self.extract_entry_bytes(entry, fh=fh)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(raw_bytes)
        return target_path


def check_loose_data_strings(data_dir: Path = GAME_DATA_DIR) -> Dict[str, Any]:
    """Check if loose files exist in Data/Strings and whether they contain Cyrillic characters."""
    strings_dir = data_dir / "Strings"
    info: Dict[str, Any] = {
        "exists": strings_dir.exists(),
        "file_count": 0,
        "contains_cyrillic": False,
        "use_bsa_for_english": True,
        "reason": "Data/Strings does not exist; using BSA archives for ground-truth English.",
    }
    if not strings_dir.exists():
        return info

    files = list(strings_dir.glob("*_english.*"))
    info["file_count"] = len(files)
    if not files:
        info["reason"] = "Data/Strings has no *_english.* files; using BSA archives."
        return info

    sample_file = files[0]
    raw = sample_file.read_bytes()[:65536]
    text_sample = raw.decode("utf-8", errors="ignore")
    if CYRILLIC_RE.search(text_sample):
        info["contains_cyrillic"] = True
        info["use_bsa_for_english"] = True
        info["reason"] = f"Data/Strings/{sample_file.name} contains Cyrillic; relying on BSA archives for original English."
    else:
        info["contains_cyrillic"] = False
        info["use_bsa_for_english"] = True
        info["reason"] = "Extracting directly from BSA archives to guarantee clean vanilla English."
    return info


def is_target_english_resource(rel_path_lower: str) -> bool:
    """Match strings/*_english.(strings|dlstrings|ilstrings) and interface/translate_english.txt."""
    if rel_path_lower == "interface/translate_english.txt":
        return True
    if rel_path_lower.startswith("strings/") and (
        rel_path_lower.endswith("_english.strings")
        or rel_path_lower.endswith("_english.dlstrings")
        or rel_path_lower.endswith("_english.ilstrings")
    ):
        return True
    return False


def extract_all_english_from_bsas(
    data_dir: Optional[Path] = None,
    out_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Extract all English localization files from Skyrim - Interface.bsa, Skyrim - Patch.bsa,
    _ResourcePack.bsa, and cc*.bsa archives into data/extracted_en/ with Path Traversal guards.
    """
    target_data_dir = Path(data_dir) if data_dir else get_game_data_dir()
    output_root = (Path(out_dir) if out_dir else EXTRACTED_EN_DIR).resolve()
    loose_check = check_loose_data_strings(target_data_dir)

    output_root.mkdir(parents=True, exist_ok=True)
    if not target_data_dir.exists():
        return {
            "loose_strings_check": loose_check,
            "bsas_scanned": 0,
            "bsas_with_strings": 0,
            "extracted_count": 0,
            "extracted_files": [],
        }

    all_bsas = sorted(target_data_dir.glob("*.bsa"), key=lambda p: p.name.lower())
    priority_bsas: List[Path] = []
    patch_bsas: List[Path] = []
    for bsa in all_bsas:
        name_l = bsa.name.lower()
        if "patch" in name_l:
            patch_bsas.append(bsa)
        elif name_l == "skyrim - interface.bsa" or name_l.startswith("cc") or name_l.startswith("_resourcepack"):
            priority_bsas.append(bsa)

    ordered_bsas = priority_bsas + patch_bsas
    extracted_files: List[Dict[str, Any]] = []
    bsas_with_strings = 0

    for bsa_path in ordered_bsas:
        archive = BSAArchive(bsa_path)
        matches = [e for e in archive.entries if is_target_english_resource(e["path_lower"])]
        if not matches:
            continue
        bsas_with_strings += 1
        with open(bsa_path, "rb") as fh:
            for entry in matches:
                target_path = output_root / entry["path_lower"].lstrip("/\\")
                if not target_path.resolve().is_relative_to(output_root):
                    raise PathTraversalError(f"Blocked BSA path traversal: {entry['path_lower']}")
                raw_bytes = archive.extract_entry_bytes(entry, fh=fh)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(raw_bytes)
                extracted_files.append({
                    "bsa": bsa_path.name,
                    "rel_path": entry["path_lower"],
                    "dest_path": str(target_path),
                    "size": len(raw_bytes),
                    "was_compressed": entry["is_compressed"],
                })

    return {
        "loose_strings_check": loose_check,
        "bsas_scanned": len(ordered_bsas),
        "bsas_with_strings": bsas_with_strings,
        "extracted_count": len(extracted_files),
        "extracted_files": extracted_files,
    }


if __name__ == "__main__":
    extract_all_english_from_bsas()
