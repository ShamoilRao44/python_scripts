#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_dependency_graph.py  -  VB.NET IR JSON  ->  dependency_graph.md  +  dependency_graph.html

Single file, Python 3.8 compatible, standard library only. No LLM, no Graphviz, no internet needed.

WHAT IT ANSWERS (type-level dependencies, the detailed companion of the architecture graph)
  - Who depends on whom, through which kind of dependency (field type, call, ...), with the JSON evidence
  - What breaks if I change type X?   (transitive dependents)   /   What does X need?   (transitive dependencies)
  - Where are the cycles?             (strongly connected components, computed here and compared with the IR's own)
  - What should be migrated first?    (IR SuggestedMigrationOrder if present, else computed from dependency levels)
  - Which types are coupling hot-spots? (fan-in, fan-out, instability)

OUTPUT
  dependency_graph.md    Mermaid diagram by dependency level + short tables (hot-spots, cycles, migration order)
  dependency_graph.html  ONE self-contained interactive page (open in any browser):
                         dependency-level layout, hover/click highlight, impact analysis with depth control,
                         edge-kind filters, search, cycle highlighting, sortable metrics table,
                         migration-order list, edge evidence panel, download SVG / CSV

HOW IT IS BUILT (all deterministic)
  Nodes  = Types[].       Edges = TypeDependencyGraph.Edges that point to another known type
                          + CallGraph.Edges rolled up from methods to their types.
  Levels = longest path to a type with no internal dependencies (level 0 = foundation). Cycles are collapsed first.
  Unresolved targets (unresolved-type:, unresolved-call:) are NOT dependencies between types; they are counted as
  external dependencies per type. Confidence from AnalysisStatus/Metrics is shown, because an analysis with many
  unresolved edges under-reports internal dependencies.

INPUT  : the IR JSON only. Set the two paths below.
"""
import os
import re
import sys
import json
import hashlib
import datetime
from collections import Counter, defaultdict

# =============================== CONFIG ===============================
IR_JSON_PATH = r"<PATH_TO_IR_JSON_FILE>"       # e.g. r"/mnt/MyDocuments/.../project.ir.json"
OUTPUT_DIR = r"<PATH_TO_OUTPUT_DIRECTORY>"     # e.g. r"/mnt/MyDocuments/.../graph_output"
IR_BASE_DIR = ""        # optional: folder that contains files/ and types/ (per-item IR). "" = not used
OUTPUT_MD = "dependency_graph.md"
OUTPUT_HTML = "dependency_graph.html"

HIDE_PRIMITIVE_LIBS = True      # hide System.Void / Boolean / String ... from the external-dependency counts
INCLUDE_CALLGRAPH = True        # also roll CallGraph.Edges up to type level (kind "call")
MD_MAX_TYPES = 40               # Mermaid diagrams get unreadable (and some renderers refuse them) above this
MD_MAX_EDGES = 80
EVIDENCE_PER_EDGE = 5           # sample member/file/line records stored per edge

# ============================ UTILITIES ===============================
def log(msg):
    print("[%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def gi(d, *names, **kw):
    """Case-insensitive dict lookup; returns first non-None match."""
    default = kw.get("default")
    if not isinstance(d, dict):
        return default
    low = dict((k.lower(), k) for k in d)
    for n in names:
        k = low.get(n.lower())
        if k is not None and d[k] is not None:
            return d[k]
    return default


def trunc(s, n):
    s = str(s)
    return s if len(s) <= n else s[:n] + "..."


def oneline(s):
    return re.sub(r"\s+", " ", str(s)).strip()


def compact(v, maxlen=200, depth=0):
    if v is None:
        return "null"
    if isinstance(v, (bool, int, float)):
        return str(v)
    if isinstance(v, str):
        return trunc(oneline(v), maxlen)
    if isinstance(v, list):
        if not v:
            return "[]"
        items = ", ".join(compact(x, max(40, maxlen // 2), depth + 1) for x in v[:4])
        return "[%d: %s]" % (len(v), trunc(items, maxlen))
    if isinstance(v, dict):
        if depth > 1:
            return "{...}"
        items = "; ".join("%s=%s" % (k, compact(x, max(40, maxlen // 2), depth + 1)) for k, x in list(v.items())[:8])
        return "{" + trunc(items, maxlen * 2) + "}"
    return trunc(v, maxlen)


def md_table(headers, rows):
    def esc(x):
        return str(x).replace("|", "\\|").replace("\n", " ")
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(esc(c) for c in r) + " |")
    return "\n".join(out)


def mid(s):
    return "n" + re.sub(r"[^0-9A-Za-z]", "_", str(s))


def mlabel(s):
    return str(s).replace('"', "'")


def type_short(tid):
    s = tid[2:] if tid[:2] in ("T:", "M:", "F:") else tid
    return s.split(".")[-1]


def method_name_from_id(mid_):
    head = mid_[2:] if mid_.startswith("M:") else mid_
    head = head.split("(")[0]
    return head.split(".")[-1]


def method_type(mid_):
    if not isinstance(mid_, str) or not mid_.startswith("M:"):
        return None
    head = mid_[2:].split("(")[0]
    if "." not in head:
        return None
    return "T:" + head.rsplit(".", 1)[0]


def method_label(mid_):
    head = mid_[2:] if mid_.startswith("M:") else mid_
    head = head.split("(")[0]
    return ".".join(head.split(".")[-2:])


def vendor_of(name):
    name = str(name).strip()
    if name.startswith("System.Windows.Forms"):
        return "WinForms"
    if name.startswith("System."):
        return ".".join(name.split(".")[:2])
    if "." in name:
        return re.split(r"[.<(]", name)[0]
    return "(unqualified)"


# ============================ STAGE 0: FACTS ==========================
CODE_KEYS = ("rawcode", "normalizedcode", "body", "code", "sourcetext", "initializercode")


def collect(obj, path, strings, kv_hits):
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = path + "." + k if path else k
            kl = k.lower()
            if ("storedproc" in kl or kl in ("sql", "sqlcalls", "sqlstatements", "commandtext")) and v not in (None, "", [], {}):
                kv_hits.append((p, k, compact(v, 240)))
            collect(v, p, strings, kv_hits)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            collect(v, "%s[%d]" % (path, i), strings, kv_hits)
    elif isinstance(obj, str):
        strings.append((path, obj))


def members_of(t, plural, kind_word):
    v = gi(t, plural)
    if isinstance(v, list) and v:
        return v
    mem = gi(t, "Members")
    if isinstance(mem, list):
        return [m for m in mem if isinstance(m, dict) and str(gi(m, "Kind", default="")).lower() == kind_word.lower()]
    return []


def build_model(ir):
    M = {}
    types = ir.get("Types") or []
    files = ir.get("Files") or []
    file_lines = dict((gi(f, "RelativePath", default=""), gi(f, "Lines", default=0) or 0) for f in files)
    T = []
    for i, t in enumerate(types):
        tid = gi(t, "Id", default="T:?%d" % i)
        ir_extra = {}
        irf = gi(t, "IrFile")
        if IR_BASE_DIR and irf:
            p = os.path.join(IR_BASE_DIR, irf)
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8-sig") as f:
                        ir_extra = json.load(f)
                except Exception as e:
                    log("could not read per-type IR %s: %s" % (p, e))
        src = gi(t, "SourceFiles", default=[]) or []
        fields = members_of(t, "Fields", "Field") or members_of(ir_extra, "Fields", "Field")
        methods = members_of(t, "Methods", "Method") or members_of(ir_extra, "Methods", "Method")
        props = members_of(t, "Properties", "Property") or members_of(ir_extra, "Properties", "Property")
        mnames = [method_name_from_id(gi(m, "Id", default="")) if isinstance(m, dict) and gi(m, "Id") else
                  (gi(m, "Name", default="") if isinstance(m, dict) else str(m)) for m in methods]
        base = gi(t, "BaseClass")
        base_txt = gi(base, "Text", default="") if isinstance(base, dict) else (base or "")
        is_form = any(s.lower().endswith(".designer.vb") for s in src) or "InitializeComponent" in mnames
        nconds = 0
        for m in methods:
            c = gi(m, "Conditions")
            if isinstance(c, list):
                nconds += len(c)
        T.append({
            "i": i, "raw": t, "id": tid, "name": gi(t, "Name", default=type_short(tid)),
            "full": gi(t, "FullName", default=tid[2:]), "ns": gi(t, "Namespace", default=""),
            "kind": gi(t, "Kind", default="?"), "src": src, "mods": gi(t, "Modifiers", default=[]) or [],
            "base": base_txt, "ifaces": [gi(x, "Text", default=str(x)) if isinstance(x, dict) else str(x)
                                         for x in (gi(t, "Interfaces", default=[]) or [])],
            "attrs": gi(t, "Attributes", default=[]) or [],
            "fields": fields, "methods": methods, "mnames": mnames, "props": props,
            "loc": sum(file_lines.get(s, 0) for s in src), "is_form": is_form, "nconds": nconds,
            "ir_irfile": irf,
        })
    M["types"] = T
    M["by_id"] = dict((x["id"], x) for x in T)
    M["files"] = files
    return M


def scan_ir(ir, M):
    """One pass over the whole JSON: strings, SQL, connection strings, VB6 patterns, emails."""
    strings, kv_hits = [], []
    collect(ir, "", strings, kv_hits)
    S = {"kv_hits": kv_hits}
    # emails anywhere
    emails = set()
    for _, s in strings:
        for m in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", s):
            emails.add(m)
    S["emails"] = sorted(emails)
    # code strings inside Types
    parents = defaultdict(dict)
    for p, s_ in strings:
        if not p.startswith("Types["):
            continue
        last = re.sub(r"\[\d+\]$", "", p.split(".")[-1]).lower()
        if last in CODE_KEYS:
            parent = p.rsplit(".", 1)[0] if "." in p else p
            parents[parent][last] = (p, s_)
    code, used_keys = [], Counter()
    for parent, d in parents.items():
        if "initializercode" in d:
            code.append(d["initializercode"])
        for k in ("rawcode", "normalizedcode", "body", "code", "sourcetext"):
            if k in d:
                code.append(d[k])
                used_keys[k] += 1
                break
    S["code_strings"] = code
    S["code_key_used"] = "/".join(k for k, _ in used_keys.most_common()) or "none"

    def tidx(p):
        m = re.match(r"Types\[(\d+)\]", p)
        return int(m.group(1)) if m else None

    lit_re = re.compile(r'"((?:[^"]|"")*)"')
    sql_re = re.compile(r"\b(select\b.+?\bfrom\b|insert\s+into\b|update\b.+?\bset\b|delete\s+from\b|exec(?:ute)?\s+\w+|create\s+table\b|alter\s+table\b)", re.I | re.S)
    conn_re = re.compile(r"(data source|server|provider|initial catalog|database|integrated security|user id|uid|pwd|password)\s*=", re.I)
    sql_lits, conns, seen = [], [], set()
    for p, s in code:
        for lit in lit_re.findall(s):
            if len(lit) < 8:
                continue
            if conn_re.search(lit) and len(set(x.lower() for x in conn_re.findall(lit))) >= 2:
                masked = re.sub(r"(?i)(password|pwd|user id|uid)\s*=\s*[^;\"]*", r"\1=***", lit)
                key = ("c", masked)
                if key not in seen:
                    seen.add(key)
                    conns.append((p, trunc(oneline(masked), 300)))
            elif sql_re.search(lit):
                key = ("s", lit)
                if key not in seen:
                    seen.add(key)
                    sql_lits.append((p, trunc(oneline(lit), 300)))
    S["sql"] = sql_lits[:400]
    S["conns"] = conns[:30]
    # command-type hints
    S["sp_hint_count"] = sum(len(re.findall(r"CommandType\.StoredProcedure", s)) for _, s in code)

    # VB6-style / debt patterns
    pats = [
        ("On Error statements", r"\bOn\s+Error\b", "High"),
        ("GoTo / GoSub", r"\b(GoTo|GoSub)\b", "Medium"),
        ("Variant keyword", r"\bVariant\b", "High"),
        ("CreateObject (late-bound COM)", r"\bCreateObject\s*\(", "High"),
        ("Late binding: 'As Object' declarations (event-handler 'sender' excluded)", r"(?<!sender )\bAs\s+Object\b", "Medium"),
        ("Classic ADODB usage", r"\bADODB\.", "High"),
        ("SQL built by string concatenation", r'"[^"]*\b(select|insert|update|delete)\b[^"]*"\s*&\s*[\w(]', "High"),
        ("DoEvents", r"\bDoEvents\b", "Medium"),
        ("Hard-coded drive paths", r'"[A-Za-z]:\\', "Medium"),
        ("Hard-coded IP addresses", r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "Medium"),
        ("Hard-coded URLs", r"https?://", "Low"),
        ("VB runtime helpers (MsgBox/InputBox/Val/CInt/IIf/Left/Mid...)", r"\b(MsgBox|InputBox|Val|CInt|CStr|CDbl|CDate|IIf|Mid|Left|Right)\s*\(", "Low"),
        ("ByRef parameters", r"\bByRef\b", "Low"),
        ("Bare 'End' statement", r"(?im)^\s*End\s*$", "Medium"),
        ("Empty Catch blocks", r"(?is)\bCatch\b[^\n]*\n\s*End\s+Try", "Medium"),
    ]
    code_text_by_type = defaultdict(list)
    for p, s in code:
        code_text_by_type[tidx(p)].append(s)
    debt = []
    for name, rx, rating in pats:
        cre = re.compile(rx, re.I)
        total, per_type, sample = 0, Counter(), None
        for ti, texts in code_text_by_type.items():
            n = sum(len(cre.findall(x)) for x in texts)
            if n:
                total += n
                per_type[ti] += n
                if sample is None:
                    sample = "Types[%s]" % ti
        debt.append({"name": name, "rating": rating, "count": total, "top": per_type.most_common(3), "sample": sample})
    S["debt"] = debt
    S["trycatch"] = sum(len(re.findall(r"\bTry\b", x)) for _, x in code)
    S["catch"] = sum(len(re.findall(r"\bCatch\b", x)) for _, x in code)
    S["logging_hits"] = Counter()
    for _, x in code:
        for kw in ("Trace.", "Debug.", "log4net", "NLog", "EventLog", "File.AppendAllText", "StreamWriter"):
            if kw in x:
                S["logging_hits"][kw] += x.count(kw)

    # security evidence (identifier counts per type)
    sec_kw = ["password", "md5", "sha1", "sha256", "hash", "encrypt", "decrypt", "login", "role", "admin", "permission", "otp", "captcha"]
    sec = defaultdict(Counter)
    for ti, texts in code_text_by_type.items():
        low = " ".join(texts).lower()
        for kw in sec_kw:
            n = low.count(kw)
            if n:
                sec[kw][ti] = n
    S["security"] = sec

    # duplicate method bodies
    dups = defaultdict(list)
    for T in M["types"]:
        for j, m in enumerate(T["methods"]):
            if not isinstance(m, dict):
                continue
            nm = T["mnames"][j] if j < len(T["mnames"]) else ""
            if nm in ("InitializeComponent", "Dispose", ".ctor", "#ctor"):
                continue
            body = gi(m, "NormalizedCode", "RawCode", "Body", "Code")
            if isinstance(body, str) and len(body) >= 120:
                h = hashlib.md5(re.sub(r"\s+", "", body).lower().encode("utf-8")).hexdigest()
                dups[h].append("Types[%d].Methods[%d]" % (T["i"], j))
    S["dups"] = [v for v in dups.values() if len(v) > 1]

    # conditions (business rules raw material)
    conds = []
    for T in M["types"]:
        for j, m in enumerate(T["methods"]):
            c = gi(m, "Conditions") if isinstance(m, dict) else None
            if isinstance(c, list):
                for k, item in enumerate(c):
                    conds.append("[JSON: Types[%d].Methods[%d].Conditions[%d]] %s %s" % (
                        T["i"], j, k, T["mnames"][j] if j < len(T["mnames"]) else "", compact(item, 260)))
    S["conds"] = conds
    return S


def build_graphs(ir, M):
    G = {}
    ids = set(M["by_id"])
    tg = ir.get("TypeDependencyGraph") or {}
    cg = ir.get("CallGraph") or {}
    out_deps, in_deps, vendor_use = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    for e in tg.get("Edges") or []:
        frm, to = gi(e, "From"), gi(e, "To")
        if frm not in ids:
            continue
        if to in ids:
            if to != frm:
                out_deps[frm][to] += 1
                in_deps[to][frm] += 1
        elif isinstance(to, str) and to.startswith("unresolved-type:"):
            disp = gi(e, "TargetDisplay", default=to[len("unresolved-type:"):])
            vendor_use[frm][vendor_of(disp)] += 1
    call_t = defaultdict(Counter)
    call_internal = 0
    for e in cg.get("Edges") or []:
        frm, to = gi(e, "From"), gi(e, "To")
        ft = method_type(frm)
        tt = method_type(to) if isinstance(to, str) else None
        if gi(e, "IsInternal") is True:
            call_internal += 1
        if ft in ids and tt in ids and ft != tt:
            call_t[ft][tt] += 1
    G.update(out_deps=out_deps, in_deps=in_deps, vendor_use=vendor_use, call_t=call_t, call_internal=call_internal)
    G["tg_order"] = tg.get("SuggestedMigrationOrder") or []
    G["tg_scc"] = tg.get("StronglyConnectedComponents") or []
    G["cg_order"] = cg.get("SuggestedMigrationOrder") or []
    return G


def vendor_from_dep(name):
    base = re.sub(r"[._]v?\d+(?:[._]\d+)*(?:\.\w+)?$", "", name)
    return re.split(r"[._]", base)[0]


def version_from_dep(name):
    mv = re.search(r"Version=(\d+(?:\.\d+)+)", name)
    if mv:
        return mv.group(1)
    m = re.search(r"[._]v?(\d+(?:[._]\d+)+)", name)
    return m.group(1).replace("_", ".") if m else ""


def complexity(T):
    score = T["loc"] / 400.0 + len(T["methods"]) / 15.0 + T["nconds"] / 15.0
    return "High" if score >= 4 else ("Medium" if score >= 1.5 else "Low")




# ============================ HTML TEMPLATE ===========================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>/*__TITLE__*/</title>
<style>
:root{--bg:#ffffff;--panel:#f6f7f9;--text:#1c2330;--sub:#5b6578;--line:#d5d9e2;--accent:#2563eb;--orange:#ea580c;--red:#e11d48;--warn-bg:#fff4e0;--warn-tx:#7a4a00;--ok-bg:#eaf3ff;--ok-tx:#1d4570}
@media (prefers-color-scheme: dark){:root{--bg:#16181d;--panel:#1e2128;--text:#e6e8ee;--sub:#9aa3b5;--line:#343a46;--accent:#60a5fa;--orange:#fb923c;--red:#fb7185;--warn-bg:#3a2e14;--warn-tx:#f2cf8a;--ok-bg:#1c2a3d;--ok-tx:#a9c8ee}}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--text);display:flex;flex-direction:column}
header{padding:10px 16px;border-bottom:1px solid var(--line)}
h1{font-size:17px;margin:0 0 2px}
.sub{color:var(--sub);font-size:12.5px}
.banner{margin-top:8px;padding:7px 10px;border-radius:6px;font-size:12.5px;background:var(--ok-bg);color:var(--ok-tx)}
.banner.warn{background:var(--warn-bg);color:var(--warn-tx)}
.toolbar{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;padding:8px 16px;border-bottom:1px solid var(--line)}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.seg button{border:0;background:transparent;color:var(--text);padding:5px 11px;cursor:pointer;font:inherit}
.seg button.on{background:var(--accent);color:#fff}
button.tool,input[type=search],select{border:1px solid var(--line);background:var(--bg);color:var(--text);border-radius:6px;padding:5px 9px;font:inherit}
button.tool{cursor:pointer}
input[type=search]{min-width:150px}
label.sl{display:flex;align-items:center;gap:6px;color:var(--sub);font-size:12.5px;white-space:nowrap}
details.kinds{position:relative}
details.kinds summary{cursor:pointer;border:1px solid var(--line);border-radius:6px;padding:5px 9px;list-style:none;color:var(--text)}
details.kinds div{position:absolute;z-index:6;background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:8px 10px;margin-top:4px;min-width:230px;box-shadow:0 4px 14px rgba(0,0,0,.18)}
details.kinds label{display:block;white-space:nowrap;font-size:12.5px}
.main{flex:1;min-height:0;display:flex}
#stage{flex:1;min-width:0;position:relative}
#svg{width:100%;height:100%;display:block;cursor:grab;touch-action:none}
#svg.drag{cursor:grabbing}
#side{width:350px;max-width:42vw;border-left:1px solid var(--line);background:var(--panel);padding:12px 14px;overflow:auto;font-size:13px}
#side h2{font-size:15px;margin:0 0 4px}
#side table,.tab table{width:100%;border-collapse:collapse;margin:6px 0 10px}
#side td,#side th,.tab td,.tab th{padding:3px 5px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
#side th,.tab th{color:var(--sub);font-weight:600;font-size:12px}
.tag{display:inline-block;font-size:11.5px;padding:1px 7px;border-radius:10px;border:1px solid var(--line);color:var(--sub);margin:0 4px 4px 0}
.tag.red{color:var(--red);border-color:var(--red)}
.src{color:var(--sub);font:12px ui-monospace,Consolas,monospace}
a.lnk{color:var(--accent);cursor:pointer;text-decoration:underline}
.kpis{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin:8px 0}
.kpis div{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:5px 8px}
.kpis b{display:block;font-size:16px}
.btns button{margin:4px 4px 0 0}
.tab{flex:1;min-height:0;overflow:auto;padding:12px 16px;display:none}
.tab.on{display:block}
.tab th.s{cursor:pointer;user-select:none}
.tab tr.row{cursor:pointer}
.tab tr.row:hover{background:var(--panel)}
#tip{position:fixed;pointer-events:none;background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:6px 9px;font-size:12.5px;max-width:330px;box-shadow:0 4px 14px rgba(0,0,0,.18);display:none;z-index:7}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;padding:7px 16px;border-top:1px solid var(--line);color:var(--sub);font-size:12px}
.legend i{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:5px;vertical-align:-2px;border:2px solid}
.chip{display:inline-block;margin:2px 4px 2px 0;padding:1px 8px;border:1px solid var(--line);border-radius:10px;cursor:pointer;background:var(--bg)}
@media (max-width:820px){.main{flex-direction:column}#side{width:100%;max-width:none;max-height:38vh;border-left:0;border-top:1px solid var(--line)}}
</style>
</head>
<body>
<header>
  <h1 id="title"></h1>
  <div class="sub" id="subtitle"></div>
  <div class="banner" id="banner"></div>
</header>
<div class="toolbar">
  <div class="seg" id="tabs" role="group" aria-label="View"><button data-tab="graph" class="on">Graph</button><button data-tab="metrics">Metrics</button><button data-tab="order">Migration order</button><button data-tab="cycles">Cycles</button></div>
  <input type="search" id="q" placeholder="Search types..." aria-label="Search">
  <label class="sl">Colour by <select id="colorBy"><option value="role">Role</option><option value="level">Level</option><option value="coupling">Coupling</option><option value="cycle">Cycles</option></select></label>
  <details class="kinds" id="kindsBox"><summary>Edge kinds</summary><div id="kinds"></div></details>
  <label class="sl">Min weight <input type="range" id="minw" min="1" max="10" value="1"> <b id="minwv">1</b></label>
  <label class="sl"><input type="checkbox" id="showExt"> external libraries</label>
  <label class="sl">Impact <select id="mode"><option value="direct">Direct links</option><option value="deps">What it needs (transitive)</option><option value="dpd">What breaks if changed (transitive)</option><option value="both">Both</option></select></label>
  <label class="sl">Depth <select id="depth"></select></label>
  <span class="sub" id="shown"></span>
  <button class="tool" id="zin" title="Zoom in">+</button><button class="tool" id="zout" title="Zoom out">&minus;</button><button class="tool" id="fit">Fit</button>
  <button class="tool" id="dl">SVG</button><button class="tool" id="csv">CSV</button>
</div>
<div class="main" id="tab-graph">
  <div id="stage"><svg id="svg" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Dependency graph"></svg></div>
  <aside id="side"></aside>
</div>
<div class="tab" id="tab-metrics"></div>
<div class="tab" id="tab-order"></div>
<div class="tab" id="tab-cycles"></div>
<div class="legend" id="legend"></div>
<div id="tip"></div>
<script id="data" type="application/json">/*__DATA__*/</script>
<script>
(function(){
"use strict";
var D = JSON.parse(document.getElementById('data').textContent);
var NS='http://www.w3.org/2000/svg';
function $(id){return document.getElementById(id);}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function fmt(n){return (n||0).toLocaleString();}
function trunc(s,n){s=String(s);return s.length>n?s.slice(0,n-1)+'\u2026':s;}
function el(tag,attrs,parent,text){var e=document.createElementNS(NS,tag);for(var k in attrs){e.setAttribute(k,attrs[k]);}if(text!=null)e.textContent=text;if(parent)parent.appendChild(e);return e;}
var svg=$('svg'), tip=$('tip'), side=$('side');
var T=D.types, E=D.edges, maxL=D.meta.maxLevel;
var typeById={}; T.forEach(function(t){typeById[t.i]=t;});
var extById={}; D.ext.forEach(function(x){extById[x.id]=x;});
var kindsAll=Object.keys(D.meta.edgeKinds).filter(function(k){return E.some(function(e){return e.k[k]!=null;});});
var S={tab:'graph',color:'role',kinds:{},minW:1,showExt:false,sel:null,hov:null,mode:'direct',depth:2,cycle:null,query:'',sortKey:'order',sortDir:1};
kindsAll.forEach(function(k){S.kinds[k]=true;});
var vb={x:0,y:0,w:1500,h:900}, content={w:1500,h:900}, nodes={}, nodeList=[], bands=[], vis=[], outAdj={}, inAdj={}, edgeEls=[], nodeEls={}, lab=null;

/* ---------- palette ---------- */
function dark(){return window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches;}
function pal(){var d=dark();return {bg:d?'#16181d':'#ffffff',text:d?'#e6e8ee':'#1c2330',sub:d?'#9aa3b5':'#5b6578',line:d?'#7d879b':'#8a93a6',accent:d?'#60a5fa':'#2563eb',orange:d?'#fb923c':'#ea580c',red:d?'#fb7185':'#e11d48',d:d};}
var ROLE={form:'#3b82f6',data:'#f59e0b',generated:'#94a3b8',other:'#10b981'};
var ROLENAME={form:'Form (UI)',data:'Data access',generated:'Generated (My.*)',other:'Other / business'};
function maxOf(f){var m=1;T.forEach(function(t){var v=f(t);if(v>m)m=v;});return m;}
var maxCoup=maxOf(function(t){return t.fanin+t.fanout;});
function nodeColor(t,P){
  if(S.color==='role')return ROLE[t.role]||ROLE.other;
  if(S.color==='level'){var f=maxL?t.level/maxL:0;return 'hsl('+Math.round(150+110*f)+',60%,'+(P.d?'62%':'42%')+')';}
  if(S.color==='coupling'){var c=(t.fanin+t.fanout)/maxCoup;return 'hsl('+Math.round(55-55*c)+',85%,'+(P.d?'58%':'46%')+')';}
  return t.cyc>=0?P.red:(t.fanin+t.fanout===0?'#94a3b8':'#10b981');
}

/* ---------- visible edges, adjacency ---------- */
function visEdges(){
  var out=[];
  E.forEach(function(e,n){
    var w=0;for(var k in e.k){if(S.kinds[k])w+=e.k[k];}
    if(w>=S.minW&&w>0)out.push({s:e.s,t:e.t,w:w,k:e.k,cyc:e.cyc,idx:n,e:e,x:false});
  });
  if(S.showExt)D.xEdges.forEach(function(x){if(x.w>=S.minW)out.push({s:x.s,t:x.t,w:x.w,k:{},cyc:false,idx:-1,e:null,x:true});});
  return out;
}
function buildAdj(){outAdj={};inAdj={};vis.forEach(function(e){if(e.x)return;(outAdj[e.s]=outAdj[e.s]||[]).push(e.t);(inAdj[e.t]=inAdj[e.t]||[]).push(e.s);});}
function reach(start,adj,depth){var seen={};seen[start]=0;var q=[start];while(q.length){var u=q.shift(),d=seen[u];if(d>=depth)continue;(adj[u]||[]).forEach(function(v){if(seen[v]===undefined){seen[v]=d+1;q.push(v);}});}return seen;}

/* ---------- layout ---------- */
function layout(){
  nodes={};nodeList=[];bands=[];
  var cw=150,ch=34,gx=12,gy=10,left=120,W=1500,per=Math.max(3,Math.floor((W-left-30)/(cw+gx))),y=40;
  for(var L=maxL;L>=0;L--){
    var list=T.filter(function(t){return t.level===L;}).sort(function(a,b){return a.row-b.row;});
    if(!list.length)continue;
    var nr=Math.ceil(list.length/per), h=nr*(ch+gy)+14;
    bands.push({y:y,h:h,level:L,count:list.length});
    list.forEach(function(t,k){
      var r=Math.floor(k/per),c=k%per,inRow=Math.min(per,list.length-r*per),off=((per-inRow)*(cw+gx))/2;
      var n={id:t.i,kind:'type',t:t,x:left+off+c*(cw+gx)+cw/2,y:y+7+r*(ch+gy)+ch/2,w:cw,h:ch};
      nodes[t.i]=n;nodeList.push(n);
    });
    y+=h+16;
  }
  if(S.showExt&&D.ext.length){
    var perx=Math.max(3,Math.floor((W-left-30)/182)),nr2=Math.ceil(D.ext.length/perx);
    bands.push({y:y,h:nr2*44+14,level:-1,count:D.ext.length});
    D.ext.forEach(function(x,k){var r=Math.floor(k/perx),c=k%perx,inRow=Math.min(perx,D.ext.length-r*perx),off=((perx-inRow)*182)/2;
      var n={id:x.id,kind:'ext',x:x,x0:0,label:x.label,xx:left+off+c*182+85,x_:0,w:170,h:30};
      n.x=left+off+c*182+85;n.y=y+7+r*44+15;n.ext=x;nodes[x.id]=n;nodeList.push(n);});
    y+=nr2*44+30;
  }
  content={w:W,h:y+10};
}

/* ---------- edge geometry ---------- */
function spread(list){
  var anchors={};
  function add(id,side,rec){var k=id+'~~'+side;(anchors[k]=anchors[k]||[]).push(rec);}
  list.forEach(function(e){
    var a=nodes[e.s],b=nodes[e.t],same=Math.abs(a.y-b.y)<2,down=b.y>a.y;
    e._ss=same?'t':(down?'b':'t');e._es=same?'t':(down?'t':'b');
    add(e.s,e._ss,{e:e,k:'s',o:b.x});add(e.t,e._es,{e:e,k:'t',o:a.x});
  });
  Object.keys(anchors).forEach(function(k){
    var l=anchors[k];l.sort(function(p,q){return p.o-q.o;});
    var n=nodes[k.split('~~')[0]],span=Math.min(n.w*0.85,14*(l.length-1));
    l.forEach(function(r,i){var off=l.length>1?(i/(l.length-1)-0.5)*span:0;if(r.k==='s')r.e._sx=n.x+off;else r.e._tx=n.x+off;});
  });
}
function path(e){
  var a=nodes[e.s],b=nodes[e.t],x1=e._sx,x2=e._tx;
  var y1=a.y+(e._ss==='b'?a.h/2:-a.h/2),y2=b.y+(e._es==='b'?b.h/2:-b.h/2),c1x,c1y,c2x,c2y,dy;
  if(Math.abs(a.y-b.y)<2){dy=Math.min(70,22+Math.abs(a.x-b.x)*0.1);c1x=x1;c1y=y1-dy;c2x=x2;c2y=y2-dy;}
  else{var down=b.y>a.y;dy=Math.max(24,Math.abs(y2-y1)*0.4)*(down?1:-1);c1x=x1;c1y=y1+dy;c2x=x2;c2y=y2-dy;}
  return 'M'+x1+','+y1+' C'+c1x+','+c1y+' '+c2x+','+c2y+' '+x2+','+y2;
}

/* ---------- focus ---------- */
function focus(){
  var f=S.sel!=null?S.sel:S.hov, info={n:{},mode:null,f:f};
  if(S.cycle!=null){var c=D.cycles[S.cycle];if(c){c.members.forEach(function(i){info.n[i]='cyc';});info.cyc=true;return info;}}
  if(f==null)return info;
  if(typeof f==='string'){info.n[f]='self';vis.forEach(function(e){if(e.x&&e.t===f)info.n[e.s]='dep';});return info;}
  var m=S.sel!=null?S.mode:'direct', d=m==='direct'?1:S.depth;
  var o=(m==='direct'||m==='deps'||m==='both')?reach(f,outAdj,d):{}, i_=(m==='direct'||m==='dpd'||m==='both')?reach(f,inAdj,d):{};
  info.out=o;info.inn=i_;info.n[f]='self';
  Object.keys(o).forEach(function(k){if(+k!==f)info.n[k]='dep';});
  Object.keys(i_).forEach(function(k){if(+k!==f)info.n[k]=(info.n[k]==='dep'?'both':'dpd');});
  return info;
}
function edgeTag(e,info){
  if(info.cyc){return (info.n[e.s]&&info.n[e.t]&&!e.x)?'cyc':null;}
  if(info.f==null)return null;
  if(typeof info.f==='string'){return (e.x&&e.t===info.f)?'dep':null;}
  if(!e.x&&info.out&&info.out[e.s]!==undefined&&info.out[e.t]!==undefined)return 'dep';
  if(!e.x&&info.inn&&info.inn[e.s]!==undefined&&info.inn[e.t]!==undefined)return 'dpd';
  return null;
}

/* ---------- render ---------- */
function render(){
  var P=pal();
  vis=visEdges();buildAdj();layout();
  var shown=vis.filter(function(e){return nodes[e.s]&&nodes[e.t];}).sort(function(a,b){return b.w-a.w;});
  var capped=shown.length>1500; if(capped)shown=shown.slice(0,1500);
  while(svg.firstChild)svg.removeChild(svg.firstChild);
  var defs=el('defs',{},svg);
  [['a0',P.line],['a1',P.accent],['a2',P.orange],['a3',P.red]].forEach(function(m){
    var mk=el('marker',{id:m[0],viewBox:'0 0 10 10',refX:'9',refY:'5',markerUnits:'userSpaceOnUse',markerWidth:'9',markerHeight:'9',orient:'auto'},defs);
    el('path',{d:'M0,0 L10,5 L0,10 z',fill:m[1]},mk);
  });
  el('rect',{x:-3000,y:-3000,width:content.w+6000,height:content.h+6000,fill:P.bg,id:'bgrect'},svg);
  var root=el('g',{},svg);
  bands.forEach(function(b,k){
    el('rect',{x:10,y:b.y,width:content.w-20,height:b.h,rx:8,fill:P.d?'#ffffff':'#000000','fill-opacity':k%2?'0.025':'0.05'},root);
    var lbl=b.level<0?'External':'Level '+b.level;
    el('text',{x:22,y:b.y+22,fill:P.text,'font-size':'13','font-weight':'700'},root,lbl);
    el('text',{x:22,y:b.y+38,fill:P.sub,'font-size':'11'},root,b.level<0?'unresolved libs':(b.level===0?'foundation':b.count+(b.count===1?' type':' types')));
  });
  spread(shown);
  var eg=el('g',{},root);edgeEls=[];
  var base=shown.length>200?0.18:0.42;
  shown.forEach(function(e){
    var sw=Math.max(1,Math.min(4,1+Math.log(e.w+1)/Math.log(2)*0.5));
    var d=path(e);
    var p=el('path',{d:d,fill:'none','stroke-width':sw,'stroke-dasharray':e.x?'4 3':'', 'data-s':e.s,'data-t':e.t},eg);
    var ttl=(e.x?nodes[e.s].t.name+' \u2192 '+nodes[e.t].label:nodes[e.s].t.name+' depends on '+nodes[e.t].t.name)+': '+e.w;
    el('title',{},p,ttl);
    var hit=null;
    if(!e.x&&shown.length<=900){hit=el('path',{d:d,fill:'none',stroke:'transparent','stroke-width':'10',cursor:'pointer'},eg);hit.addEventListener('click',function(ev){ev.stopPropagation();showEdge(e);});}
    edgeEls.push({el:p,e:e,sw:sw});
  });
  lab=el('g',{},root);
  var ng=el('g',{},root);nodeEls={};
  nodeList.forEach(function(n){
    var g=el('g',{'class':'node','data-id':n.id,cursor:'pointer'},ng);
    if(n.kind==='type'){
      var c=nodeColor(n.t,P);
      el('rect',{x:n.x-n.w/2,y:n.y-n.h/2,width:n.w,height:n.h,rx:6,fill:c,'fill-opacity':'0.2',stroke:c,'stroke-width':'1.6'},g);
      el('text',{x:n.x,y:n.y+4,'text-anchor':'middle',fill:P.text,'font-size':'12','font-weight':'600'},g,trunc(n.t.name,22));
      if(n.t.cyc>=0)el('circle',{cx:n.x+n.w/2-6,cy:n.y-n.h/2+6,r:'4.5',fill:P.red,stroke:P.bg,'stroke-width':'1.5'},g);
    } else {
      el('rect',{x:n.x-n.w/2,y:n.y-n.h/2,width:n.w,height:n.h,rx:15,fill:'#64748b','fill-opacity':'0.12',stroke:'#64748b','stroke-width':'1.4','stroke-dasharray':'5 4'},g);
      el('text',{x:n.x,y:n.y+4,'text-anchor':'middle',fill:P.text,'font-size':'12'},g,trunc(n.label,24));
    }
    nodeEls[n.id]=g;
    g.addEventListener('mouseenter',function(ev){S.hov=(n.kind==='type'?n.id:n.id);applyFocus();showTip(ev,n);});
    g.addEventListener('mousemove',moveTip);
    g.addEventListener('mouseleave',function(){S.hov=null;applyFocus();tip.style.display='none';});
    g.addEventListener('click',function(ev){ev.stopPropagation();select(S.sel===n.id?null:n.id);});
  });
  render.P=P;render.base=base;
  $('shown').textContent=shown.length+' links shown'+(capped?' (strongest 1500)':'');
  applyFocus();setViewBox();
}
function applyFocus(){
  var P=render.P;if(!P||!nodeEls)return;
  var info=focus(),any=info.f!=null||info.cyc;
  edgeEls.forEach(function(o){
    var tag=edgeTag(o.e,info),col=P.line,mk='a0',op=render.base;
    if(o.e.cyc&&!o.e.x){col=P.red;mk='a3';op=Math.max(op,0.6);}
    if(tag==='dep'){col=P.accent;mk='a1';op=1;}else if(tag==='dpd'){col=P.orange;mk='a2';op=1;}else if(tag==='cyc'){col=P.red;mk='a3';op=1;}
    else if(any)op=0.04;
    o.el.setAttribute('stroke',col);o.el.setAttribute('marker-end','url(#'+mk+')');o.el.setAttribute('opacity',String(op));
    o.el.setAttribute('stroke-width',String(tag?Math.max(o.sw,1.8):o.sw));
  });
  while(lab.firstChild)lab.removeChild(lab.firstChild);
  if(any)edgeEls.forEach(function(o){var tag=edgeTag(o.e,info);if(!tag||edgeEls.length>400)return;
    var a=nodes[o.e.s],b=nodes[o.e.t];
    el('text',{x:(o.e._sx+o.e._tx)/2+4,y:(a.y+b.y)/2,'font-size':'11','font-weight':'700',fill:tag==='dpd'?P.orange:(tag==='cyc'?P.red:P.accent),'paint-order':'stroke',stroke:P.bg,'stroke-width':'3'},lab,String(o.e.w));});
  var q=S.query.toLowerCase();
  nodeList.forEach(function(n){
    var g=nodeEls[n.id],r=g.firstChild,tag=info.n[n.id],hit=q&&n.kind==='type'&&(n.t.name+' '+n.t.full).toLowerCase().indexOf(q)>=0;
    var dim=(any&&!tag)||(!any&&q&&!hit);
    g.setAttribute('opacity',dim?'0.2':'1');
    var sw=1.6,st=null;
    if(tag==='self'){sw=3.5;st=P.text;}else if(tag==='dep'){sw=2.8;st=P.accent;}else if(tag==='dpd'){sw=2.8;st=P.orange;}else if(tag==='both'){sw=2.8;st=P.red;}else if(tag==='cyc'){sw=2.8;st=P.red;}
    if(hit){sw=3;st=P.accent;}
    if(st){r.setAttribute('stroke',st);}else if(n.kind==='type'){r.setAttribute('stroke',nodeColor(n.t,P));}else{r.setAttribute('stroke','#64748b');}
    r.setAttribute('stroke-width',String(sw));
  });
}

/* ---------- tooltip & side panel ---------- */
function tipHtml(n){
  if(n.kind==='ext')return '<b>'+esc(n.label)+'</b><br>external / unresolved library<br>'+n.ext.total+' references from '+n.ext.types+' types';
  var t=n.t;return '<b>'+esc(t.full)+'</b><br>'+esc(ROLENAME[t.role])+' \u00b7 level '+t.level+(t.cyc>=0?' \u00b7 <span style="color:var(--red)">in cycle '+(t.cyc+1)+'</span>':'')+'<br>depends on '+t.fanout+' ('+t.tout+' transitive) \u00b7 used by '+t.fanin+' ('+t.tin+' transitive)<br><span class="sub">'+esc(t.src)+'</span>';
}
function showTip(ev,n){tip.innerHTML=tipHtml(n);tip.style.display='block';moveTip(ev);}
function moveTip(ev){var x=ev.clientX+14,y=ev.clientY+14,w=tip.offsetWidth,h=tip.offsetHeight;if(x+w>innerWidth-8)x=ev.clientX-w-14;if(y+h>innerHeight-8)y=ev.clientY-h-14;tip.style.left=x+'px';tip.style.top=y+'px';}
function tbl(head,rows){if(!rows.length)return '<p class="sub">none</p>';return '<table><tr>'+head.map(function(h){return '<th>'+esc(h)+'</th>';}).join('')+'</tr>'+rows.map(function(r){return '<tr>'+r.map(function(c){return '<td>'+c+'</td>';}).join('')+'</tr>';}).join('')+'</table>';}
function lnk(i,text){return '<a class="lnk" data-go="'+esc(i)+'">'+esc(text)+'</a>';}
function kinds(k){return Object.keys(k).map(function(x){return esc(x)+' \u00d7'+k[x];}).join(', ');}
function select(id){S.sel=id;S.cycle=null;applyFocus();showSide(id);}
function showSide(id){
  if(id==null){side.innerHTML='<b>Details</b><p class="sub">Hover a box to see its direct links. Click a box to pick it, then choose <i>Impact</i> in the toolbar to see everything it needs or everything that breaks if it changes. Click a link for its JSON evidence. Scroll to zoom, drag to pan.</p><div class="kpis"><div><b>'+D.meta.types+'</b>types</div><div><b>'+D.meta.pairs+'</b>type-to-type dependencies</div><div><b>'+D.meta.cycles+'</b>cycles</div><div><b>'+(D.meta.maxLevel+1)+'</b>dependency levels</div></div>';return;}
  var h='';
  if(typeof id==='string'){
    var x=extById[id],users=D.xEdges.filter(function(e){return e.t===id;}).sort(function(a,b){return b.w-a.w;});
    h+='<h2>'+esc(x.label)+'</h2><span class="tag">external / unresolved library</span><p>'+x.total+' references from '+x.types+' types.</p><b>Used by</b>'+tbl(['Type','Refs'],users.slice(0,30).map(function(e){return [lnk(e.s,typeById[e.s].name),e.w];}));
  } else {
    var t=typeById[id];
    h+='<h2>'+esc(t.name)+'</h2><div class="src">'+esc(t.full)+'</div><span class="tag">'+esc(ROLENAME[t.role])+'</span><span class="tag">level '+t.level+'</span>'+(t.cyc>=0?'<span class="tag red">in cycle '+(t.cyc+1)+'</span>':'')+'<span class="tag">migration #'+t.order+'</span>';
    h+='<div class="kpis"><div><b>'+t.fanout+'</b>depends on directly</div><div><b>'+t.tout+'</b>depends on (all levels)</div><div><b>'+t.fanin+'</b>used by directly</div><div><b>'+t.tin+'</b>breaks if changed (all levels)</div></div>';
    h+='<p>Instability '+(t.instab==null?'n/a':t.instab)+' (0 = stable, 1 = unstable; heuristic) \u00b7 '+fmt(t.loc)+' LOC \u00b7 '+t.methods+' methods'+(t.self?' \u00b7 '+t.self+' self-references':'')+'</p>';
    h+='<div class="btns"><button class="tool" data-mode="deps">What it needs</button><button class="tool" data-mode="dpd">What breaks if changed</button><button class="tool" data-mode="both">Both</button><button class="tool" data-mode="direct">Direct only</button></div>';
    var outs=E.filter(function(e){return e.s===id;}).sort(function(a,b){return b.w-a.w;}),ins=E.filter(function(e){return e.t===id;}).sort(function(a,b){return b.w-a.w;});
    h+='<b>Depends on</b>'+tbl(['Type','Refs','Kinds'],outs.map(function(e){return [lnk(e.t,typeById[e.t].name),e.w,kinds(e.k)];}));
    h+='<b>Used by</b>'+tbl(['Type','Refs','Kinds'],ins.map(function(e){return [lnk(e.s,typeById[e.s].name),e.w,kinds(e.k)];}));
    h+='<b>External libraries</b>'+tbl(['Library','Refs'],t.ext.map(function(p){return [esc(p[0]),p[1]];}));
    h+='<p class="src">JSON: '+esc(t.src)+' \u00b7 files: '+esc(t.files.join(', ')||'n/a')+'</p>';
  }
  side.innerHTML=h;
}
function showEdge(e){
  var a=typeById[e.s],b=typeById[e.t],ed=e.e;
  var h='<h2>'+esc(a.name)+' \u2192 '+esc(b.name)+'</h2><p>'+esc(a.name)+' depends on '+esc(b.name)+'. Weight '+e.w+'.'+(e.cyc?' <span class="tag red">inside a cycle</span>':'')+'</p>';
  h+='<b>Kinds</b><p>'+kinds(ed.k)+'</p><b>Evidence (first '+ed.samples.length+')</b>'+tbl(['Kind','Member','Where'],ed.samples.map(function(s){return [esc(s[0]),esc(s[1]||'-'),esc(s[2]+(s[3]!==''&&s[3]!=null?':'+s[3]:''))];}));
  h+='<p class="src">JSON: '+esc(ed.ev.join(', '))+'</p>';
  side.innerHTML=h;
}
side.addEventListener('click',function(ev){
  var a=ev.target.closest&&ev.target.closest('a[data-go]');
  if(a){var v=a.getAttribute('data-go');select(/^x:/.test(v)?v:parseInt(v,10));if(nodes[S.sel])centerOn(nodes[S.sel]);return;}
  var b=ev.target.closest&&ev.target.closest('button[data-mode]');
  if(b){S.mode=b.getAttribute('data-mode');$('mode').value=S.mode;applyFocus();}
});

/* ---------- zoom / pan ---------- */
function setViewBox(){svg.setAttribute('viewBox',vb.x+' '+vb.y+' '+vb.w+' '+vb.h);}
function fit(){var r=svg.getBoundingClientRect(),ar=(r.width||1000)/(r.height||700),w=content.w+20,h=content.h+20;
  if(w/h<ar){vb.h=h;vb.w=h*ar;vb.x=-(vb.w-content.w)/2;vb.y=-10;}else{vb.w=w;vb.h=w/ar;vb.x=-10;vb.y=-10;}setViewBox();}
function zoomAt(f,cx,cy){var pt=svg.createSVGPoint();pt.x=cx;pt.y=cy;var m=svg.getScreenCTM();if(!m)return;var p=pt.matrixTransform(m.inverse());
  var nw=Math.max(150,Math.min(content.w*4,vb.w*f)),k=nw/vb.w;vb.x=p.x-(p.x-vb.x)*k;vb.y=p.y-(p.y-vb.y)*k;vb.w=nw;vb.h=vb.h*k;setViewBox();}
function centerOn(n){vb.x=n.x-vb.w/2;vb.y=n.y-vb.h/2;setViewBox();}
svg.addEventListener('wheel',function(ev){ev.preventDefault();zoomAt(ev.deltaY<0?0.88:1.14,ev.clientX,ev.clientY);},{passive:false});
var drag=null;
svg.addEventListener('pointerdown',function(ev){if(ev.target.closest&&ev.target.closest('.node'))return;drag={x:ev.clientX,y:ev.clientY,vx:vb.x,vy:vb.y,moved:false};svg.classList.add('drag');svg.setPointerCapture(ev.pointerId);});
svg.addEventListener('pointermove',function(ev){if(!drag)return;var m=svg.getScreenCTM();if(!m)return;var dx=(ev.clientX-drag.x)/m.a,dy=(ev.clientY-drag.y)/m.d;if(Math.abs(dx)+Math.abs(dy)>3)drag.moved=true;vb.x=drag.vx-dx;vb.y=drag.vy-dy;setViewBox();});
svg.addEventListener('pointerup',function(){if(drag&&!drag.moved){S.cycle=null;select(null);}drag=null;svg.classList.remove('drag');});

/* ---------- tables ---------- */
var COLS=[['Type','name'],['Role','role'],['Level','level'],['Fan-in','fanin'],['Fan-out','fanout'],['All dependents','tin'],['All dependencies','tout'],['Instability','instab'],['Cycle','cyc'],['External refs','extTotal'],['LOC','loc'],['Migration #','order']];
function sortedTypes(){
  var q=S.query.toLowerCase(),k=S.sortKey,d=S.sortDir;
  return T.filter(function(t){return !q||(t.name+' '+t.full).toLowerCase().indexOf(q)>=0;}).sort(function(a,b){
    var x=a[k],y=b[k];if(x==null)x=-1;if(y==null)y=-1;
    if(typeof x==='string')return d*x.localeCompare(y);return d*(x-y)||a.name.localeCompare(b.name);});
}
function renderMetrics(){
  var rows=sortedTypes();
  $('tab-metrics').innerHTML='<p class="sub">Click a column to sort, a row to open the type in the graph. Fan-in = types that depend on it, fan-out = types it depends on, "all" = including indirect. Instability = fan-out / (fan-in + fan-out), a heuristic.</p><table><tr>'+
    COLS.map(function(c){return '<th class="s" data-k="'+c[1]+'">'+esc(c[0])+(S.sortKey===c[1]?(S.sortDir>0?' \u25b2':' \u25bc'):'')+'</th>';}).join('')+'</tr>'+
    rows.map(function(t){return '<tr class="row" data-i="'+t.i+'"><td>'+esc(t.name)+'</td><td>'+esc(ROLENAME[t.role])+'</td><td>'+t.level+'</td><td>'+t.fanin+'</td><td>'+t.fanout+'</td><td>'+t.tin+'</td><td>'+t.tout+'</td><td>'+(t.instab==null?'-':t.instab)+'</td><td>'+(t.cyc>=0?'cycle '+(t.cyc+1):'-')+'</td><td>'+t.extTotal+'</td><td>'+fmt(t.loc)+'</td><td>'+t.order+'</td></tr>';}).join('')+'</table>';
}
function renderOrder(){
  var q=S.query.toLowerCase();
  $('tab-order').innerHTML='<p class="sub">Source: '+esc(D.meta.order_src)+'. Types with nothing below them (level 0) can be migrated first; a type is safest to migrate after everything it depends on.</p><table><tr><th>#</th><th>Type</th><th>Level</th><th>Depends on</th><th>Used by</th><th>Cycle</th></tr>'+
    D.order.filter(function(i){var t=typeById[i];return !q||(t.name+' '+t.full).toLowerCase().indexOf(q)>=0;}).map(function(i){var t=typeById[i];return '<tr class="row" data-i="'+i+'"><td>'+t.order+'</td><td>'+esc(t.name)+'</td><td>'+t.level+'</td><td>'+t.fanout+'</td><td>'+t.fanin+'</td><td>'+(t.cyc>=0?'cycle '+(t.cyc+1):'-')+'</td></tr>';}).join('')+'</table>';
}
function renderCycles(){
  var h='<p class="sub">A cycle is a group of types that depend on each other, directly or indirectly. They usually have to be migrated together. The IR itself reports '+D.meta.ir_scc+' strongly connected components; this script found '+D.meta.cycles+'.</p>';
  if(!D.cycles.length)h+='<p>No cycles between known types were found.</p>';
  D.cycles.forEach(function(c){
    var mem={};c.members.forEach(function(i){mem[i]=1;});
    var inner=E.filter(function(e){return mem[e.s]&&mem[e.t];});
    h+='<h3>Cycle '+(c.id+1)+' \u00b7 '+c.members.length+' types \u00b7 '+inner.length+' links <button class="tool" data-cyc="'+c.id+'">Show on graph</button></h3><div>'+c.members.map(function(i){return '<span class="chip" data-i="'+i+'">'+esc(typeById[i].name)+'</span>';}).join('')+'</div>';
    h+=tbl(['From','To','Weight'],inner.slice(0,30).map(function(e){return [esc(typeById[e.s].name),esc(typeById[e.t].name),e.w];}));
  });
  $('tab-cycles').innerHTML=h;
}
function goType(i){setTab('graph');select(i);if(nodes[i])centerOn(nodes[i]);}
['tab-metrics','tab-order','tab-cycles'].forEach(function(id){
  $(id).addEventListener('click',function(ev){
    var th=ev.target.closest&&ev.target.closest('th.s');
    if(th){var k=th.getAttribute('data-k');if(S.sortKey===k)S.sortDir=-S.sortDir;else{S.sortKey=k;S.sortDir=(k==='name'||k==='role')?1:-1;}renderMetrics();return;}
    var cb=ev.target.closest&&ev.target.closest('button[data-cyc]');
    if(cb){S.cycle=parseInt(cb.getAttribute('data-cyc'),10);S.sel=null;setTab('graph');applyFocus();showSide(null);return;}
    var r=ev.target.closest&&ev.target.closest('[data-i]');
    if(r)goType(parseInt(r.getAttribute('data-i'),10));
  });
});

/* ---------- controls ---------- */
function setTab(t){
  S.tab=t;
  Array.prototype.forEach.call($('tabs').children,function(b){b.classList.toggle('on',b.getAttribute('data-tab')===t);});
  $('tab-graph').style.display=t==='graph'?'flex':'none';
  ['metrics','order','cycles'].forEach(function(n){$('tab-'+n).classList.toggle('on',t===n);});
  if(t==='metrics')renderMetrics();if(t==='order')renderOrder();if(t==='cycles')renderCycles();
  if(t==='graph')setTimeout(fit,0);
  legend();
}
function legend(){
  var h='';
  if(S.tab!=='graph'){$('legend').innerHTML='<span>Switch to the Graph tab to see the picture.</span>';return;}
  if(S.color==='role')h=['form','data','other','generated'].map(function(r){return '<span><i style="border-color:'+ROLE[r]+';background:'+ROLE[r]+'33"></i>'+ROLENAME[r]+'</span>';}).join('');
  else if(S.color==='level')h='<span>Colour = dependency level (green = foundation, violet = top)</span>';
  else if(S.color==='coupling')h='<span>Colour = fan-in + fan-out (yellow = low, red = high)</span>';
  else h='<span><i style="border-color:#e11d48;background:#e11d4833"></i>in a cycle</span><span><i style="border-color:#10b981;background:#10b98133"></i>not in a cycle</span><span><i style="border-color:#94a3b8;background:#94a3b833"></i>no internal links</span>';
  h+='<span>Arrow: type \u2192 type it depends on. Blue = what the selection needs, orange = what depends on it, red = cycle. Red dot = member of a cycle. Levels: top depends on lower levels.</span>';
  $('legend').innerHTML=h;
}
$('tabs').addEventListener('click',function(ev){var b=ev.target.closest&&ev.target.closest('button[data-tab]');if(b)setTab(b.getAttribute('data-tab'));});
$('q').oninput=function(){S.query=this.value.trim();if(S.tab==='graph')applyFocus();else if(S.tab==='metrics')renderMetrics();else if(S.tab==='order')renderOrder();};
$('colorBy').onchange=function(){S.color=this.value;render();legend();};
$('minw').oninput=function(){S.minW=parseInt(this.value,10)||1;$('minwv').textContent=String(S.minW);render();};
$('showExt').onchange=function(){S.showExt=this.checked;render();fit();};
$('mode').onchange=function(){S.mode=this.value;applyFocus();};
$('depth').onchange=function(){S.depth=parseInt(this.value,10);applyFocus();};
$('zin').onclick=function(){var r=svg.getBoundingClientRect();zoomAt(0.8,r.left+r.width/2,r.top+r.height/2);};
$('zout').onclick=function(){var r=svg.getBoundingClientRect();zoomAt(1.25,r.left+r.width/2,r.top+r.height/2);};
$('fit').onclick=fit;
function download(name,type,text){var blob=new Blob([text],{type:type}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;document.body.appendChild(a);a.click();document.body.removeChild(a);setTimeout(function(){URL.revokeObjectURL(a.href);},500);}
$('dl').onclick=function(){var c=svg.cloneNode(true);c.setAttribute('viewBox','-10 -10 '+(content.w+20)+' '+(content.h+20));c.setAttribute('width',content.w+20);c.setAttribute('height',content.h+20);download('dependency_graph.svg','image/svg+xml',new XMLSerializer().serializeToString(c));};
$('csv').onclick=function(){
  var rows=[COLS.map(function(c){return c[0];})].concat(sortedTypes().map(function(t){return [t.name,t.full,t.role,t.level,t.fanin,t.fanout,t.tin,t.tout,t.instab==null?'':t.instab,t.cyc>=0?t.cyc+1:'',t.extTotal,t.loc,t.order];}));
  rows[0]=['Type','FullName','Role','Level','FanIn','FanOut','AllDependents','AllDependencies','Instability','Cycle','ExternalRefs','LOC','MigrationOrder'];
  download('dependency_metrics.csv','text/csv',rows.map(function(r){return r.map(function(v){return '"'+String(v).replace(/"/g,'""')+'"';}).join(',');}).join('\r\n'));
};
window.addEventListener('resize',function(){if(S.tab==='graph')fit();});
if(window.matchMedia){var mq=window.matchMedia('(prefers-color-scheme: dark)'),cb=function(){render();};if(mq.addEventListener)mq.addEventListener('change',cb);else if(mq.addListener)mq.addListener(cb);}

/* ---------- start ---------- */
$('title').textContent='Dependency Graph - '+D.meta.project;
$('subtitle').textContent=D.meta.types+' types \u00b7 '+D.meta.pairs+' type-to-type dependencies \u00b7 '+D.meta.cycles+' cycles \u00b7 '+(D.meta.maxLevel+1)+' dependency levels \u00b7 '+(D.meta.framework||'framework not recorded')+' \u00b7 IR schema '+(D.meta.schema||'?');
var bn=$('banner');bn.textContent=D.meta.banner;if(D.meta.warn)bn.classList.add('warn');
$('kinds').innerHTML=kindsAll.length?kindsAll.map(function(k){return '<label><input type="checkbox" data-kind="'+esc(k)+'" checked> '+esc(k)+'</label>';}).join(''):'<span class="sub">no edge kinds</span>';
$('kinds').addEventListener('change',function(ev){var k=ev.target.getAttribute('data-kind');if(k!=null){S.kinds[k]=ev.target.checked;render();}});
(function(){var s=$('depth'),opts=[1,2,3,4,5,99];opts.forEach(function(d){var o=document.createElement('option');o.value=String(d);o.textContent=d===99?'all':String(d);if(d===2)o.selected=true;s.appendChild(o);});})();
(function(){var mx=1;E.forEach(function(e){if(e.w>mx)mx=e.w;});var s=$('minw');s.max=String(Math.max(2,mx));})();
if(!E.length)$('kindsBox').style.display='none';
render();fit();showSide(null);legend();
window.__dep={D:D,S:S,setTab:setTab,select:select,render:render};
})();
</script>
</body>
</html>
"""


# ========================== DEPENDENCY MODEL ==========================
PRIMITIVE_LIBS = set("System." + x for x in (
    "Void Boolean String Int16 Int32 Int64 UInt16 UInt32 UInt64 Byte SByte Char Object Double Single Decimal DateTime TimeSpan Guid "
    "Array Type EventArgs EventHandler IntPtr Exception ValueType Enum Nullable Delegate").split())
PRIMITIVE_LIBS.update(["(unqualified)", "?", ""])
DBAPI_RE = re.compile(r"(ADODB\.|\bSqlConnection\b|\bSqlCommand\b|\bSqlDataAdapter\b|\bOleDbConnection\b|\bOleDbCommand\b|"
                      r"\bOdbcConnection\b|\bOracleConnection\b|\bMySqlConnection\b|\bDataAdapter\b|\bExecuteReader\b|"
                      r"\bExecuteNonQuery\b|\bExecuteScalar\b)")
CALL_KIND = "call (CallGraph)"


def tidx_of(p):
    m = re.match(r"Types\[(\d+)\]", p)
    return int(m.group(1)) if m else None


def tarjan(nodes, adj):
    """Iterative Tarjan. Components come out dependencies-first (an edge u->v puts comp(v) before comp(u))."""
    index, low, onstack, stack, comps = {}, {}, set(), [], []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        onstack.add(root)
        work = [(root, iter(adj[root]))]
        while work:
            v, it = work[-1]
            pushed = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    onstack.add(w)
                    work.append((w, iter(adj[w])))
                    pushed = True
                    break
                if w in onstack:
                    low[v] = min(low[v], index[w])
            if pushed:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    onstack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                comps.append(comp)
    return comps


def popcount(x):
    return bin(x).count("1")


def build_dep_model(ir, M, S):
    T = M["types"]
    N = len(T)
    idx_of = dict((x["id"], x["i"]) for x in T)
    tg = ir.get("TypeDependencyGraph") or {}
    cg = ir.get("CallGraph") or {}
    A = ir.get("AnalysisStatus") or {}
    MET = ir.get("Metrics") or {}

    # ---- 1. edges between known types, with kind + evidence ----
    pair = {}
    selfref = Counter()
    kinds_all = Counter()
    ext = defaultdict(Counter)
    hidden = Counter()
    unknown_from = 0

    def add_pair(a, b, kind, n, e, src, member=None):
        p = pair.setdefault((a, b), {"w": 0, "k": Counter(), "ev": [], "samples": []})
        p["w"] += 1
        p["k"][kind] += 1
        if len(p["ev"]) < EVIDENCE_PER_EDGE:
            p["ev"].append("%s[%d]" % (src, n))
            mem = member if member is not None else gi(e, "Member", default="")
            p["samples"].append([kind, str(mem or ""), str(gi(e, "SourceFile", default="") or ""), gi(e, "Line", default="")])

    for n, e in enumerate(tg.get("Edges") or []):
        frm, to = gi(e, "From"), gi(e, "To")
        kind = str(gi(e, "Kind", default="reference"))
        kinds_all[kind] += 1
        if frm not in idx_of:
            unknown_from += 1
            continue
        a = idx_of[frm]
        if to in idx_of:
            if idx_of[to] == a:
                selfref[a] += 1
            else:
                add_pair(a, idx_of[to], kind, n, e, "TypeDependencyGraph.Edges")
        elif isinstance(to, str) and to.startswith("unresolved-type:"):
            v = vendor_of(gi(e, "TargetDisplay", default=to[len("unresolved-type:"):]))
            if HIDE_PRIMITIVE_LIBS and v in PRIMITIVE_LIBS:
                hidden[v] += 1
            else:
                ext[a][v] += 1
    if INCLUDE_CALLGRAPH:
        for n, e in enumerate(cg.get("Edges") or []):
            frm, to = gi(e, "From"), gi(e, "To")
            ft = method_type(frm) if isinstance(frm, str) else None
            tt = method_type(to) if isinstance(to, str) and to.startswith("M:") else None
            if ft in idx_of and tt in idx_of:
                kinds_all[CALL_KIND] += 1
                if ft != tt:
                    add_pair(idx_of[ft], idx_of[tt], CALL_KIND, n, e, "CallGraph.Edges", member=method_name_from_id(to))

    adj = dict((i, []) for i in range(N))
    radj = dict((i, []) for i in range(N))
    for (a, b) in pair:
        adj[a].append(b)
        radj[b].append(a)
    for i in range(N):
        adj[i].sort()
        radj[i].sort()

    # ---- 2. cycles, levels, transitive reach ----
    comps = tarjan(list(range(N)), adj)
    comp_of = {}
    for ci, c in enumerate(comps):
        for v in c:
            comp_of[v] = ci
    succ = defaultdict(set)
    pred = defaultdict(set)
    for (a, b) in pair:
        if comp_of[a] != comp_of[b]:
            succ[comp_of[a]].add(comp_of[b])
            pred[comp_of[b]].add(comp_of[a])
    level = {}
    reach = {}
    for ci, c in enumerate(comps):          # dependencies first
        level[ci] = 0 if not succ[ci] else 1 + max(level[s] for s in succ[ci])
        r = 0
        for v in c:
            r |= 1 << v
        for s in succ[ci]:
            r |= reach[s]
        reach[ci] = r
    rreach = {}
    for ci in range(len(comps) - 1, -1, -1):  # dependents first
        r = 0
        for v in comps[ci]:
            r |= 1 << v
        for p in pred[ci]:
            r |= rreach[p]
        rreach[ci] = r
    cyc_list = [sorted(c) for c in comps if len(c) > 1]
    cyc_list.sort(key=lambda c: (-len(c), c[0]))
    cyc_id = {}
    for k, c in enumerate(cyc_list):
        for v in c:
            cyc_id[v] = k

    # ---- 3. layout order inside levels (barycenter sweeps, deterministic) ----
    lvl = dict((v, level[comp_of[v]]) for v in range(N))
    maxlvl = max(lvl.values()) if lvl else 0
    rows = defaultdict(list)
    for v in range(N):
        rows[lvl[v]].append(v)
    for L in rows:
        rows[L].sort(key=lambda v: T[v]["name"].lower())
    nbrs = dict((v, sorted(set(adj[v]) | set(radj[v]))) for v in range(N))
    posn = {}

    def renorm():
        for L, lst in rows.items():
            for k, v in enumerate(lst):
                posn[v] = (k + 0.5) / max(1, len(lst))
    renorm()
    for _ in range(6):
        for L in sorted(rows, reverse=True) + sorted(rows):
            def bary(v):
                ns = nbrs[v]
                return sum(posn[u] for u in ns) / len(ns) if ns else posn[v]
            rows[L].sort(key=lambda v: (bary(v), T[v]["name"].lower()))
            renorm()

    # ---- 4. roles, external deps, per-type facts ----
    sql_count, has_db = Counter(), set()
    for p, _ in S["sql"]:
        ti = tidx_of(p)
        if ti is not None:
            sql_count[ti] += 1
    for p, s in S["code_strings"]:
        ti = tidx_of(p)
        if ti is not None and DBAPI_RE.search(s):
            has_db.add(ti)
    names = dict((x["i"], x["name"]) for x in T)
    ext_tot = Counter()
    for a, c in ext.items():
        for v, n in c.items():
            ext_tot[v] += n
    keep_ext = [v for v, _ in ext_tot.most_common(25)]

    # migration order: IR's if usable, else computed (dependencies first)
    ir_order = [idx_of[x] for x in (tg.get("SuggestedMigrationOrder") or []) if isinstance(x, str) and x in idx_of]
    ir_order_raw = len(tg.get("SuggestedMigrationOrder") or [])
    computed = sorted(range(N), key=lambda v: (lvl[v], -len(radj[v]), T[v]["name"].lower()))
    if ir_order:
        seen = set(ir_order)
        order = list(dict.fromkeys(ir_order)) + [v for v in computed if v not in seen]
        order_src = "IR TypeDependencyGraph.SuggestedMigrationOrder (%d of %d entries match a known type%s)" % (
            len(ir_order), ir_order_raw, "; %d types not listed by the IR were appended by dependency level" % (N - len(seen)) if N > len(seen) else "")
    else:
        order = computed
        order_src = "computed by this script (dependency level, then number of dependents); the IR gives no usable SuggestedMigrationOrder"
    opos = dict((v, k + 1) for k, v in enumerate(order))

    types_out = []
    for x in T:
        i = x["i"]
        fo, fi = len(adj[i]), len(radj[i])
        if x["is_form"]:
            role = "form"
        elif ".My." in x["full"] or (x["full"].split(".")[-1].startswith("My") and x["full"].rsplit(".", 1)[0].endswith("My")):
            role = "generated"
        elif sql_count.get(i) or i in has_db:
            role = "data"
        else:
            role = "other"
        types_out.append({
            "i": i, "name": x["name"], "full": x["full"], "kind": x["kind"], "role": role, "loc": x["loc"], "methods": len(x["methods"]),
            "fanout": fo, "fanin": fi, "wout": sum(pair[(i, b)]["w"] for b in adj[i]), "win": sum(pair[(a, i)]["w"] for a in radj[i]),
            "tout": popcount(reach[comp_of[i]]) - 1, "tin": popcount(rreach[comp_of[i]]) - 1, "level": lvl[i],
            "cyc": cyc_id.get(i, -1), "instab": (round(fo / float(fo + fi), 2) if (fo + fi) else None),
            "ext": [[v, n] for v, n in ext[i].most_common(6)], "extTotal": sum(ext[i].values()), "order": opos[i], "self": selfref.get(i, 0),
            "files": x["src"], "src": "Types[%d]" % i, "row": rows[lvl[i]].index(i)})
    edges_out = []
    for (a, b), p in sorted(pair.items()):
        edges_out.append({"s": a, "t": b, "w": p["w"], "k": dict(p["k"]), "ev": p["ev"], "samples": p["samples"],
                          "cyc": bool(a in cyc_id and b in cyc_id and cyc_id[a] == cyc_id[b])})
    ext_nodes = [{"id": "x:" + v, "label": v, "total": ext_tot[v], "types": sum(1 for a in ext if v in ext[a])} for v in keep_ext]
    x_edges = [{"s": a, "t": "x:" + v, "w": n} for a in ext for v, n in ext[a].items() if v in keep_ext]

    ir_scc = tg.get("StronglyConnectedComponents") or []
    unresolved, internal = MET.get("UnresolvedEdges"), MET.get("InternalEdges")
    if internal is None:
        internal = sum(1 for e in (tg.get("Edges") or []) if gi(e, "To") in idx_of)
    if unresolved is None:
        unresolved = sum(1 for e in (tg.get("Edges") or []) if isinstance(gi(e, "To"), str) and gi(e, "To").startswith("unresolved"))
    warn = (A.get("Authoritative") is False) or bool(A.get("CompilationErrors"))
    banner = ("Authoritative=%s; compilation errors=%s; internal edges=%s vs unresolved edges=%s. %s" % (
        A.get("Authoritative"), A.get("CompilationErrors"), internal, unresolved,
        "Dependencies between types are probably UNDER-counted: unresolved links are not dependencies between known types, so cycles and impact lists may be incomplete."
        if warn else "No analysis warning recorded.")) if (A or MET) else "AnalysisStatus/Metrics not present in the JSON: confidence unknown."
    return {
        "meta": {"project": ir.get("ProjectName") or "VB.NET project", "schema": ir.get("SchemaVersion"), "generated": ir.get("GeneratedAtUtc"),
                 "framework": ir.get("TargetFramework"), "banner": banner, "warn": warn, "types": N, "maxLevel": maxlvl,
                 "edgeKinds": dict(kinds_all), "order_src": order_src, "ir_scc": len(ir_scc), "cycles": len(cyc_list),
                 "cyc_types": sum(len(c) for c in cyc_list), "hidden_libs": dict(hidden), "unknown_from": unknown_from,
                 "isolated": sum(1 for t in types_out if not t["fanin"] and not t["fanout"]), "pairs": len(pair), "callgraph": INCLUDE_CALLGRAPH,
                 "self_refs": sum(selfref.values())},
        "types": types_out, "edges": edges_out, "cycles": [{"id": k, "members": c} for k, c in enumerate(cyc_list)],
        "ext": ext_nodes, "xEdges": x_edges, "order": order, "levels": dict((str(L), len(v)) for L, v in sorted(rows.items())),
    }


# ============================== MARKDOWN ==============================
def ml(s):
    return oneline(re.sub(r'["\[\]{}|<>#;`]', " ", str(s).replace("&", "and")))


def to_markdown(model):
    meta, types, edges = model["meta"], model["types"], model["edges"]
    tn = dict((t["i"], t) for t in types)
    # ---- diagram ----
    ranked = sorted(types, key=lambda t: -(t["fanin"] + t["fanout"]))
    connected = [t for t in ranked if t["fanin"] + t["fanout"] > 0]
    shown = set(t["i"] for t in connected[:MD_MAX_TYPES])
    e_show = sorted([e for e in edges if e["s"] in shown and e["t"] in shown], key=lambda z: -z["w"])[:MD_MAX_EDGES]
    lines = ["flowchart TB"]
    bylevel = defaultdict(list)
    for i in shown:
        bylevel[tn[i]["level"]].append(i)
    for L in sorted(bylevel, reverse=True):
        lines.append('  subgraph LV%d["Level %d%s"]' % (L, L, " - foundation (no internal dependencies)" if L == 0 else ""))
        for i in sorted(bylevel[L], key=lambda v: tn[v]["row"]):
            cls = ":::cyc" if tn[i]["cyc"] >= 0 else (":::leaf" if L == 0 else "")
            lines.append('    t%d["%s"]%s' % (i, ml(trunc(tn[i]["name"], 30)), cls))
        lines.append("  end")
    cyc_idx = []
    for k, e in enumerate(e_show):
        lines.append('  t%d -->|"%d"| t%d' % (e["s"], e["w"], e["t"]))
        if e["cyc"]:
            cyc_idx.append(str(k))
    lines.append("  classDef cyc fill:#fee2e2,stroke:#e11d48,color:#111")
    lines.append("  classDef leaf fill:#d1fae5,stroke:#10b981,color:#111")
    if cyc_idx:
        lines.append("  linkStyle %s stroke:#e11d48,stroke-width:2px" % ",".join(cyc_idx))
    mm = "```mermaid\n" + "\n".join(lines) + "\n```" if shown else "No dependencies between known types were found."
    omitted = len(connected) - len(shown)

    # ---- tables ----
    kinds = md_table(["Edge kind", "Count in the IR"], sorted(meta["edgeKinds"].items(), key=lambda kv: -kv[1])) if meta["edgeKinds"] else "No edges in the JSON."
    summary = md_table(["Fact", "Value"], [
        ["Types", meta["types"]], ["Distinct type-to-type dependencies", meta["pairs"]],
        ["Dependency levels (0 = foundation)", meta["maxLevel"] + 1], ["Cycles (groups of types that depend on each other)", meta["cycles"]],
        ["Types inside cycles", meta["cyc_types"]], ["Types with no internal dependency links at all", meta["isolated"]],
        ["Cycles reported by the IR itself (StronglyConnectedComponents)", meta["ir_scc"]],
        ["Call-graph edges rolled up", "yes" if meta["callgraph"] else "no"]])
    top_in = [[t["name"], t["fanin"], t["tin"], t["level"], t["src"]] for t in sorted(types, key=lambda t: (-t["fanin"], t["name"]))[:10] if t["fanin"]]
    top_out = [[t["name"], t["fanout"], t["tout"], t["level"], t["src"]] for t in sorted(types, key=lambda t: (-t["fanout"], t["name"]))[:10] if t["fanout"]]
    cyc_rows = []
    for c in model["cycles"]:
        mem = set(c["members"])
        inner = sum(1 for e in edges if e["s"] in mem and e["t"] in mem)
        cyc_rows.append([c["id"] + 1, len(mem), ", ".join(tn[i]["name"] for i in c["members"][:12]) + (" ..." if len(mem) > 12 else ""), inner])
    mig = [[k + 1, tn[i]["name"], tn[i]["level"], tn[i]["fanout"], tn[i]["fanin"], "yes" if tn[i]["cyc"] >= 0 else "-", tn[i]["src"]]
           for k, i in enumerate(model["order"][:60])]
    iso = [t["name"] for t in types if not t["fanin"] and not t["fanout"]]
    ext_rows = [[t["name"], t["extTotal"], ", ".join("%s x%d" % tuple(p) for p in t["ext"][:4])] for t in sorted(types, key=lambda t: -t["extTotal"])[:10] if t["extTotal"]]
    hid = sum(meta["hidden_libs"].values())
    oq = ["Dependencies are only those the analysis could resolve; with the confidence warning above, treat 'no dependency' as 'none found'.",
          "Dependency level and cycles are computed by this script from the edges (Assumption: edge direction = From depends on To).",
          "The suggested migration order is a starting point only: it ignores runtime, database and UI coupling that is not in the type graph."]
    if meta["ir_scc"] != meta["cycles"]:
        oq.append("The IR lists %d strongly connected components but this script found %d cycles; ask the owner of the analysis tool how it defines them." % (meta["ir_scc"], meta["cycles"]))
    if meta["unknown_from"]:
        oq.append("%d TypeDependencyGraph edges start at a type id that is not in Types[] and were ignored." % meta["unknown_from"])
    out = ["# Dependency Graph - %s" % meta["project"],
           "_Generated %s from the IR JSON (schema %s, IR generated %s). Interactive version with impact analysis: `%s`._" % (
               datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), meta["schema"], meta["generated"] or "time not recorded", OUTPUT_HTML),
           ("> **Confidence warning.** " if meta["warn"] else "> **Confidence.** ") + meta["banner"],
           "## 1. Summary\n\n" + summary,
           "## 2. Dependency diagram\n\nArrows point from a type to a type it depends on; the number is how many references/calls the IR records. "
           "Levels: a type sits one level above the highest level it depends on, so level 0 can be migrated first. Red = in a cycle, green = foundation.\n\n" + mm +
           ("\n\n%d more connected types are not drawn (diagram limit %d); the HTML shows all of them." % (omitted, MD_MAX_TYPES) if omitted > 0 else ""),
           "## 3. Cycles\n\n" + (md_table(["Cycle", "Types", "Members", "Links inside"], cyc_rows) if cyc_rows else "No cycles between known types were found."),
           "## 4. Suggested migration order\n\nSource: " + meta["order_src"] + "\n\n" +
           (md_table(["#", "Type", "Level", "Depends on", "Used by", "In cycle", "JSON"], mig) if mig else "none") +
           ("\n\nFirst 60 of %d types shown." % len(model["order"]) if len(model["order"]) > 60 else ""),
           "## 5. Coupling hot-spots\n\n### Most depended-upon (risky to change)\n" + (md_table(["Type", "Direct dependents", "All dependents", "Level", "JSON"], top_in) if top_in else "none") +
           "\n\n### Depend on the most types\n" + (md_table(["Type", "Direct dependencies", "All dependencies", "Level", "JSON"], top_out) if top_out else "none"),
           "## 6. Types with no internal dependency links\n\n" + (", ".join(iso) if iso else "none"),
           "## 7. External (unresolved) dependencies per type\n\n" + (md_table(["Type", "References", "Top libraries"], ext_rows) if ext_rows else "none") +
           ("\n\n%d references to .NET primitive types (System.Void, Boolean, ...) are not counted." % hid if hid else ""),
           "## 8. Edge kinds in the IR\n\n" + kinds,
           "## 9. Assumptions and open questions\n\n" + "\n".join("- " + q for q in oq)]
    return "\n\n".join(out) + "\n"


def to_html(model):
    data = json.dumps(model, ensure_ascii=False, default=str).replace("</", "<\\/")
    return (HTML_TEMPLATE.replace("/*__TITLE__*/", re.sub(r"[<>&]", "", "Dependency Graph - %s" % model["meta"]["project"]))
            .replace("/*__DATA__*/", data))


def main():
    if "<PATH" in IR_JSON_PATH or "<PATH" in OUTPUT_DIR:
        sys.exit("Set IR_JSON_PATH and OUTPUT_DIR at the top of the script.")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log("loading %s" % IR_JSON_PATH)
    with open(IR_JSON_PATH, "r", encoding="utf-8-sig") as f:
        ir = json.load(f)
    log("extracting facts")
    M = build_model(ir)
    S = scan_ir(ir, M)
    model = build_dep_model(ir, M, S)
    m = model["meta"]
    log("types=%d dependency pairs=%d cycles=%d levels=%d isolated=%d" % (m["types"], m["pairs"], m["cycles"], m["maxLevel"] + 1, m["isolated"]))
    md_path, html_path = os.path.join(OUTPUT_DIR, OUTPUT_MD), os.path.join(OUTPUT_DIR, OUTPUT_HTML)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(to_markdown(model))
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(to_html(model))
    log("DONE -> %s" % md_path)
    log("DONE -> %s" % html_path)


if __name__ == "__main__":
    main()
