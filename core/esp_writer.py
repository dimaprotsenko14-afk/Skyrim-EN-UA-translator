"""
Phase 2 - Step 1.2: Binary ESP / ESM / ESL Plugin Writer & Rebuilder (core/esp_writer.py)
Implements rebuild_plugin_file(input_path, output_path, translation_fn) which:
- Recursively traverses TES4, nested GRUPs (24-byte headers), and Records (24-byte headers).
- Decompresses 0x00040000 zlib records when scanning subrecords.
- Updates whitelisted text subrecords (FULL, DESC, CNAM, NNAM, NAM1, RNAM, etc.),
  recalculates uint16 lengths (and XXXX 32-bit extended lengths for >65535 byte books),
  recompresses modified zlib records, and updates Record and nested GRUP byte size headers.
- Guarantees 100% SHA-256 byte-for-byte identity when rebuilt with an identity translation_fn.
"""

import hashlib
import struct
import zlib
from pathlib import Path
from typing import Callable, Dict, List, Set, Tuple, Any, Optional

from core.esp_parser import (
    RECORD_SUBRECORD_WHITELIST,
    STRICT_RECORD_BLACKLIST,
    is_cosmetic_or_morph_plugin,
)
from core.strings_parser import decode_skyrim_bytes

STRICT_TOP_LEVEL_GRUP_PASSTHROUGH: Set[bytes] = {
    b"CELL", b"WRLD", b"HDPT", b"RACE", b"LCTN", b"REGN", b"NAVM",
    b"LAND", b"REFR", b"ACHR", b"PGRE", b"PHZD", b"WATR", b"WTHR",
    b"IDLE", b"PACK", b"ANIO", b"ARMA",
}

# Signature for translation callback:
# translation_fn(en_text: str, record_type: str, subrecord_type: str, form_id: int, editor_id: str) -> str
TranslationFn = Callable[[str, str, str, int, str], str]


def _rebuild_subrecords(
    rec_data: bytes,
    rec_type: bytes,
    form_id: int,
    is_localized: bool,
    allowed_subs: Set[bytes],
    translation_fn: TranslationFn,
) -> Tuple[bytes, bool]:
    """
    Parse and rebuild subrecords of a single record.
    Returns (new_rec_data, was_modified).
    """
    spos = 0
    sn = len(rec_data)
    ext_size = 0
    pending_xxxx_raw: Optional[bytes] = None
    editor_id = ""
    last_epft = None
    rec_type_str = rec_type.decode("ascii", errors="replace")

    out_chunks: List[bytes] = []
    was_modified = False

    while spos + 6 <= sn:
        sub_start = spos
        sub_type = rec_data[spos : spos + 4]
        sub_size = struct.unpack_from("<H", rec_data, spos + 4)[0]
        spos += 6

        if sub_type == b"XXXX":
            if spos + 4 <= sn:
                ext_size = struct.unpack_from("<I", rec_data, spos)[0]
            pending_xxxx_raw = rec_data[sub_start : spos + sub_size]
            spos += sub_size
            continue

        actual_size = ext_size if ext_size else sub_size
        used_xxxx = pending_xxxx_raw is not None
        xxxx_raw = pending_xxxx_raw
        ext_size = 0
        pending_xxxx_raw = None

        if spos + actual_size > sn:
            # Malformed tail; preserve raw bytes
            if xxxx_raw:
                out_chunks.append(xxxx_raw)
            out_chunks.append(rec_data[sub_start:])
            break

        sub_payload = rec_data[spos : spos + actual_size]
        orig_full_sub = (xxxx_raw or b"") + rec_data[sub_start : spos + actual_size]
        spos += actual_size

        if sub_type == b"EDID" and actual_size > 0:
            editor_id = sub_payload.rstrip(b"\x00").decode("latin1", errors="replace")
            out_chunks.append(orig_full_sub)
            continue

        if rec_type == b"PERK" and sub_type == b"EPFT" and actual_size == 1:
            last_epft = sub_payload[0]
            out_chunks.append(orig_full_sub)
            continue

        # Determine if this subrecord is a translatable inline string
        is_translatable = (
            sub_type in allowed_subs
            and actual_size > 0
            and (not is_localized or rec_type == b"GMST")
        )
        if is_translatable and rec_type == b"GMST" and (not editor_id or not editor_id.startswith("s")):
            is_translatable = False
        if is_translatable and rec_type == b"PERK" and sub_type == b"EPFD" and last_epft not in (2, 4, 7):
            is_translatable = False
        if is_translatable and rec_type == b"QUST" and any(k in editor_id.lower() for k in ("menu", "slider", "morph", "skse", "internal", "handler")):
            is_translatable = False

        if not is_translatable:
            out_chunks.append(orig_full_sub)
            continue

        # Decode inline string (preserve trailing null handling)
        has_trailing_null = sub_payload.endswith(b"\x00")
        raw_str_bytes = sub_payload[:-1] if has_trailing_null else sub_payload
        orig_text = decode_skyrim_bytes(raw_str_bytes)
        sub_type_str = sub_type.decode("ascii", errors="replace")

        new_text = translation_fn(orig_text, rec_type_str, sub_type_str, form_id, editor_id)
        if new_text is None or new_text == orig_text:
            out_chunks.append(orig_full_sub)
            continue

        # Encode new text as UTF-8
        was_modified = True
        new_payload = new_text.encode("utf-8") + (b"\x00" if has_trailing_null else b"")
        new_len = len(new_payload)

        if new_len > 0xFFFF or used_xxxx:
            xxxx_header = b"XXXX" + struct.pack("<HI", 4, new_len)
            sub_header = sub_type + struct.pack("<H", 0)
            out_chunks.append(xxxx_header + sub_header + new_payload)
        else:
            sub_header = sub_type + struct.pack("<H", new_len)
            out_chunks.append(sub_header + new_payload)

    if not was_modified:
        return rec_data, False
    return b"".join(out_chunks), True


def _process_block(
    data: memoryview,
    start: int,
    end: int,
    is_localized: bool,
    translation_fn: TranslationFn,
    plugin_name: str = "",
    skip_npc: bool = False,
) -> Tuple[bytes, int]:
    """
    Recursively process a range [start, end) of records and GRUPs.
    Updates Record data_size and nested GRUP group_size headers whenever child records change.
    Returns (rebuilt_bytes, modified_records_count).
    """
    pos = start
    chunks: List[bytes] = []
    modified_count = 0
    passthrough_grups = set(STRICT_TOP_LEVEL_GRUP_PASSTHROUGH)
    if skip_npc or is_cosmetic_or_morph_plugin(plugin_name):
        passthrough_grups.add(b"NPC_")

    while pos + 24 <= end:
        rtype = bytes(data[pos : pos + 4])
        if rtype == b"GRUP":
            grup_size = struct.unpack_from("<I", data, pos + 4)[0]
            grup_end = min(pos + grup_size, end)
            grup_label = bytes(data[pos + 8 : pos + 12])
            grup_type = struct.unpack_from("<i", data, pos + 12)[0]
            grup_header_tail = bytes(data[pos + 8 : pos + 24])

            # Top-level GRUP raw byte slice pass-through for blacklisted categories
            if grup_type == 0 and grup_label in passthrough_grups:
                chunks.append(bytes(data[pos:grup_end]))
                pos = grup_end
                continue

            body_bytes, sub_mod_count = _process_block(
                data, pos + 24, grup_end, is_localized, translation_fn, plugin_name=plugin_name, skip_npc=skip_npc
            )
            if sub_mod_count == 0 and len(body_bytes) == (grup_size - 24):
                chunks.append(bytes(data[pos:grup_end]))
            else:
                new_grup_size = 24 + len(body_bytes)
                new_hdr = b"GRUP" + struct.pack("<I", new_grup_size) + grup_header_tail
                chunks.append(new_hdr + body_bytes)
                modified_count += sub_mod_count

            pos = grup_end
            continue

        # Standard 24-byte Record header
        data_size, flags, form_id, vc1, ver, vc2 = struct.unpack_from("<IIIIHH", data, pos + 4)
        rec_end = min(pos + 24 + data_size, end)

        # Strict blacklist check
        if rtype in STRICT_RECORD_BLACKLIST or (rtype == b"NPC_" and (skip_npc or is_cosmetic_or_morph_plugin(plugin_name))):
            chunks.append(bytes(data[pos:rec_end]))
            pos = rec_end
            continue

        allowed_subs = RECORD_SUBRECORD_WHITELIST.get(rtype)

        if not allowed_subs or data_size == 0 or (is_localized and rtype != b"GMST"):
            chunks.append(bytes(data[pos:rec_end]))
            pos = rec_end
            continue

        is_compressed = bool(flags & 0x00040000)
        raw_rec_payload = bytes(data[pos + 24 : rec_end])

        if is_compressed:
            if len(raw_rec_payload) <= 4:
                chunks.append(bytes(data[pos:rec_end]))
                pos = rec_end
                continue
            try:
                decompressed = zlib.decompress(raw_rec_payload[4:])
            except zlib.error:
                chunks.append(bytes(data[pos:rec_end]))
                pos = rec_end
                continue

            new_sub_bytes, was_mod = _rebuild_subrecords(
                decompressed, rtype, form_id, is_localized, allowed_subs, translation_fn
            )
            if not was_mod:
                chunks.append(bytes(data[pos:rec_end]))
            else:
                recompressed = struct.pack("<I", len(new_sub_bytes)) + zlib.compress(new_sub_bytes)
                new_hdr = struct.pack(
                    "<4sIIIIHH", rtype, len(recompressed), flags, form_id, vc1, ver, vc2
                )
                chunks.append(new_hdr + recompressed)
                modified_count += 1
        else:
            new_sub_bytes, was_mod = _rebuild_subrecords(
                raw_rec_payload, rtype, form_id, is_localized, allowed_subs, translation_fn
            )
            if not was_mod:
                chunks.append(bytes(data[pos:rec_end]))
            else:
                new_hdr = struct.pack(
                    "<4sIIIIHH", rtype, len(new_sub_bytes), flags, form_id, vc1, ver, vc2
                )
                chunks.append(new_hdr + new_sub_bytes)
                modified_count += 1

        pos = rec_end

    if pos < end:
        chunks.append(bytes(data[pos:end]))

    return b"".join(chunks), modified_count


def rebuild_plugin_file(
    input_path: Path | str,
    output_path: Path | str,
    translation_fn: TranslationFn,
    skip_npc: bool = False,
) -> Dict[str, Any]:
    """
    Rebuild an ESP/ESM/ESL plugin file, applying translation_fn to all whitelisted
    text subrecords, recalculating subrecord uint16 / XXXX uint32 lengths, recompressing
    0x00040000 zlib records, and updating Record and nested GRUP byte size headers.
    """
    in_p = Path(input_path)
    out_p = Path(output_path)
    raw_bytes = in_p.read_bytes()
    n = len(raw_bytes)
    if n < 24 or raw_bytes[:4] != b"TES4":
        raise ValueError(f"Invalid TES4 plugin file: {in_p}")

    _rtype, tes4_size, tes4_flags, _fid, _vc1, _ver, _vc2 = struct.unpack_from("<4sIIIIHH", raw_bytes, 0)
    is_localized = bool(tes4_flags & 0x00000080)
    tes4_end = 24 + tes4_size

    mv = memoryview(raw_bytes)
    tes4_block = bytes(mv[:tes4_end])
    body_bytes, modified_records = _process_block(
        mv, tes4_end, n, is_localized, translation_fn, plugin_name=in_p.name, skip_npc=skip_npc
    )

    final_bytes = tes4_block + body_bytes
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_bytes(final_bytes)

    return {
        "input_path": str(in_p),
        "output_path": str(out_p),
        "is_localized": is_localized,
        "original_size": n,
        "rebuilt_size": len(final_bytes),
        "modified_records": modified_records,
        "sha256_orig": hashlib.sha256(raw_bytes).hexdigest(),
        "sha256_rebuilt": hashlib.sha256(final_bytes).hexdigest(),
    }
