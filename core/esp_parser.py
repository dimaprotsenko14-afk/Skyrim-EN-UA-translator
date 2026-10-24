"""
Step 2.3: Binary ESP / ESM / ESL Record Parser (core/esp_parser.py)
Fast pure-Python binary parser for Creation Engine .esp, .esm, and .esl plugin files.
Supports both Localized plugins (TES4 flag 0x00000080 -> uint32 StringID pointers)
and Non-Localized plugins (inline UTF-8/CP1251/CP1252 null-terminated strings),
zlib record decompression (flag 0x00040000), and XXXX 32-bit extended subrecords.
"""

import struct
import zlib
from pathlib import Path
from typing import Dict, List, Set, Tuple, Any

from core.strings_parser import decode_skyrim_bytes

# Strict allowed record types and whitelisted translatable subrecords
RECORD_SUBRECORD_WHITELIST: Dict[bytes, Set[bytes]] = {
    b"WEAP": {b"FULL", b"DESC"},
    b"ARMO": {b"FULL", b"DESC"},
    b"ALCH": {b"FULL", b"DESC"},
    b"INGR": {b"FULL"},
    b"SCRL": {b"FULL", b"DESC"},
    b"SLGM": {b"FULL"},
    b"KEYM": {b"FULL"},
    b"MISC": {b"FULL"},
    b"AMMO": {b"FULL", b"DESC"},
    b"BOOK": {b"FULL", b"DESC", b"CNAM"},
    b"SPEL": {b"FULL", b"DESC"},
    b"MGEF": {b"FULL", b"DNAM"},
    b"ENCH": {b"FULL"},
    b"PERK": {b"FULL", b"DESC", b"EPFD", b"EPF2"},
    b"SHOU": {b"FULL", b"DESC"},
    b"WOOP": {b"FULL", b"TNAM"},
    b"QUST": {b"FULL", b"CNAM", b"NNAM"},
    b"DIAL": {b"FULL"},
    b"INFO": {b"NAM1", b"RNAM"},
    b"MESG": {b"FULL", b"DESC", b"ITXT"},
    b"CONT": {b"FULL"},
    b"ACTI": {b"FULL", b"RNAM"},
    b"FLOR": {b"FULL", b"RNAM"},
    b"FURN": {b"FULL"},
    b"LIGH": {b"FULL"},
    b"TACT": {b"FULL"},
    b"TREE": {b"FULL", b"RNAM"},
    b"NPC_": {b"FULL", b"SHRT"},
    b"AVIF": {b"FULL", b"DESC"},
    b"GMST": {b"DATA"},
    b"LSCR": {b"DESC"},
}

STRICT_RECORD_BLACKLIST: Set[bytes] = {
    b"HDPT", b"RACE", b"CELL", b"WRLD", b"LCTN", b"REGN", b"NAVM",
    b"LAND", b"REFR", b"ACHR", b"PGRE", b"PHZD", b"WATR", b"WTHR",
    b"IDLE", b"PACK", b"ANIO", b"ARMA", b"STAT", b"SNDR", b"SOUN",
}

COSMETIC_OR_MORPH_KEYWORDS = (
    "high poly", "highpoly", "hair", "eye", "eyes", "makeup", "tint",
    "morph", "slider", "synthesis", "conflict_patch", "occlusion", "dyndolod",
    "texgen", "lux", "3bb", "3ba", "cbbe", "himbo", "unp", "body", "skeleton",
    "xpmsse", "physics", "hdt", "cbpc", "overlay"
)


def is_cosmetic_or_morph_plugin(name: str) -> bool:
    nl = name.lower()
    return any(k in nl for k in COSMETIC_OR_MORPH_KEYWORDS)


def parse_plugin_file(plugin_path: Path | str) -> Dict[str, Any]:
    """
    Parse a .esp, .esm, or .esl file and extract all whitelisted translatable subrecords.
    Returns:
      {
        "plugin_name": str,
        "is_localized": bool,
        "is_esl_flagged": bool,
        "records": [
          {
            "form_id": int,          # raw uint32 form_id
            "base_form_id": str,     # "0x00XXXXXX" (load order byte masked out)
            "record_type": str,      # e.g. "WEAP"
            "subrecord_type": str,   # e.g. "FULL"
            "editor_id": str,        # EDID if present
            "string_id": int | None, # uint32 StringID if localized
            "text": str | None,      # inline decoded text if non-localized
          }, ...
        ]
      }
    """
    path = Path(plugin_path)
    data = path.read_bytes()
    n = len(data)
    if n < 24 or data[:4] != b"TES4":
        raise ValueError(f"Not a valid TES4 plugin file: {path}")

    rtype, tes4_size, tes4_flags, _fid, _vc1, _ver, _vc2 = struct.unpack_from("<4sIIIIHH", data, 0)
    is_localized = bool(tes4_flags & 0x00000080)
    is_esl = bool(tes4_flags & 0x00000200) or path.suffix.lower() == ".esl"

    masters: List[str] = []
    tes4_end = min(24 + tes4_size, n)
    tpos = 24
    while tpos + 6 <= tes4_end:
        st = data[tpos : tpos + 4]
        ss = struct.unpack_from("<H", data, tpos + 4)[0]
        tpos += 6
        if st == b"MAST" and ss > 0 and tpos + ss <= tes4_end:
            mname = data[tpos : tpos + ss].rstrip(b"\x00").decode("utf-8", errors="replace")
            if mname:
                masters.append(mname)
        tpos += ss

    pos = 24 + tes4_size
    extracted_records: List[Dict[str, Any]] = []

    while pos + 24 <= n:
        rec_type = data[pos : pos + 4]
        if rec_type == b"GRUP":
            # GRUP header is 24 bytes; child records/sub-GRUPs immediately follow
            pos += 24
            continue

        data_size, flags, form_id, _vc1, _ver, _vc2 = struct.unpack_from("<IIIIHH", data, pos + 4)
        rec_end = pos + 24 + data_size
        if rec_end > n:
            break

        if rec_type in STRICT_RECORD_BLACKLIST:
            pos = rec_end
            continue

        if rec_type == b"NPC_" and is_cosmetic_or_morph_plugin(path.name):
            pos = rec_end
            continue

        allowed_subs = RECORD_SUBRECORD_WHITELIST.get(rec_type)
        if not allowed_subs or data_size == 0:
            pos = rec_end
            continue

        is_compressed = bool(flags & 0x00040000)
        rec_data = data[pos + 24 : rec_end]
        if is_compressed:
            if len(rec_data) <= 4:
                pos = rec_end
                continue
            decompressed_size = struct.unpack_from("<I", rec_data, 0)[0]
            try:
                rec_data = zlib.decompress(rec_data[4:])
            except zlib.error:
                pos = rec_end
                continue

        spos = 0
        sn = len(rec_data)
        ext_size = 0
        editor_id = ""
        last_epft = None
        rec_type_str = rec_type.decode("ascii", errors="replace")
        base_fid_hex = f"0x{form_id & 0x00FFFFFF:08X}"

        while spos + 6 <= sn:
            sub_type = rec_data[spos : spos + 4]
            sub_size = struct.unpack_from("<H", rec_data, spos + 4)[0]
            spos += 6

            if sub_type == b"XXXX":
                if spos + 4 <= sn:
                    ext_size = struct.unpack_from("<I", rec_data, spos)[0]
                spos += sub_size
                continue

            if ext_size:
                sub_size = ext_size
                ext_size = 0

            if spos + sub_size > sn:
                break

            if sub_type == b"EDID" and sub_size > 0:
                editor_id = rec_data[spos : spos + sub_size].rstrip(b"\x00").decode("latin1", errors="replace")
            elif rec_type == b"PERK" and sub_type == b"EPFT" and sub_size == 1:
                last_epft = rec_data[spos]
            elif sub_type in allowed_subs and sub_size > 0:
                # For GMST, only string settings (editor_id starting with 's') are translatable strings
                if rec_type == b"GMST" and (not editor_id or not editor_id.startswith("s")):
                    spos += sub_size
                    continue
                # For PERK EPFD, only EPFT in (2, 4, 7) (e.g. 2=Add Activate Choice button text) is a translatable string
                if rec_type == b"PERK" and sub_type == b"EPFD" and last_epft not in (2, 4, 7):
                    spos += sub_size
                    continue

                sub_bytes = rec_data[spos : spos + sub_size]
                sub_type_str = sub_type.decode("ascii", errors="replace")

                if is_localized and rec_type != b"GMST":
                    if sub_size == 4:
                        sid = struct.unpack("<I", sub_bytes)[0]
                        if sid > 0:
                            extracted_records.append({
                                "form_id": form_id,
                                "base_form_id": base_fid_hex,
                                "record_type": rec_type_str,
                                "subrecord_type": sub_type_str,
                                "editor_id": editor_id,
                                "string_id": sid,
                                "text": None,
                            })
                else:
                    raw = sub_bytes.rstrip(b"\x00")
                    if raw:
                        txt = decode_skyrim_bytes(raw)
                        if txt:
                            extracted_records.append({
                                "form_id": form_id,
                                "base_form_id": base_fid_hex,
                                "record_type": rec_type_str,
                                "subrecord_type": sub_type_str,
                                "editor_id": editor_id,
                                "string_id": None,
                                "text": txt,
                            })

            spos += sub_size

        pos = rec_end

    return {
        "plugin_name": path.name,
        "is_localized": is_localized,
        "is_esl_flagged": is_esl,
        "masters": masters,
        "records": extracted_records,
    }
