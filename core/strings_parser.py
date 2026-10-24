"""
Step 2.2: Binary String Table & UI Translation File Parser (core/strings_parser.py)
Implements the Creation Engine binary string table specification (.strings, .dlstrings, .ilstrings)
and Scaleform UI / MCM translation files (Interface/translate_*.txt, Interface/Translations/*.txt).
"""

import struct
from pathlib import Path
from typing import Dict, Tuple


def decode_skyrim_bytes(raw: bytes) -> str:
    """Decode bytes using utf-8 with fallback to cp1251 (Cyrillic) and cp1252 (Western)."""
    if not raw:
        return ""
    for encoding in ("utf-8", "cp1251", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_string_table_bytes(data: bytes, is_length_prefixed: bool = False) -> Dict[int, str]:
    """
    Parse Creation Engine binary string table bytes into {string_id: text}.
    - Header (8 bytes): count, data_size = struct.unpack('<II', data[:8])
    - Directory (count * 8 bytes): string_id, offset = struct.unpack('<II', ...)
    - Data section starts at data_start = 8 + count * 8:
      - .strings (is_length_prefixed=False): null-terminated bytes starting at data_start + offset up to \\x00
      - .dlstrings / .ilstrings (is_length_prefixed=True): length = struct.unpack('<I', data[pos:pos+4])[0]
        at pos = data_start + offset, then read length bytes (stripping trailing \\x00).
    """
    if len(data) < 8:
        return {}
    count, data_size = struct.unpack("<II", data[:8])
    if count == 0:
        return {}

    data_start = 8 + count * 8
    total_len = len(data)
    result: Dict[int, str] = {}

    for i in range(count):
        dir_pos = 8 + i * 8
        if dir_pos + 8 > total_len:
            break
        string_id, offset = struct.unpack_from("<II", data, dir_pos)
        pos = data_start + offset
        if pos >= total_len:
            result[string_id] = ""
            continue

        if is_length_prefixed:
            if pos + 4 > total_len:
                result[string_id] = ""
                continue
            length = struct.unpack_from("<I", data, pos)[0]
            str_start = pos + 4
            str_end = min(str_start + length, total_len)
            raw_bytes = data[str_start:str_end].rstrip(b"\x00")
        else:
            null_pos = data.find(b"\x00", pos)
            if null_pos != -1:
                raw_bytes = data[pos:null_pos]
            else:
                raw_bytes = data[pos:]

        result[string_id] = decode_skyrim_bytes(raw_bytes)

    return result


def parse_strings_file(file_path: Path | str) -> Dict[int, str]:
    """Parse a .strings, .dlstrings, or .ilstrings file from disk."""
    path = Path(file_path)
    ext = path.suffix.lower()
    is_len_prefixed = ext in (".dlstrings", ".ilstrings")
    data = path.read_bytes()
    return parse_string_table_bytes(data, is_length_prefixed=is_len_prefixed)


def parse_translate_txt_bytes(raw: bytes) -> Dict[str, str]:
    """
    Parse raw bytes of Interface/translate_*.txt or MCM Interface/Translations/*.txt
    (UTF-16 LE/BE with BOM or UTF-8) containing '$Key\\tValue' lines.
    """
    if not raw:
        return {}

    text = ""
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16", errors="replace")
    elif raw.startswith(b"\xef\xbb\xbf"):
        text = raw[3:].decode("utf-8", errors="replace")
    else:
        if b"\x00" in raw[:100]:
            text = raw.decode("utf-16-le", errors="replace")
        else:
            text = decode_skyrim_bytes(raw)

    entries: Dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip("\r\n")
        if not stripped or not stripped.startswith("$"):
            continue
        if "\t" in stripped:
            key, val = stripped.split("\t", 1)
        else:
            parts = stripped.split(None, 1)
            if len(parts) < 2:
                continue
            key, val = parts[0], parts[1]
        key = key.strip()
        val = val.strip("\t ")
        if key:
            entries[key] = val

    return entries


def parse_translate_txt(file_path: Path | str) -> Dict[str, str]:
    """
    Parse Interface/translate_*.txt or MCM Interface/Translations/*.txt files
    (UTF-16 LE/BE with BOM or UTF-8) containing '$Key\\tValue' lines.
    """
    path = Path(file_path)
    return parse_translate_txt_bytes(path.read_bytes())


def extract_module_and_ext(filename: str) -> Tuple[str, str]:
    """
    Given a filename like 'skyrim_english.DLSTRINGS' or 'ccbgssse001-fish_english.strings',
    return (module_name_lower, ext_lower_without_dot) e.g. ('skyrim', 'dlstrings').
    """
    name_l = filename.lower()
    ext = Path(name_l).suffix.lstrip(".")
    stem = Path(name_l).stem
    if stem.endswith("_english"):
        mod_name = stem[: -len("_english")]
    elif "_" in stem:
        mod_name = stem.rsplit("_", 1)[0]
    else:
        mod_name = stem
    return mod_name, ext
