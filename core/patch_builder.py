"""
Unified MO2 / Vortex patch builder (core/patch_builder.py).

Combines, into one installable `[UA] Skyrim Translation Patch` subfolder AND a ready-to-install
`[UA] Skyrim Translation Patch.zip` archive:
  1. (optional) the donor / reference Ukrainian localization as the base layer
     (Strings/, Interface/ incl. fonts + translate_english.txt, translated plugins), and
  2. every mod translated into `translated_mods/<ModName>/` layered on top (plugins with modified
     records > 0, Strings/, Interface/Translations/).

Every write is protected by the Path Traversal guard `target.resolve().is_relative_to(output_root)`.
"""

import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from core.config import (
    PROJECT_ROOT,
    InvalidTargetError,
    PathTraversalError,
    ensure_safe_output_path,
    get_reference_dir,
    get_translated_mods_dir,
    load_config_model,
    resolve_portable_path,
    sanitize_output_patch_dir,
)
from core.esp_parser import parse_plugin_file

PATCH_FOLDER_NAME = "[UA] Skyrim Translation Patch"
PLUGIN_EXTS = (".esp", ".esm", ".esl")
STRING_EXTS = (".strings", ".dlstrings", ".ilstrings")
_CYRILLIC_RE = re.compile(r"[А-Яа-яІіЇїЄєҐґ]")

ProgressCb = Optional[Callable[[str, float], None]]


@dataclass
class PatchBuildRequest:
    """RORO input for building the unified patch."""

    output_patch_dir: Optional[Path] = None
    translated_dir: Optional[Path] = None
    donor_dir: Optional[Path] = None
    include_donor: bool = True
    clean_output: bool = True
    create_zip: bool = True


@dataclass
class PatchBuildResult:
    """RORO output of the unified patch builder."""

    patch_dir: str
    zip_path: str = ""
    donor_files: int = 0
    mod_files: int = 0
    mods_merged: List[str] = field(default_factory=list)
    skipped_zero_delta_plugins: List[str] = field(default_factory=list)
    overridden_donor_files: int = 0
    total_files: int = 0
    by_kind: Dict[str, int] = field(default_factory=dict)


def _kind(rel: Path) -> str:
    low = rel.as_posix().lower()
    if low.endswith(PLUGIN_EXTS):
        return "plugins"
    if low.endswith(STRING_EXTS):
        return "strings"
    if "interface/translations/" in low:
        return "mcm"
    if low.startswith("interface/"):
        return "interface"
    return "other"


def _find_child_ci(parent: Path, name: str) -> Optional[Path]:
    for c in parent.iterdir():
        if c.is_dir() and c.name.lower() == name.lower():
            return c
    return None


def _copy_file(src: Path, dst: Path, output_root: Path) -> None:
    ensure_safe_output_path(dst, output_root)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _plugin_has_translated_records(plugin_path: Path) -> bool:
    """
    Zero-Delta Plugin Guard:
    Verify that a plugin file inside translated_mods/ actually contains > 0 whitelisted records
    AND at least one translated (Cyrillic) subrecord string.
    Plugins like `SGEyebrows - Improved.esp` (which only contain blacklisted HDPT records and 0 safe records)
    are excluded from `translated_mods/` and the final patch.
    """
    try:
        parsed = parse_plugin_file(plugin_path)
        records = parsed.get("records", [])
        if not records:
            return False
        return any(bool(r.get("text") and _CYRILLIC_RE.search(r["text"])) for r in records)
    except Exception:
        return False


def _donor_content_root(donor: Path) -> Path:
    """Descend into nested folders (e.g. `Skyrim_UA/Data`) until Strings/ or Interface/ is found."""
    cur = donor
    for _ in range(4):
        if _find_child_ci(cur, "Strings") or _find_child_ci(cur, "Interface"):
            return cur
        subs = [c for c in cur.iterdir() if c.is_dir()]
        data = next((c for c in subs if c.name.lower() == "data"), None)
        if data:
            cur = data
        elif len(subs) == 1:
            cur = subs[0]
        else:
            break
    return donor


def _layer_donor(donor: Path, out: Path, written: Dict[str, Path]) -> int:
    root = _donor_content_root(donor)
    count = 0
    for sub in ("Strings", "Interface"):
        d = _find_child_ci(root, sub)
        if not d:
            continue
        for f in d.rglob("*"):
            if f.is_file():
                rel = Path(sub) / f.relative_to(d)
                _copy_file(f, out / rel, out)
                written[rel.as_posix().lower()] = out / rel
                count += 1
    for f in root.iterdir():
        if f.is_file() and f.suffix.lower() in PLUGIN_EXTS:
            _copy_file(f, out / f.name, out)
            written[f.name.lower()] = out / f.name
            count += 1
    return count


def _layer_translated_mod(
    mod_dir: Path,
    out: Path,
    written: Dict[str, Path],
    donor_keys: set,
    skipped_plugins: List[str],
) -> Dict[str, int]:
    stats = {"files": 0, "overridden": 0}

    def put(src: Path, rel: Path) -> None:
        key = rel.as_posix().lower()
        if key in donor_keys:
            stats["overridden"] += 1
        _copy_file(src, out / rel, out)
        written[key] = out / rel
        stats["files"] += 1

    for f in mod_dir.iterdir():
        if f.is_file() and f.suffix.lower() in PLUGIN_EXTS:
            if not _plugin_has_translated_records(f):
                skipped_plugins.append(f.name)
                try:
                    f.unlink()
                except OSError:
                    pass
                continue
            put(f, Path(f.name))
    for sub, exts in (("Strings", STRING_EXTS), ("Interface", None)):
        d = _find_child_ci(mod_dir, sub)
        if not d:
            continue
        for f in d.rglob("*"):
            if not f.is_file():
                continue
            if exts and f.suffix.lower() not in exts:
                continue
            if sub == "Interface" and f.suffix.lower() not in (".txt", ".swf", ".gfx"):
                continue
            put(f, Path(sub) / f.relative_to(d))
    return stats


def _resolve_named_patch_dir(base_out: Path) -> Path:
    """
    Ensure the unified patch is always built inside a clearly named `[UA] Skyrim Translation Patch`
    subfolder when the user selects a generic folder (e.g. `Нова папка`).
    Also cleans up any loose patch files previously dumped directly into `base_out`.
    """
    if base_out.name.lower() == PATCH_FOLDER_NAME.lower() or base_out.name.lower().startswith("[ua]"):
        return base_out

    # Clean up legacy loose patch artifacts if base_out previously had Strings/, Interface/, meta.ini dumped into it
    if base_out.is_dir() and (base_out / "meta.ini").is_file() and (
        (base_out / "Strings").is_dir() or (base_out / "Interface").is_dir()
    ):
        for sub in ("Strings", "Interface"):
            sub_p = _find_child_ci(base_out, sub)
            if sub_p and sub_p.is_dir():
                shutil.rmtree(sub_p, ignore_errors=True)
        for f in list(base_out.iterdir()):
            if f.is_file() and (f.name.lower() == "meta.ini" or f.suffix.lower() in PLUGIN_EXTS):
                try:
                    f.unlink()
                except OSError:
                    pass

    return (base_out / PATCH_FOLDER_NAME).resolve()


def _build_patch_zip(patch_dir: Path) -> Path:
    """
    Create `<parent>/[UA] Skyrim Translation Patch.zip` alongside `patch_dir` for 1-click
    Mod Organizer 2 / Vortex archive installation.
    """
    parent_dir = patch_dir.parent.resolve()
    zip_path = (parent_dir / f"{patch_dir.name}.zip").resolve()
    ensure_safe_output_path(zip_path, parent_dir)
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for file_path in sorted(patch_dir.rglob("*")):
            if file_path.is_file():
                rel_arc = file_path.relative_to(patch_dir).as_posix()
                zf.write(file_path, arcname=rel_arc)
    return zip_path


def build_unified_mo2_patch(
    request: Optional[PatchBuildRequest] = None,
    include_donor: Optional[bool] = None,
    progress_cb: ProgressCb = None,
) -> PatchBuildResult:
    """
    Build the unified `[UA] Skyrim Translation Patch` subfolder AND `[UA] Skyrim Translation Patch.zip`
    archive alongside it (donor base layer + translated mods on top).
    """
    req = request or PatchBuildRequest()
    if include_donor is not None:
        req.include_donor = include_donor

    def report(msg: str, pct: float) -> None:
        if progress_cb:
            progress_cb(msg, pct)

    cfg = load_config_model()
    out_raw = sanitize_output_patch_dir(req.output_patch_dir or cfg.output_patch_dir)
    base_out = resolve_portable_path(out_raw, f"./output/{PATCH_FOLDER_NAME}").resolve()
    if base_out == PROJECT_ROOT.resolve():
        raise InvalidTargetError("Patch folder must not be the application root directory.")

    out = _resolve_named_patch_dir(base_out)
    ensure_safe_output_path(out, base_out)

    translated = Path(req.translated_dir) if req.translated_dir else get_translated_mods_dir()
    donor = Path(req.donor_dir) if req.donor_dir else get_reference_dir()

    if req.clean_output and out.exists():
        if out.name.lower().startswith("[ua]") or "translation patch" in out.name.lower():
            shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    written: Dict[str, Path] = {}
    result = PatchBuildResult(patch_dir=str(out))

    donor_keys: set = set()
    if req.include_donor:
        if not donor.is_dir():
            raise InvalidTargetError(f"Donor (reference) folder does not exist: {donor}")
        report(f"Layer 1/3: donor translation from {donor} ...", 10.0)
        result.donor_files = _layer_donor(donor, out, written)
        donor_keys = set(written)
        report(f"Donor layer: {result.donor_files:,} files.", 45.0)

    report("Layer 2/3: translated mods ...", 50.0)
    mod_dirs = (
        sorted([d for d in translated.iterdir() if d.is_dir()], key=lambda p: p.name.lower())
        if translated.is_dir()
        else []
    )
    for i, md in enumerate(mod_dirs):
        st = _layer_translated_mod(md, out, written, donor_keys, result.skipped_zero_delta_plugins)
        if st["files"]:
            result.mods_merged.append(md.name)
            result.mod_files += st["files"]
            result.overridden_donor_files += st["overridden"]
            report(f"  merged {md.name}: {st['files']} files", 50.0 + 35.0 * (i + 1) / max(1, len(mod_dirs)))
        else:
            # Clean up zero-delta mod directory from translated_mods/ and dictionary registry
            shutil.rmtree(md, ignore_errors=True)
            reg_file = PROJECT_ROOT / "data" / "dictionary_rows" / f"{md.name}.json"
            if reg_file.exists():
                try:
                    reg_file.unlink()
                except OSError:
                    pass
            report(f"  skipped {md.name} (0 translated files)", 50.0 + 35.0 * (i + 1) / max(1, len(mod_dirs)))

    from core.dictionary_exporter import generate_master_index
    from core.mod_translator import _ensure_mo2_patch_meta_ini

    _ensure_mo2_patch_meta_ini(out)
    generate_master_index()

    for p in out.rglob("*"):
        if p.is_file():
            result.by_kind[_kind(p.relative_to(out))] = result.by_kind.get(_kind(p.relative_to(out)), 0) + 1
    result.total_files = sum(result.by_kind.values())

    if req.create_zip:
        report("Layer 3/3: packaging [UA] Skyrim Translation Patch.zip ...", 90.0)
        zip_file = _build_patch_zip(out)
        result.zip_path = str(zip_file)

    report(f"Unified patch ready: {result.total_files:,} files -> {out} (+ .zip)", 100.0)
    return result
