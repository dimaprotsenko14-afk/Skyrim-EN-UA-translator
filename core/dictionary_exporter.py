"""
Single-file HTML dictionary exporter (core/dictionary_exporter.py).

For every translated mod folder, produces STRICTLY ONE report file:
    translated_mods/<ModFolderName>/<ModFolderName>_dictionary.html
(interactive dark theme: live search, Record-Type / MCM filter, source filter, copy buttons)
plus one master page:
    translated_mods/INDEX_DICTIONARY.html
(links to every mod dictionary + overall translation progress).

Rows from several plugins / MCM files of the same mod folder are merged through a small registry
(data/dictionary_rows/<mod>.json), so a second export never overwrites the first. No .csv / .md
per-mod reports are generated any more.
"""

import html
import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
TRANSLATED_MODS_DIR = PROJECT_ROOT / "translated_mods"
REGISTRY_DIR = DATA_DIR / "dictionary_rows"
CATALOG_JSON = DATA_DIR / "mods_catalog.json"
INDEX_PATH = TRANSLATED_MODS_DIR / "INDEX_DICTIONARY.html"

_CSS = """
:root{--bg:#12141a;--panel:#1b1e27;--border:#2c3140;--text:#e8ecf4;--muted:#9aa3b5;--gold:#e2b86b;--uk:#a8e6cf;
--tm-bg:#1e4631;--tm-fg:#7ce3a3;--nn-bg:#1f365c;--nn-fg:#8bb8ff;--ok:#7ce3a3;--warn:#e2b86b}
*{box-sizing:border-box}body{margin:0;padding:24px 32px;background:var(--bg);color:var(--text);
font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;line-height:1.45}
.header{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:20px 24px;margin-bottom:18px}
h1{margin:0 0 8px;font-size:22px;color:var(--gold)}a{color:#8bb8ff;text-decoration:none}a:hover{text-decoration:underline}
.stats{display:flex;gap:20px;flex-wrap:wrap;color:var(--muted);font-size:13.5px;margin-bottom:14px}.stats strong{color:var(--text)}
.controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
input[type=text],select{padding:10px 14px;border-radius:6px;border:1px solid #3c4458;background:#101218;color:var(--text);font-size:14px;outline:none}
#q{flex:1;min-width:260px}input:focus,select:focus{border-color:var(--gold)}
.btn{padding:9px 14px;border-radius:6px;border:1px solid var(--border);background:#232734;color:var(--text);cursor:pointer;font-weight:600;font-size:13px}
.btn.active{background:var(--gold);color:#12141a;border-color:var(--gold)}
.wrap{background:var(--panel);border:1px solid var(--border);border-radius:10px;overflow:hidden}
table{width:100%;border-collapse:collapse}th{background:#222632;color:var(--gold);text-align:left;padding:12px 14px;font-size:13px;
border-bottom:1px solid var(--border);position:sticky;top:0}td{padding:9px 14px;border-bottom:1px solid #232733;vertical-align:top}
tr:hover td{background:#212532}code{font-family:'Cascadia Mono',Consolas,monospace;font-size:12.5px;color:#cbd5e8}
.c-idx{width:46px;color:var(--muted)}.c-rec{width:110px}.c-fid{width:115px}.c-edid{width:190px;word-break:break-all}.c-src{width:130px}
.c-en{width:30%;color:#d0d7e6}.c-uk{width:32%;color:var(--uk);font-weight:500}
.badge{display:inline-block;padding:3px 8px;border-radius:4px;font-size:11.5px;font-weight:600}
.b-tm{background:var(--tm-bg);color:var(--tm-fg)}.b-nn{background:var(--nn-bg);color:var(--nn-fg)}
.cp{margin-left:8px;padding:1px 7px;border-radius:4px;border:1px solid #3c4458;background:#232734;color:var(--muted);cursor:pointer;font-size:11px}
.cp:hover{color:var(--gold);border-color:var(--gold)}.cp.done{color:var(--ok);border-color:var(--ok)}
.bar{height:16px;background:#101218;border:1px solid var(--border);border-radius:8px;overflow:hidden;margin:8px 0}
.st-COMPLETED{color:var(--ok)}.st-IN_PROGRESS{color:var(--warn)}.st-PENDING{color:var(--muted)}.st-FAILED{color:#ff6b6b}
.wave-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px;margin:14px 0}
.wave-card{background:#161922;border:1px solid var(--border);border-radius:8px;padding:12px 16px}
.wave-card h4{margin:0 0 6px;color:var(--gold);font-size:13.5px}
.wave-card .w-cnt{font-size:18px;font-weight:700;color:var(--text);margin-bottom:4px}
.wave-card .w-sub{font-size:12px;color:var(--muted)}
.w-badge{display:inline-block;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:600;background:#2b3245;color:#bcd}
"""

_JS = """
let src='ALL';
function setSrc(m,b){src=m;document.querySelectorAll('.btn.src').forEach(x=>x.classList.remove('active'));b.classList.add('active');f();}
function f(){const q=document.getElementById('q').value.toLowerCase().trim();const rt=document.getElementById('rt').value;
let n=0;document.querySelectorAll('#tb tr').forEach(tr=>{const s=tr.dataset.source||'';const r=tr.dataset.rec||'';
const ms=src==='ALL'||(src==='TM'&&s.startsWith('TM'))||(src==='NN'&&!s.startsWith('TM'));
const mr=rt==='ALL'||(rt==='MCM'?r.startsWith('MCM'):r.split(':')[0]===rt);
const mt=!q||tr.innerText.toLowerCase().includes(q);const show=ms&&mr&&mt;tr.style.display=show?'':'none';if(show)n++;});
document.getElementById('vis').textContent=n;}
function cp(b){const t=b.previousElementSibling.innerText;const ok=()=>{b.classList.add('done');b.textContent='✓';setTimeout(()=>{b.classList.remove('done');b.textContent='copy'},1200)};
if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(ok)}else{const a=document.createElement('textarea');a.value=t;document.body.appendChild(a);a.select();document.execCommand('copy');a.remove();ok();}}
"""


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()


def _esc_cell(text: str) -> str:
    return html.escape(text).replace("\r\n", "<br>").replace("\n", "<br>")


def _registry_path(mod_folder_name: str) -> Path:
    return REGISTRY_DIR / f"{_safe(mod_folder_name)}.json"


def export_mod_dictionary(
    mod_folder_name: str,
    rows: List[Dict[str, str]],
    section: str,
    mod_title: Optional[str] = None,
    out_dir: Optional[Path] = None,
    refresh_master: bool = False,
) -> Dict[str, str]:
    """Merge `rows` (one plugin / MCM file = one `section`) into the mod registry and write the single HTML dictionary."""
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    reg_path = _registry_path(mod_folder_name)
    reg: Dict[str, Any] = {"mod_name": mod_folder_name, "sections": {}}
    if reg_path.exists():
        try:
            reg = json.loads(reg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    reg["mod_name"] = mod_folder_name
    reg["sections"][section] = rows
    reg["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    reg_path.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")

    target_dir = Path(out_dir) / mod_folder_name if out_dir else TRANSLATED_MODS_DIR / mod_folder_name
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / f"{_safe(mod_folder_name)}_dictionary.html"
    out_path.write_text(_render_mod_html(reg, mod_title or mod_folder_name), encoding="utf-8")
    if refresh_master:
        generate_master_index()
    return {"html_path": str(out_path), "rows": str(sum(len(v) for v in reg["sections"].values()))}


def _render_mod_html(reg: Dict[str, Any], title: str) -> str:
    all_rows: List[Dict[str, str]] = []
    for sec, rws in reg["sections"].items():
        for r in rws:
            all_rows.append({**r, "_section": sec})
    total = len(all_rows)
    tm = sum(1 for r in all_rows if r["source"].startswith("TM"))
    rec_types = sorted({r["record_subrecord"].split(":")[0] for r in all_rows})
    opts = "".join(f"<option value='{html.escape(t)}'>{html.escape(t)}</option>" for t in rec_types if t != "MCM")
    has_mcm = "MCM" in rec_types

    body: List[str] = []
    for i, r in enumerate(all_rows, 1):
        badge = "b-tm" if r["source"].startswith("TM") else "b-nn"
        body.append(
            f"<tr data-source='{html.escape(r['source'])}' data-rec='{html.escape(r['record_subrecord'])}'>"
            f"<td class='c-idx'>{i}</td><td class='c-rec'><code>{html.escape(r['record_subrecord'])}</code></td>"
            f"<td><code>{html.escape(r['form_id'])}</code></td><td class='c-edid'><code>{html.escape(r['editor_id'])}</code></td>"
            f"<td class='c-src'><span class='badge {badge}'>{html.escape(r['source'])}</span></td>"
            f"<td class='c-en'><span>{_esc_cell(r['en_text'])}</span><button class='cp' onclick='cp(this)'>copy</button></td>"
            f"<td class='c-uk'><span>{_esc_cell(r['uk_text'])}</span><button class='cp' onclick='cp(this)'>copy</button></td></tr>"
        )
    sections_txt = ", ".join(html.escape(s) for s in reg["sections"])
    return f"""<!DOCTYPE html><html lang="uk"><head><meta charset="UTF-8">
<title>{html.escape(title)} — словник перекладу</title><style>{_CSS}</style></head><body>
<div class="header"><h1>{html.escape(title)} — словник перекладу</h1>
<div class="stats"><span>Записів: <strong id="vis">{total}</strong> / {total}</span><span>TM: <strong>{tm}</strong></span>
<span>Glossary+Neural / LLM: <strong>{total - tm}</strong></span><span>Файли: {sections_txt}</span>
<span><a href="../INDEX_DICTIONARY.html">← індекс усіх модів</a></span></div>
<div class="controls"><input type="text" id="q" placeholder="Пошук: українською, англійською, FormID, EditorID…" oninput="f()">
<select id="rt" onchange="f()"><option value="ALL">Усі типи записів</option>{'<option value="MCM">MCM / Interface</option>' if has_mcm else ''}{opts}</select>
<button class="btn src active" onclick="setSrc('ALL',this)">Усі</button>
<button class="btn src" onclick="setSrc('TM',this)">TM</button>
<button class="btn src" onclick="setSrc('NN',this)">Neural</button></div></div>
<div class="wrap"><table><thead><tr><th>#</th><th>Record:Subrecord</th><th>FormID</th><th>EditorID</th><th>Джерело</th>
<th>Оригінал (EN)</th><th>Переклад (UK)</th></tr></thead><tbody id="tb">{''.join(body)}</tbody></table></div>
<script>{_JS}</script></body></html>"""


def generate_master_index() -> Path:
    """INDEX_DICTIONARY.html: links to every mod dictionary + wave completion stats + overall progress."""
    from core.checkpoint_manager import CheckpointManager

    TRANSLATED_MODS_DIR.mkdir(parents=True, exist_ok=True)
    cp = CheckpointManager()

    cat_map: Dict[str, Dict[str, Any]] = {}
    wave_targets = {
        "Wave 1": {"name": "Wave 1: TM Instant", "total_mods": 154, "total_strings": 2544, "done_mods": 0, "done_strings": 0},
        "Wave 2": {"name": "Wave 2: Small & MCM", "total_mods": 435, "total_strings": 11693, "done_mods": 0, "done_strings": 0},
        "Wave 3": {"name": "Wave 3: Medium Overhauls", "total_mods": 233, "total_strings": 77317, "done_mods": 0, "done_strings": 0},
        "Wave 4": {"name": "Wave 4: Large Overhauls", "total_mods": 94, "total_strings": 532993, "done_mods": 0, "done_strings": 0},
    }

    def _cat_to_wave(cat_name: str) -> str:
        if cat_name == "ALREADY_LOCALIZED_OR_TM":
            return "Wave 1"
        if cat_name == "TRANSLATABLE_SMALL":
            return "Wave 2"
        if cat_name == "TRANSLATABLE_MEDIUM":
            return "Wave 3"
        if cat_name == "TRANSLATABLE_LARGE":
            return "Wave 4"
        return "Wave 2"

    if CATALOG_JSON.exists():
        try:
            cat = json.loads(CATALOG_JSON.read_text(encoding="utf-8"))
            for m in cat.get("mods", []):
                mname = m.get("mod_name")
                if mname:
                    c = m.get("category", "")
                    w = _cat_to_wave(c)
                    cat_map[mname] = {"category": c, "wave": w, "total_strings": m.get("total_strings", 0)}
        except (OSError, ValueError):
            pass

    mods: List[Dict[str, Any]] = []
    for reg_file in sorted(REGISTRY_DIR.glob("*.json")) if REGISTRY_DIR.exists() else []:
        try:
            reg = json.loads(reg_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows = [r for rws in reg["sections"].values() for r in rws]
        name = reg["mod_name"]
        changed = sum(1 for r in rows if r["en_text"].strip() != r["uk_text"].strip())
        link = f"{urllib.parse.quote(name)}/{urllib.parse.quote(_safe(name))}_dictionary.html"
        entry = cp.get(name) or {}
        st = entry.get("status", "COMPLETED")
        c_info = cat_map.get(name, {})
        wave_key = entry.get("wave") or c_info.get("wave") or "Wave 2"
        if wave_key in wave_targets and st == "COMPLETED":
            wave_targets[wave_key]["done_mods"] += 1
            wave_targets[wave_key]["done_strings"] += len(rows)

        mods.append({
            "name": name,
            "wave": wave_key,
            "rows": len(rows),
            "changed": changed,
            "tm": sum(1 for r in rows if r["source"].startswith("TM")),
            "sections": list(reg["sections"]),
            "link": link,
            "status": st,
            "synced": entry.get("synced_to_mo2", False),
            "updated": reg.get("updated", ""),
        })

    cat_total = sum(w["total_strings"] for w in wave_targets.values())
    cat_mods = sum(w["total_mods"] for w in wave_targets.values())
    done_rows = sum(m["rows"] for m in mods)
    done_mods_cnt = sum(1 for m in mods if m["status"] == "COMPLETED")

    pct = 100.0 * done_rows / max(cat_total, 1)
    mod_pct = 100.0 * done_mods_cnt / max(cat_mods, 1)

    cards_html = []
    for wk, wdata in wave_targets.items():
        w_pct = 100.0 * wdata["done_mods"] / max(wdata["total_mods"], 1)
        cards_html.append(
            f"<div class='wave-card'><h4>{html.escape(wdata['name'])}</h4>"
            f"<div class='w-cnt'>{wdata['done_mods']} / {wdata['total_mods']} модів ({w_pct:.1f}%)</div>"
            f"<div class='bar'><div style='width:{min(w_pct, 100):.1f}%'></div></div>"
            f"<div class='w-sub'>{wdata['done_strings']:,} / {wdata['total_strings']:,} рядків</div></div>"
        )
    cards_str = "".join(cards_html)

    trs = "".join(
        f"<tr data-wave='{html.escape(m['wave'])}'><td><span class='w-badge'>{html.escape(m['wave'])}</span> "
        f"<a href='{m['link']}'>{html.escape(m['name'])}</a></td><td>{html.escape(', '.join(m['sections']))}</td>"
        f"<td>{m['rows']:,}</td><td>{m['changed']:,}</td><td>{m['tm']:,}</td>"
        f"<td class='st-{m['status']}'>{m['status']}{' ✓MO2' if m['synced'] else ''}</td>"
        f"<td>{html.escape(m['updated'][:19])}</td></tr>"
        for m in sorted(mods, key=lambda x: (x["wave"], x["name"].lower()))
    )

    doc = f"""<!DOCTYPE html><html lang="uk"><head><meta charset="UTF-8"><title>Індекс словників перекладу (Skyrim SE/AE)</title>
<style>{_CSS}</style></head><body><div class="header"><h1>Індекс словників перекладу (Skyrim SE/AE)</h1>
<div class="stats"><span>Перекладено модів: <strong>{done_mods_cnt}</strong> / {cat_mods}</span>
<span>Рядків у словниках: <strong>{done_rows:,}</strong> / {cat_total:,}</span></div>
<div>Рядки: {pct:.1f}%<div class="bar"><div style="width:{min(pct, 100):.1f}%"></div></div></div>
<div>Моди: {mod_pct:.1f}%<div class="bar"><div style="width:{min(mod_pct, 100):.1f}%"></div></div></div>
<div class="wave-grid">{cards_str}</div>
<div class="controls"><input type="text" id="q" placeholder="Фільтр за назвою моду…" oninput="g()">
<select id="wv" onchange="g()"><option value="ALL">Усі хвилі (Wave 1-4)</option>
<option value="Wave 1">Wave 1: TM Instant (154)</option>
<option value="Wave 2">Wave 2: Small & MCM (435)</option>
<option value="Wave 3">Wave 3: Medium (233)</option>
<option value="Wave 4">Wave 4: Large (94)</option></select>
</div></div>
<div class="wrap"><table><thead><tr><th>Мод & Хвиля</th><th>Файли</th><th>Записів</th><th>Змінено</th><th>З TM</th><th>Статус</th><th>Оновлено</th></tr></thead>
<tbody id="tb">{trs}</tbody></table></div>
<script>
function g(){{
  const q = document.getElementById('q').value.toLowerCase().trim();
  const wv = document.getElementById('wv').value;
  document.querySelectorAll('#tb tr').forEach(tr=>{{
    const w = tr.dataset.wave || '';
    const mw = (wv === 'ALL' || w === wv);
    const mq = (!q || tr.innerText.toLowerCase().includes(q));
    tr.style.display = (mw && mq) ? '' : 'none';
  }});
}}
</script>
</body></html>"""
    INDEX_PATH.write_text(doc, encoding="utf-8")
    return INDEX_PATH


def cleanup_legacy_reports(root: Path = TRANSLATED_MODS_DIR) -> Dict[str, int]:
    """Delete every legacy .csv / .md report and every duplicate-named *_translations.html inside translated_mods/."""
    removed = {"csv": 0, "md": 0, "html": 0}
    if not root.exists():
        return removed
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        low = f.name.lower()
        if low.endswith(".csv"):
            f.unlink(); removed["csv"] += 1
        elif low.endswith(".md"):
            f.unlink(); removed["md"] += 1
        elif low.endswith("_translations.html"):
            f.unlink(); removed["html"] += 1
    return removed
