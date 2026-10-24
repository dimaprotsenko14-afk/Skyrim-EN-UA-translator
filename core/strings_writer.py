"""
Phase 2 - Step 1.1: Binary String Table & UI Translation Writer (core/strings_writer.py)
Implements Creation Engine binary string table compilation (.strings, .dlstrings, .ilstrings)
with string deduplication offsets and exact 8-byte header (count, data_size), plus
Scaleform UI translation writer (UTF-16 LE with BOM and $Key\\tValue\\r\\n).
"""

import struct
import tempfile
from pathlib import Path
from typing import Dict, Tuple

from core.strings_parser import parse_strings_file, parse_translate_txt


def compile_string_table_bytes(
    entries: Dict[int, str],
    is_length_prefixed: bool = False,
    deduplicate: bool = True,
) -> bytes:
    """
    Compile a {string_id: text} dictionary into Creation Engine binary string table bytes.
    - Header (8 bytes): struct.pack('<II', count, data_size)
    - Directory (count * 8 bytes): struct.pack('<II', string_id, offset)
    - Data section:
      * .strings (is_length_prefixed=False): UTF-8 bytes + b'\\x00'
      * .dlstrings / .ilstrings (is_length_prefixed=True): struct.pack('<I', len(utf8) + 1) + UTF-8 bytes + b'\\x00'
    """
    count = len(entries)
    if count == 0:
        return struct.pack("<II", 0, 0)

    directory_parts = []
    data_chunks = []
    current_offset = 0
    dedup_map: Dict[bytes, int] = {}

    for string_id, text in entries.items():
        encoded = (text or "").encode("utf-8") + b"\x00"
        if is_length_prefixed:
            payload = struct.pack("<I", len(encoded)) + encoded
        else:
            payload = encoded

        if deduplicate and payload in dedup_map:
            offset = dedup_map[payload]
        else:
            offset = current_offset
            if deduplicate:
                dedup_map[payload] = offset
            data_chunks.append(payload)
            current_offset += len(payload)

        directory_parts.append(struct.pack("<II", int(string_id) & 0xFFFFFFFF, offset))

    data_blob = b"".join(data_chunks)
    header = struct.pack("<II", count, len(data_blob))
    return header + b"".join(directory_parts) + data_blob


def write_string_table_file(
    entries: Dict[int, str],
    output_path: Path | str,
    deduplicate: bool = True,
) -> int:
    """
    Write entries {string_id: text} to output_path (.strings, .dlstrings, or .ilstrings).
    Returns number of bytes written.
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    ext = out.suffix.lower()
    is_len_prefixed = ext in (".dlstrings", ".ilstrings")
    blob = compile_string_table_bytes(entries, is_length_prefixed=is_len_prefixed, deduplicate=deduplicate)
    out.write_bytes(blob)
    return len(blob)


def write_translate_txt(entries: Dict[str, str], output_path: Path | str) -> int:
    """
    Write UI translation dictionary {$Key: Value} to output_path encoded as
    UTF-16 LE with BOM (\\xff\\xfe) and '$Key\\tValue\\r\\n' lines.
    Returns number of bytes written.
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for key, val in entries.items():
        k = key if key.startswith("$") else f"${key}"
        lines.append(f"{k}\t{val}\r\n")
    content = "".join(lines)
    raw_bytes = b"\xff\xfe" + content.encode("utf-16-le")
    out.write_bytes(raw_bytes)
    return len(raw_bytes)


def verify_strings_round_trip(original_path: Path | str) -> Tuple[bool, int]:
    """
    Verify that parsing an original .strings/.dlstrings/.ilstrings file with strings_parser.py,
    writing it with strings_writer.py, and parsing it again yields 100% identical
    (string_id -> text) dictionaries across all entries.
    """
    orig = Path(original_path)
    parsed_orig = parse_strings_file(orig)
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_file = Path(tmp_dir) / orig.name
        write_string_table_file(parsed_orig, tmp_file)
        parsed_rebuilt = parse_strings_file(tmp_file)

    is_identical = parsed_orig == parsed_rebuilt
    return is_identical, len(parsed_orig)


def verify_translate_txt_round_trip(original_path: Path | str) -> Tuple[bool, int]:
    """Verify round-trip identity for Interface/translate_*.txt files."""
    orig = Path(original_path)
    parsed_orig = parse_translate_txt(orig)
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_file = Path(tmp_dir) / orig.name
        write_translate_txt(parsed_orig, tmp_file)
        parsed_rebuilt = parse_translate_txt(tmp_file)

    is_identical = parsed_orig == parsed_rebuilt
    return is_identical, len(parsed_orig)
