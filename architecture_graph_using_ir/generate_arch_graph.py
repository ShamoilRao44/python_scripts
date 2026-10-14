#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_arch_graph.py  -  VB.NET IR JSON  ->  architecture_graph.md  +  architecture_graph.html

Single file, Python 3.8 compatible, only dependency: requests (and only if USE_LLM = True).
No Graphviz, no JavaScript libraries and no internet are needed to VIEW the result.

WHAT IT PRODUCES
  architecture_graph.md    Mermaid diagrams (overview + per-layer detail) and a few short tables.
                           Renders in GitHub / GitLab / Azure DevOps / VS Code with a Mermaid extension.
  architecture_graph.html  ONE self-contained interactive page (open it in any browser):
                           - Layers view and Types view, zoom / pan, search
                           - hover = highlight a node's links, click = details panel with JSON sources
                           - slider to hide weak links, download as SVG

HOW THE GRAPH IS BUILT (all deterministic, the LLM is optional and OFF by default)
  1. Facts come from the IR JSON: Types, Files, TypeDependencyGraph.Edges, CallGraph.Edges,
     unresolved-type targets, ExternalDependencies, ConfigurationArtifacts, SQL / DB-API use in code.
  2. Every type is placed in a layer (Presentation / Business-shared / Data access / Generated).
     The basis of each placement is stored and shown; inferred placement is an Assumption.
  3. Type edges are rolled up to group edges; external libraries are merged by vendor and
     .NET primitives (System.Void, Boolean ...) are hidden as noise.
  4. Confidence from AnalysisStatus (Authoritative, compile errors, unresolved vs internal edges)
     is shown in a banner, because an unresolved-heavy analysis makes the graph incomplete.

INPUT  : the IR JSON only. Set the two paths below.
OUTPUT : OUTPUT_DIR/architecture_graph.md and OUTPUT_DIR/architecture_graph.html
"""
import os
import re
import sys
import json
import time
import hashlib
import datetime
from collections import Counter, defaultdict

try:
    import requests
except ImportError:  # only needed when USE_LLM = True
    requests = None

# =============================== CONFIG ===============================
IR_JSON_PATH = r"<PATH_TO_IR_JSON_FILE>"       # e.g. r"/mnt/MyDocuments/.../project.ir.json"
OUTPUT_DIR = r"<PATH_TO_OUTPUT_DIRECTORY>"     # e.g. r"/mnt/MyDocuments/.../graph_output"
IR_BASE_DIR = ""        # optional: folder that contains files/ and types/ (per-item IR). "" = not used
OUTPUT_MD = "architecture_graph.md"
OUTPUT_HTML = "architecture_graph.html"

# --- Grouping ---
GROUP_BY = "layer_namespace"   # "layer" = one box per layer; "layer_namespace" = split a layer by namespace when it has several
GROUP_RULES = []               # optional manual rules, first match wins: [(regex on type full name, "Group label", "Layer name"), ...]
#   example: GROUP_RULES = [(r"\.Reports?\.", "Reporting", "Business / shared logic")]
HIDE_PRIMITIVE_LIBS = True     # hide System.Void / Boolean / String ... as external-library noise

# --- Markdown size limits (Mermaid renderers refuse very large diagrams) ---
MD_MAX_TYPES_PER_LAYER = 25
MD_MAX_TYPE_EDGES = 60
MD_MAX_EXTERNALS = 10

# --- Optional LLM: one small call that writes a one-sentence role per group (labelled Assumption) ---
USE_LLM = False
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://172.31.8.100:9005/v1/chat/completions")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3-coder-next-awq")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "<PUT_API_KEY_HERE>")   # prefer env var over hard-coding
LLM_TIMEOUT = 1500
LLM_TEMPERATURE = 0.1
LLM_MAX_TOKENS = None
LLM_RETRIES = 3
MAX_PROMPT_CHARS = 90000
MAX_TYPE_SUMMARY_IN_SECTION = 900   # unused here, kept for shared code
DRY_RUN = os.environ.get("ARCH_DRY_RUN") == "1"   # 1 = no LLM calls (stub text)

SYSTEM_PROMPT = (
    "You help document the architecture of a legacy VB.NET project.\n"
    "STRICT RULES:\n"
    "1. Use ONLY the material in the user message. Never invent facts.\n"
    "2. Every sentence you write starts with the word 'Assumption:' because it is inferred from names only.\n"
    "3. Follow the output format requested exactly.\n"
    "4. End your answer with these two blocks, exactly as shown:\n"
    "@@ASSUMPTIONS@@\n- none\n@@OPEN_QUESTIONS@@\n- none\n"
)

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


# ============================== LLM ===================================
CACHE_DIR = None
LLM_ERRORS = []


def call_llm(prompt, system, tag):
    if DRY_RUN:
        return "[DRY RUN stub for %s]\nPURPOSE: stub\n@@ASSUMPTIONS@@\n- stub assumption\n@@OPEN_QUESTIONS@@\n- stub question" % tag
    if len(prompt) > MAX_PROMPT_CHARS:
        half = MAX_PROMPT_CHARS // 2
        log("WARNING %s: prompt %d chars > limit, middle trimmed" % (tag, len(prompt)))
        prompt = prompt[:half] + "\n...[TRIMMED FOR LENGTH]...\n" + prompt[-half:]
    key = hashlib.sha256((LLM_MODEL + "\x00" + system + "\x00" + prompt).encode("utf-8")).hexdigest()
    cpath = os.path.join(CACHE_DIR, key + ".json")
    if os.path.exists(cpath):
        with open(cpath, "r", encoding="utf-8") as f:
            return json.load(f)["response"]
    body = {
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "stream": False,
        "temperature": LLM_TEMPERATURE,
        "enable_thinking": False,
    }
    if LLM_MAX_TOKENS:
        body["max_tokens"] = LLM_MAX_TOKENS
    headers = {"x-api-key": LLM_API_KEY, "Content-Type": "application/json"}
    last_err = ""
    for attempt in range(1, LLM_RETRIES + 1):
        try:
            r = requests.post(LLM_BASE_URL, json=body, headers=headers, timeout=LLM_TIMEOUT)
            r.raise_for_status()
            result = r.json()
            if "choices" in result and len(result["choices"]) > 0:
                text = result["choices"][0]["message"]["content"] or ""
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
                with open(cpath, "w", encoding="utf-8") as f:
                    json.dump({"tag": tag, "response": text}, f)
                return text
            last_err = "Unexpected response format: " + trunc(json.dumps(result), 300)
        except Exception as e:  # network, HTTP, JSON
            last_err = "%s: %s" % (type(e).__name__, e)
        log("LLM retry %d/%d for %s (%s)" % (attempt, LLM_RETRIES, tag, trunc(last_err, 160)))
        time.sleep(3 * attempt)
    LLM_ERRORS.append("%s: %s" % (tag, last_err))
    return "[LLM ERROR - section not generated: %s]" % trunc(last_err, 200)


ASSUMPTIONS = []   # (section, text)
OPEN_QUESTIONS = []


def split_markers(text):
    body = text
    parts = re.split(r"@@(ASSUMPTIONS|OPEN_QUESTIONS)@@", text)
    body = parts[0].strip()
    found = {"ASSUMPTIONS": [], "OPEN_QUESTIONS": []}
    for i in range(1, len(parts) - 1, 2):
        for line in parts[i + 1].splitlines():
            line = line.strip()
            if line[:1] in ("-", "*"):
                item = line.lstrip("-* ").strip()
                if item and item.lower() not in ("none", "none."):
                    found[parts[i]].append(item)
    return body, found["ASSUMPTIONS"], found["OPEN_QUESTIONS"]


def ask(section, tag, task, material):
    prompt = "TASK:\n%s\n\nMATERIAL (the only information you may use):\n%s" % (task, material)
    raw = call_llm(prompt, SYSTEM_PROMPT, tag)
    body, a, q = split_markers(raw)
    for x in a:
        ASSUMPTIONS.append((section, x))
    for x in q:
        OPEN_QUESTIONS.append((section, x))
    return body


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
:root{--bg:#ffffff;--panel:#f6f7f9;--text:#1c2330;--sub:#5b6578;--line:#d5d9e2;--accent:#2563eb;--warn-bg:#fff4e0;--warn-tx:#7a4a00;--ok-bg:#eaf3ff;--ok-tx:#1d4570}
@media (prefers-color-scheme: dark){:root{--bg:#16181d;--panel:#1e2128;--text:#e6e8ee;--sub:#9aa3b5;--line:#343a46;--accent:#60a5fa;--warn-bg:#3a2e14;--warn-tx:#f2cf8a;--ok-bg:#1c2a3d;--ok-tx:#a9c8ee}}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--text);display:flex;flex-direction:column}
header{padding:10px 16px;border-bottom:1px solid var(--line)}
h1{font-size:17px;margin:0 0 2px}
.sub{color:var(--sub);font-size:12.5px}
.banner{margin-top:8px;padding:7px 10px;border-radius:6px;font-size:12.5px;background:var(--ok-bg);color:var(--ok-tx)}
.banner.warn{background:var(--warn-bg);color:var(--warn-tx)}
.toolbar{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;padding:8px 16px;border-bottom:1px solid var(--line)}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.seg button{border:0;background:transparent;color:var(--text);padding:5px 12px;cursor:pointer;font:inherit}
.seg button.on{background:var(--accent);color:#fff}
button.tool,input[type=search]{border:1px solid var(--line);background:var(--bg);color:var(--text);border-radius:6px;padding:5px 10px;font:inherit}
button.tool{cursor:pointer}
input[type=search]{min-width:170px}
label.sl{display:flex;align-items:center;gap:6px;color:var(--sub);font-size:12.5px}
.main{flex:1;min-height:0;display:flex}
#stage{flex:1;min-width:0;position:relative;background:var(--bg)}
#svg{width:100%;height:100%;display:block;cursor:grab;touch-action:none}
#svg.drag{cursor:grabbing}
#panel{width:340px;max-width:42vw;border-left:1px solid var(--line);background:var(--panel);padding:12px 14px;overflow:auto;font-size:13px}
#panel h2{font-size:15px;margin:0 0 4px}
#panel table{width:100%;border-collapse:collapse;margin:6px 0 10px}
#panel td,#panel th{padding:3px 4px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
#panel th{color:var(--sub);font-weight:600;font-size:12px}
#panel .tag{display:inline-block;font-size:11.5px;padding:1px 7px;border-radius:10px;border:1px solid var(--line);color:var(--sub);margin:0 4px 4px 0}
#panel .src{color:var(--sub);font:12px ui-monospace,Consolas,monospace}
#panel a.lnk{color:var(--accent);cursor:pointer;text-decoration:underline}
#panel button{margin-top:6px}
#tip{position:fixed;pointer-events:none;background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:6px 9px;font-size:12.5px;max-width:320px;box-shadow:0 4px 14px rgba(0,0,0,.18);display:none;z-index:5}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;padding:7px 16px;border-top:1px solid var(--line);color:var(--sub);font-size:12px}
.legend i{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:5px;vertical-align:-2px;border:2px solid}
.legend i.d{border-style:dashed}
@media (max-width:760px){.main{flex-direction:column}#panel{width:100%;max-width:none;max-height:38vh;border-left:0;border-top:1px solid var(--line)}}
</style>
</head>
<body>
<header>
  <h1 id="title"></h1>
  <div class="sub" id="subtitle"></div>
  <div class="banner" id="banner"></div>
</header>
<div class="toolbar">
  <div class="seg" role="group" aria-label="View"><button id="vGroup" class="on">Layers</button><button id="vType">Types</button></div>
  <input type="search" id="q" placeholder="Search types, libraries..." aria-label="Search">
  <label class="sl">Hide links weaker than <input type="range" id="minw" min="1" max="10" value="1"> <b id="minwv">1</b></label>
  <span class="sub" id="shown"></span>
  <button class="tool" id="zin" title="Zoom in">+</button><button class="tool" id="zout" title="Zoom out">&minus;</button>
  <button class="tool" id="fit">Fit</button><button class="tool" id="dl">Download SVG</button>
</div>
<div class="main">
  <div id="stage"><svg id="svg" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Architecture graph"></svg></div>
  <aside id="panel"><b>Details</b><p class="sub">Hover a box to highlight its links. Click a box for details and JSON sources. Scroll to zoom, drag the background to pan.</p></aside>
</div>
<div class="legend">
  <span><i style="border-color:#3b82f6;background:#3b82f633"></i>Presentation</span>
  <span><i style="border-color:#10b981;background:#10b98133"></i>Business / shared</span>
  <span><i style="border-color:#f59e0b;background:#f59e0b33"></i>Data access</span>
  <span><i style="border-color:#94a3b8;background:#94a3b833"></i>Generated (My.*)</span>
  <span><i style="border-color:#a855f7;background:#a855f733"></i>Database</span>
  <span><i class="d" style="border-color:#64748b"></i>External / unresolved library</span>
  <span>Line thickness = number of references + calls. Layer placement is inferred; see the basis in each details panel.</span>
</div>
<div id="tip"></div>
<script id="data" type="application/json">/*__DATA__*/</script>
<script>
(function(){
"use strict";
var D = JSON.parse(document.getElementById('data').textContent);
var NS = 'http://www.w3.org/2000/svg';
function $(id){return document.getElementById(id);}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function fmt(n){return (n||0).toLocaleString();}
function trunc(s,n){s=String(s);return s.length>n?s.slice(0,n-1)+'…':s;}
var svg=$('svg'), tip=$('tip'), panel=$('panel');
var view='group', minW=1, sel=null, hov=null, query='';
var vb={x:0,y:0,w:1400,h:900}, content={w:1400,h:900};
var nodes=[], edges=[], bands=[], byId={};
var userMinW={group:null,type:null};

var LCOL={pres:'#3b82f6',biz:'#10b981',data:'#f59e0b',gen:'#94a3b8',db:'#a855f7',cfg:'#0ea5e9',ext:'#64748b'};
var LKEY={'Presentation':'pres','Business / shared logic':'biz','Data access':'data','Generated / framework (My.*)':'gen'};
function dark(){return window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches;}
function pal(){var d=dark();return {bg:d?'#16181d':'#ffffff',text:d?'#e6e8ee':'#1c2330',sub:d?'#9aa3b5':'#5b6578',line:d?'#7d879b':'#8a93a6',accent:d?'#60a5fa':'#2563eb',band:d?'#ffffff':'#000000'};}
function el(tag,attrs,parent,text){var e=document.createElementNS(NS,tag);for(var k in attrs){e.setAttribute(k,attrs[k]);}if(text!=null)e.textContent=text;if(parent)parent.appendChild(e);return e;}

/* ---------------- indexes ---------------- */
var typeBy={}; D.types.forEach(function(t){typeBy[t.i]=t;});
var groupBy={}; D.groups.forEach(function(g){groupBy[g.id]=g;});
var extBy={}; D.ext.forEach(function(e){extBy[e.id]=e;});
function layerKey(l){return LKEY[l]||'biz';}
function nodeColor(n){return n.kind==='ext'?(n.ext.kind==='db'?LCOL.db:n.ext.kind==='cfg'?LCOL.cfg:LCOL.ext):LCOL[layerKey(n.layer)];}

/* ---------------- build nodes + layout for current view ---------------- */
function build(){
  nodes=[];edges=[];bands=[];byId={};
  var W=1400, y=60, rowH, cols;
  function put(n){nodes.push(n);byId[n.id]=n;}
  function rows(list,perRow,w,h,gapY,startY){var r=0,out=startY;for(var i=0;i<list.length;i+=perRow){var chunk=list.slice(i,i+perRow);chunk.forEach(function(n,k){n.x=(k+1)*W/(chunk.length+1);n.y=out+h/2;n.w=w;n.h=h;});out+=h+gapY;r++;}return out;}
  var extList=D.ext.slice();
  if(view==='group'){
    [0,1,2].forEach(function(t){
      var list=D.groups.filter(function(g){return g.tier===t;});
      if(!list.length)return;
      list.forEach(function(g){put({id:g.id,kind:'group',label:g.label,sub:g.types.length+' types · '+fmt(g.loc)+' LOC',layer:g.layer,ref:g});});
      y=rows(list.map(function(g){return byId[g.id];}),4,250,84,70,y);
    });
    extList.forEach(function(e){put({id:e.id,kind:'ext',label:e.label,sub:e.cat,ext:e});});
    y=rows(extList.map(function(e){return byId[e.id];}),5,210,54,36,y+10);
    var ge=D.groups.length?D.gEdges:[];
    ge.forEach(function(e){edges.push({s:e.s,t:e.t,w:e.w,refs:e.refs,calls:e.calls,pairs:e.pairs,k:'int'});});
    D.xGroupEdges.forEach(function(e){edges.push({s:e.s,t:e.t,w:e.w,k:'ext'});});
  } else {
    var cw=156, ch=34, gx=12, gy=10, per=Math.floor((W-60)/(cw+gx));
    [0,1,2].forEach(function(t){
      var gs=D.groups.filter(function(g){return g.tier===t;});
      if(!gs.length)return;
      var list=[];
      gs.forEach(function(g){g.types.slice().sort(function(a,b){return typeBy[a].name.localeCompare(typeBy[b].name);}).forEach(function(i){list.push(typeBy[i]);});});
      var nr=Math.ceil(list.length/per), bandH=34+nr*(ch+gy)+10;
      bands.push({y:y,h:bandH,label:gs.map(function(g){return g.label;}).join(' / '),layer:gs[0].layer,count:list.length});
      list.forEach(function(tp,k){
        var r=Math.floor(k/per), c=k%per;
        put({id:'t'+tp.i,kind:'type',label:tp.name,sub:tp.kind,layer:tp.layer,ref:tp,x:30+c*(cw+gx)+cw/2,y:y+34+r*(ch+gy)+ch/2,w:cw,h:ch});
      });
      y+=bandH+34;
    });
    extList.forEach(function(e){put({id:e.id,kind:'ext',label:e.label,sub:e.cat,ext:e});});
    y=rows(extList.map(function(e){return byId[e.id];}),6,200,44,24,y);
    D.tEdges.forEach(function(e){edges.push({s:'t'+e.s,t:'t'+e.t,w:e.w,refs:e.refs,calls:e.calls,k:'int'});});
    D.xTypeEdges.forEach(function(e){edges.push({s:'t'+e.s,t:e.t,w:e.w,k:'ext'});});
  }
  edges=edges.filter(function(e){return byId[e.s]&&byId[e.t];});
  content={w:W,h:y+20};
}
function maxW(){var m=1;edges.forEach(function(e){if(e.w>m)m=e.w;});return m;}
function defaultMinW(){
  if(view==='group')return 1;
  var ws=edges.filter(function(e){return e.k==='int';}).map(function(e){return e.w;});
  var mx=1;ws.forEach(function(w){if(w>mx)mx=w;});
  for(var m=1;m<=mx;m++){var c=0;ws.forEach(function(w){if(w>=m)c++;});if(c<=90)return m;}
  return mx;
}

/* ---------------- drawing ---------------- */
function edgePath(a,b,e){
  var x1=e._sx,x2=e._tx,y1,y2,dy,c1x,c1y,c2x,c2y;
  y1=a.y+(e._ss==='b'?a.h/2:-a.h/2); y2=b.y+(e._es==='b'?b.h/2:-b.h/2);
  if(Math.abs(a.y-b.y)<2){
    dy=Math.min(90,26+Math.abs(a.x-b.x)*0.12);
    c1x=x1;c1y=y1-dy;c2x=x2;c2y=y2-dy;
  } else {
    var down=b.y>a.y; dy=Math.max(28,Math.abs(y2-y1)*0.45)*(down?1:-1);
    var bx=0;
    if(view==='group'&&Math.abs(y2-y1)>(a.h+b.h)/2+90){bx=(((x1+x2)/2)>=content.w/2?1:-1)*170;}
    c1x=x1+bx;c1y=y1+dy;c2x=x2+bx;c2y=y2-dy;
  }
  return {d:'M'+x1+','+y1+' C'+c1x+','+c1y+' '+c2x+','+c2y+' '+x2+','+y2,mx:(x1+3*c1x+3*c2x+x2)/8,my:(y1+3*c1y+3*c2y+y2)/8};
}
function spreadAnchors(list){
  var anchors={};
  function add(id,side,rec){var k=id+'~~'+side;(anchors[k]=anchors[k]||[]).push(rec);}
  list.forEach(function(e){
    var a=byId[e.s],b=byId[e.t],same=Math.abs(a.y-b.y)<2,down=b.y>a.y;
    e._ss=same?'t':(down?'b':'t'); e._es=same?'t':(down?'t':'b');
    add(e.s,e._ss,{e:e,k:'s',ox:b.x}); add(e.t,e._es,{e:e,k:'t',ox:a.x});
  });
  Object.keys(anchors).forEach(function(k){
    var l=anchors[k]; l.sort(function(p,q){return p.ox-q.ox;});
    var n=byId[k.split('~~')[0]], span=Math.min(n.w*0.8,22*(l.length-1));
    l.forEach(function(r,i){var off=l.length>1?(i/(l.length-1)-0.5)*span:0; if(r.k==='s')r.e._sx=n.x+off; else r.e._tx=n.x+off;});
  });
}
function render(){
  var P=pal();
  while(svg.firstChild)svg.removeChild(svg.firstChild);
  var defs=el('defs',{},svg);
  [['arr',P.line],['arrHi',P.accent]].forEach(function(m){
    var mk=el('marker',{id:m[0],viewBox:'0 0 10 10',refX:'9',refY:'5',markerUnits:'userSpaceOnUse',markerWidth:'10',markerHeight:'10',orient:'auto'},defs);
    el('path',{d:'M0,0 L10,5 L0,10 z',fill:m[1]},mk);
  });
  el('rect',{x:-2000,y:-2000,width:content.w+4000,height:content.h+4000,fill:P.bg,id:'bgrect'},svg);
  var root=el('g',{id:'root'},svg);
  bands.forEach(function(b){
    var c=LCOL[layerKey(b.layer)];
    el('rect',{x:10,y:b.y,width:content.w-20,height:b.h,rx:10,fill:c,'fill-opacity':'0.07',stroke:c,'stroke-opacity':'0.5'},root);
  });
  var mw=maxW();
  var shown=0, pairKey={};
  edges.forEach(function(e){pairKey[e.s+'>'+e.t]=1;});
  var eg=el('g',{id:'edges'},root), edgeEls=[];
  var vis=edges.filter(function(e){return e.w>=minW;}).sort(function(a,b){return b.w-a.w;});
  if(vis.length>600)vis=vis.slice(0,600);
  spreadAnchors(vis);
  vis.forEach(function(e){
    var a=byId[e.s],b=byId[e.t];
    var p=edgePath(a,b,e);
    var sw=view==='group'?Math.max(1.2,Math.min(6,1+Math.log(e.w+1)/Math.log(2)*0.7)):Math.max(1,Math.min(3,1+Math.log(e.w+1)/Math.log(2)*0.45));
    var path=el('path',{d:p.d,fill:'none',stroke:P.line,'stroke-width':sw,'marker-end':'url(#arr)','data-s':e.s,'data-t':e.t,'stroke-dasharray':e.k==='ext'?'5 4':''},eg);
    var title=el('title',{},path,(a.label+' → '+b.label+': '+e.w+(e.k==='int'&&e.refs!=null?' ('+e.refs+' type refs, '+e.calls+' calls'+(e.pairs?', '+e.pairs+' type pairs':'')+')':' references')));
    edgeEls.push({el:path,e:e,mx:p.mx,my:p.my});
    shown++;
  });
  e_labels=el('g',{id:'elabels'},root);
  bands.forEach(function(b){
    el('text',{x:24,y:b.y+22,fill:P.sub,'font-size':'13','font-weight':'600','paint-order':'stroke',stroke:P.bg,'stroke-width':'4','stroke-opacity':'0.85'},root,trunc(b.label,70)+' ('+b.count+' types)');
  });
  var ng=el('g',{id:'nodes'},root), nodeEls={};
  nodes.forEach(function(n){
    var c=nodeColor(n);
    var g=el('g',{'class':'node','data-id':n.id,cursor:'pointer'},ng);
    var shape;
    if(n.kind==='ext'&&n.ext.kind==='db'){
      el('rect',{x:n.x-n.w/2,y:n.y-n.h/2,width:n.w,height:n.h,rx:n.h/2.4,fill:c,'fill-opacity':'0.16',stroke:c,'stroke-width':'2'},g);
    } else if(n.kind==='ext'){
      el('rect',{x:n.x-n.w/2,y:n.y-n.h/2,width:n.w,height:n.h,rx:n.h/2,fill:c,'fill-opacity':'0.10',stroke:c,'stroke-width':'1.6','stroke-dasharray':n.ext.kind==='lib'?'5 4':''},g);
    } else {
      el('rect',{x:n.x-n.w/2,y:n.y-n.h/2,width:n.w,height:n.h,rx:8,fill:c,'fill-opacity':n.kind==='group'?'0.16':'0.14',stroke:c,'stroke-width':n.kind==='group'?'2':'1.5'},g);
    }
    var big=(n.kind==='group');
    el('text',{x:n.x,y:n.y+(big?-6:(n.kind==='ext'?-3:4)),'text-anchor':'middle',fill:P.text,'font-size':big?'14':'12','font-weight':'600'},g,trunc(n.label,big?32:(n.kind==='ext'?30:22)));
    if(big||n.kind==='ext')el('text',{x:n.x,y:n.y+(big?14:13),'text-anchor':'middle',fill:P.sub,'font-size':'11'},g,trunc(n.sub,big?40:34));
    nodeEls[n.id]=g;
    g.addEventListener('mouseenter',function(ev){hov=n.id;applyFocus();showTip(ev,n);});
    g.addEventListener('mousemove',function(ev){moveTip(ev);});
    g.addEventListener('mouseleave',function(){hov=null;applyFocus();tip.style.display='none';});
    g.addEventListener('click',function(ev){ev.stopPropagation();sel=(sel===n.id)?null:n.id;applyFocus();showPanel(sel?n:null);});
  });
  render.edgeEls=edgeEls; render.nodeEls=nodeEls; render.P=P;
  $('shown').textContent=shown+' of '+edges.length+' links shown';
  applyFocus();
  setViewBox();
}
var e_labels=null;

/* ---------------- focus / search ---------------- */
function matches(n){
  if(!query)return true;
  var q=query.toLowerCase();
  if(n.kind==='group')return n.ref.types.some(function(i){return typeBy[i].name.toLowerCase().indexOf(q)>=0;})||n.label.toLowerCase().indexOf(q)>=0;
  return (n.label+' '+(n.ref?n.ref.full:'')).toLowerCase().indexOf(q)>=0;
}
function applyFocus(){
  var P=render.P; if(!P||!render.nodeEls)return;
  var f=sel||hov, nb={};
  if(f){nb[f]=1;render.edgeEls.forEach(function(o){if(o.e.s===f)nb[o.e.t]=1;if(o.e.t===f)nb[o.e.s]=1;});}
  var base=view==='group'?0.8:0.22;
  render.edgeEls.forEach(function(o){
    var inc=f&&(o.e.s===f||o.e.t===f);
    o.el.setAttribute('stroke',inc?P.accent:P.line);
    o.el.setAttribute('marker-end',inc?'url(#arrHi)':'url(#arr)');
    o.el.setAttribute('opacity',f?(inc?'1':'0.05'):String((view==='type'&&o.e.k==='ext')?0:base));
  });
  while(e_labels.firstChild)e_labels.removeChild(e_labels.firstChild);
  render.edgeEls.forEach(function(o){
    var inc=f&&(o.e.s===f||o.e.t===f);
    if(view==='group'||inc){
      var t=el('text',{x:o.mx,y:o.my-3,'text-anchor':'middle','font-size':'11',fill:inc?P.accent:P.sub,'font-weight':inc?'700':'400','paint-order':'stroke',stroke:P.bg,'stroke-width':'3',opacity:f?(inc?'1':'0.1'):'1'},e_labels,String(o.e.w));
    }
  });
  nodes.forEach(function(n){
    var g=render.nodeEls[n.id]; var r=g.firstChild;
    var dim=(f&&!nb[n.id])||(!f&&query&&!matches(n));
    g.setAttribute('opacity',dim?'0.22':'1');
    var hit=query&&matches(n);
    r.setAttribute('stroke-width',(n.id===sel)?'4':(hit?'3.5':(n.kind==='group'?'2':'1.5')));
    if(n.id===sel||hit)r.setAttribute('stroke',P.accent); else r.setAttribute('stroke',nodeColor(n));
  });
}

/* ---------------- tooltip + panel ---------------- */
function tipHtml(n){
  if(n.kind==='group'){var g=n.ref;return '<b>'+esc(g.label)+'</b><br>'+g.types.length+' types, '+g.forms+' forms, '+fmt(g.methods)+' methods, '+fmt(g.loc)+' LOC'+(g.desc?'<br><i>'+esc(g.desc)+'</i>':'')+'<br><span class="sub">Click for members and links</span>';}
  if(n.kind==='type'){var t=n.ref;return '<b>'+esc(t.full)+'</b><br>'+esc(t.layer)+' · '+esc(t.kind)+'<br>'+fmt(t.loc)+' LOC, '+t.methods+' methods, '+t.conds+' conditions<br>links out '+t.fanout+' / in '+t.fanin+'<br><span class="sub">'+esc(t.src)+'</span>';}
  var e=n.ext;return '<b>'+esc(e.label)+'</b><br>'+esc(e.cat)+'<br>weight '+e.total+'<br><span class="sub">source: '+esc(e.source)+'</span>';
}
function showTip(ev,n){tip.innerHTML=tipHtml(n);tip.style.display='block';moveTip(ev);}
function moveTip(ev){var x=ev.clientX+14,y=ev.clientY+14;var w=tip.offsetWidth,h=tip.offsetHeight;if(x+w>window.innerWidth-8)x=ev.clientX-w-14;if(y+h>window.innerHeight-8)y=ev.clientY-h-14;tip.style.left=x+'px';tip.style.top=y+'px';}
function nm(id){var n=byId[id];return n?n.label:id;}
function tbl(head,rows){if(!rows.length)return '<p class="sub">none</p>';return '<table><tr>'+head.map(function(h){return '<th>'+esc(h)+'</th>';}).join('')+'</tr>'+rows.map(function(r){return '<tr>'+r.map(function(c){return '<td>'+c+'</td>';}).join('')+'</tr>';}).join('')+'</table>';}
function link(id,text){return '<a class="lnk" data-go="'+esc(id)+'">'+esc(text)+'</a>';}
function showPanel(n){
  if(!n){panel.innerHTML='<b>Details</b><p class="sub">Hover a box to highlight its links. Click a box for details and JSON sources.</p>';return;}
  var h='';
  if(n.kind==='group'){
    var g=n.ref;
    h+='<h2>'+esc(g.label)+'</h2><span class="tag">'+esc(g.layer)+'</span><span class="tag">layer placement inferred</span>';
    h+='<p>'+g.types.length+' types, '+g.forms+' forms, '+fmt(g.methods)+' methods, '+g.files+' files, '+fmt(g.loc)+' LOC. Links inside the group: '+g.intra+'.</p>';
    if(g.desc)h+='<p><i>'+esc(g.desc)+'</i></p>';
    h+='<b>Members</b>'+tbl(['Type','LOC','Links'],g.types.slice().sort(function(a,b){return (typeBy[b].fanin+typeBy[b].fanout)-(typeBy[a].fanin+typeBy[a].fanout);}).map(function(i){var t=typeBy[i];return [link('t'+i,t.name),fmt(t.loc),t.fanout+'/'+t.fanin];}));
    var o=D.gEdges.filter(function(e){return e.s===g.id;}), inn=D.gEdges.filter(function(e){return e.t===g.id;});
    h+='<b>Depends on</b>'+tbl(['Group','Refs','Calls'],o.sort(function(a,b){return b.w-a.w;}).map(function(e){return [esc(groupBy[e.t].label),e.refs,e.calls];}));
    h+='<b>Used by</b>'+tbl(['Group','Refs','Calls'],inn.sort(function(a,b){return b.w-a.w;}).map(function(e){return [esc(groupBy[e.s].label),e.refs,e.calls];}));
    var x=D.xGroupEdges.filter(function(e){return e.s===g.id;}).sort(function(a,b){return b.w-a.w;});
    h+='<b>External / data</b>'+tbl(['Name','Weight'],x.map(function(e){return [esc(extBy[e.t].label),e.w];}));
    h+='<p class="src">JSON: Types['+g.types.slice(0,8).join(', ')+(g.types.length>8?', …':'')+']</p>';
  } else if(n.kind==='type'){
    var t=n.ref;
    h+='<h2>'+esc(t.name)+'</h2><div class="src">'+esc(t.full)+'</div>';
    h+='<span class="tag">'+esc(t.kind)+'</span><span class="tag">'+esc(t.layer)+'</span>'+(t.form?'<span class="tag">form</span>':'')+(t.sql?'<span class="tag">SQL literals: '+t.sql+'</span>':'')+(t.db?'<span class="tag">DB API calls</span>':'');
    h+='<p><b>Layer basis:</b> '+esc(t.basis)+'</p>';
    h+='<p>'+fmt(t.loc)+' LOC, '+t.methods+' methods, '+t.conds+' conditions.<br>Files: '+esc(t.files.join(', ')||'n/a')+'</p>';
    h+='<b>Depends on</b>'+tbl(['Type','Weight'],t.out.map(function(p){return [esc(p[0]),p[1]];}));
    h+='<b>Used by</b>'+tbl(['Type','Weight'],t.in.map(function(p){return [esc(p[0]),p[1]];}));
    h+='<b>External / data</b>'+tbl(['Name','Weight'],t.ext.map(function(p){return [esc(p[0]),p[1]];}));
    h+='<p class="src">JSON: '+esc(t.src)+'</p>';
  } else {
    var e=n.ext, users={};
    D.xTypeEdges.forEach(function(x){if(x.t===e.id)users[x.s]=x.w;});
    var list=Object.keys(users).map(function(k){return [parseInt(k,10),users[k]];}).sort(function(a,b){return b[1]-a[1];});
    h+='<h2>'+esc(e.label)+'</h2><span class="tag">'+esc(e.cat)+'</span>';
    h+='<p>Total weight '+e.total+'. Source: <span class="src">'+esc(e.source)+'</span></p>';
    if(e.detail&&e.detail.length)h+='<b>Detail</b><ul>'+e.detail.map(function(d){return '<li>'+esc(d)+'</li>';}).join('')+'</ul>';
    h+='<b>Used by</b>'+tbl(['Type','Weight'],list.slice(0,25).map(function(p){return [link('t'+p[0],typeBy[p[0]].name),p[1]];}));
    if(e.kind==='db'&&Object.keys(D.crud).length){
      h+='<b>Tables (from SQL literals; C=insert R=select U=update D=delete)</b>'+tbl(['Table','Accessed by'],Object.keys(D.crud).sort().slice(0,40).map(function(t){var per=D.crud[t];return [esc(t),esc(Object.keys(per).map(function(k){return (typeBy[k]?typeBy[k].name:'?')+': '+per[k];}).join('; '))];}));
    }
  }
  panel.innerHTML=h;
}
panel.addEventListener('click',function(ev){
  var a=ev.target.closest&&ev.target.closest('a[data-go]'); if(!a)return;
  var id=a.getAttribute('data-go');
  if(id.charAt(0)==='t'&&view!=='type'){setView('type');}
  sel=id;applyFocus();if(byId[id]){showPanel(byId[id]);centerOn(byId[id]);}
});

/* ---------------- zoom / pan ---------------- */
function setViewBox(){svg.setAttribute('viewBox',vb.x+' '+vb.y+' '+vb.w+' '+vb.h);}
function fit(){
  var r=svg.getBoundingClientRect(); var ar=(r.width||1000)/(r.height||700);
  var w=content.w+20,h=content.h+20;
  if(w/h<ar){vb.h=h;vb.w=h*ar;vb.x=-(vb.w-content.w)/2;vb.y=-10;}else{vb.w=w;vb.h=w/ar;vb.x=-10;vb.y=-10;}
  setViewBox();
}
function zoomAt(f,cx,cy){
  var pt=svg.createSVGPoint();pt.x=cx;pt.y=cy;var m=svg.getScreenCTM();if(!m)return;var p=pt.matrixTransform(m.inverse());
  var nw=Math.max(150,Math.min(content.w*4,vb.w*f)),k=nw/vb.w;
  vb.x=p.x-(p.x-vb.x)*k;vb.y=p.y-(p.y-vb.y)*k;vb.w=nw;vb.h=vb.h*k;setViewBox();
}
function centerOn(n){vb.x=n.x-vb.w/2;vb.y=n.y-vb.h/2;setViewBox();}
svg.addEventListener('wheel',function(ev){ev.preventDefault();zoomAt(ev.deltaY<0?0.88:1.14,ev.clientX,ev.clientY);},{passive:false});
var drag=null;
svg.addEventListener('pointerdown',function(ev){if(ev.target.closest&&ev.target.closest('.node'))return;drag={x:ev.clientX,y:ev.clientY,vx:vb.x,vy:vb.y,moved:false};svg.classList.add('drag');svg.setPointerCapture(ev.pointerId);});
svg.addEventListener('pointermove',function(ev){if(!drag)return;var m=svg.getScreenCTM();if(!m)return;var dx=(ev.clientX-drag.x)/m.a,dy=(ev.clientY-drag.y)/m.d;if(Math.abs(dx)+Math.abs(dy)>3)drag.moved=true;vb.x=drag.vx-dx;vb.y=drag.vy-dy;setViewBox();});
svg.addEventListener('pointerup',function(ev){if(drag&&!drag.moved){sel=null;applyFocus();showPanel(null);}drag=null;svg.classList.remove('drag');});

/* ---------------- controls ---------------- */
function setView(v){
  view=v;sel=null;hov=null;
  $('vGroup').classList.toggle('on',v==='group');$('vType').classList.toggle('on',v==='type');
  build();
  var mw=maxW();var s=$('minw');s.max=String(Math.max(2,mw));
  minW=userMinW[v]!=null?userMinW[v]:defaultMinW();s.value=String(minW);$('minwv').textContent=String(minW);
  render();fit();showPanel(null);
}
$('vGroup').onclick=function(){setView('group');};
$('vType').onclick=function(){setView('type');};
$('minw').oninput=function(){minW=parseInt(this.value,10)||1;userMinW[view]=minW;$('minwv').textContent=String(minW);render();};
$('q').oninput=function(){query=this.value.trim();applyFocus();};
$('zin').onclick=function(){var r=svg.getBoundingClientRect();zoomAt(0.8,r.left+r.width/2,r.top+r.height/2);};
$('zout').onclick=function(){var r=svg.getBoundingClientRect();zoomAt(1.25,r.left+r.width/2,r.top+r.height/2);};
$('fit').onclick=fit;
$('dl').onclick=function(){
  var c=svg.cloneNode(true);c.setAttribute('viewBox','-10 -10 '+(content.w+20)+' '+(content.h+20));c.setAttribute('width',content.w+20);c.setAttribute('height',content.h+20);
  var s=new XMLSerializer().serializeToString(c);var blob=new Blob([s],{type:'image/svg+xml'});var a=document.createElement('a');
  a.href=URL.createObjectURL(blob);a.download='architecture_graph_'+view+'.svg';document.body.appendChild(a);a.click();document.body.removeChild(a);setTimeout(function(){URL.revokeObjectURL(a.href);},500);
};
window.addEventListener('resize',function(){fit();});
if(window.matchMedia){var mq=window.matchMedia('(prefers-color-scheme: dark)');var cb=function(){render();};if(mq.addEventListener)mq.addEventListener('change',cb);else if(mq.addListener)mq.addListener(cb);}

/* ---------------- start ---------------- */
$('title').textContent='Architecture Graph - '+D.meta.project;
$('subtitle').textContent=D.meta.types+' types in '+D.meta.files+' files · '+(D.meta.framework||'framework not recorded')+' · IR schema '+(D.meta.schema||'?')+' · IR generated '+(D.meta.generated||'?')+' · grouping: '+D.meta.group_by;
var bn=$('banner');bn.textContent=D.meta.banner;if(D.meta.warn)bn.classList.add('warn');
setView('group');
window.__arch={D:D,setView:setView};
})();
</script>
</body>
</html>
"""


# ============================ GRAPH MODEL =============================
L_PRES = "Presentation"
L_BIZ = "Business / shared logic"
L_GEN = "Generated / framework (My.*)"
L_DATA = "Data access"
TIER = {L_PRES: 0, L_BIZ: 1, L_GEN: 1, L_DATA: 2}
LAYER_KEY = {L_PRES: "pres", L_BIZ: "biz", L_GEN: "gen", L_DATA: "data"}
SHORT = {L_PRES: "Presentation", L_BIZ: "Business logic", L_GEN: "Generated (My.*)", L_DATA: "Data access"}

PRIMITIVE_LIBS = set("System." + x for x in (
    "Void Boolean String Int16 Int32 Int64 UInt16 UInt32 UInt64 Byte SByte Char Object Double Single Decimal DateTime TimeSpan Guid "
    "Array Type EventArgs EventHandler IntPtr Exception ValueType Enum Nullable Delegate").split())
PRIMITIVE_LIBS.update(["(unqualified)", "?", ""])

DBAPI_RE = re.compile(r"(ADODB\.|\bSqlConnection\b|\bSqlCommand\b|\bSqlDataAdapter\b|\bOleDbConnection\b|\bOleDbCommand\b|"
                      r"\bOdbcConnection\b|\bOracleConnection\b|\bMySqlConnection\b|\bDataAdapter\b|\bExecuteReader\b|"
                      r"\bExecuteNonQuery\b|\bExecuteScalar\b)")
DATA_NAME_RE = re.compile(r"(?i)(dbhelper|database|repository|dataaccess|sqlhelper|connection|\bdal\b|datalayer)")


def tidx_of(p):
    m = re.match(r"Types\[(\d+)\]", p)
    return int(m.group(1)) if m else None


def clean_table_name(x):
    x = x.replace("[", "").replace("]", "").strip(" ,;()\"")
    return re.sub(r"(?i)^dbo\.", "", x)


def sql_tables(S):
    """table -> {type index -> set(ops)}, from SQL literals found in code."""
    crud = defaultdict(lambda: defaultdict(set))
    for p, lit in S["sql"]:
        ti = tidx_of(p)
        low = lit.lower().lstrip(" `'\"(")
        if low.startswith("select"):
            op, names = "R", re.findall(r"\b(?:from|join)\s+([\[\]\w\.]+)", lit, re.I)
        elif low.startswith("insert"):
            op, names = "C", re.findall(r"\binto\s+([\[\]\w\.]+)", lit, re.I)
        elif low.startswith("update"):
            op, names = "U", re.findall(r"\bupdate\s+([\[\]\w\.]+)", lit, re.I)
        elif low.startswith("delete"):
            op, names = "D", re.findall(r"\bfrom\s+([\[\]\w\.]+)", lit, re.I)
        else:
            continue
        for nm in names:
            nm = clean_table_name(nm)
            if nm and nm.lower() not in ("select", "where", "set", "values") and not nm[0].isdigit():
                crud[nm][ti].add(op)
    return crud


def ext_category(v, dep_vendors):
    if v == "WinForms":
        return "Framework (UI)"
    if v.startswith("System") or v.startswith("Microsoft"):
        return "Framework (.NET)"
    if v.lower() in dep_vendors:
        return "Third-party (in ExternalDependencies)"
    return "Unresolved / unknown library"


def top_pairs(counter, names, n=8):
    return [[names[k], v] for k, v in counter.most_common(n)]


def build_graph_model(ir, M, S, G):
    T = M["types"]
    A = ir.get("AnalysisStatus") or {}
    MET = ir.get("Metrics") or {}
    file_lines = dict((gi(f, "RelativePath", default=""), gi(f, "Lines", default=0) or 0) for f in M["files"])
    sql_count, has_db = Counter(), set()
    for p, _ in S["sql"]:
        ti = tidx_of(p)
        if ti is not None:
            sql_count[ti] += 1
    for p, s in S["code_strings"]:
        ti = tidx_of(p)
        if ti is not None and DBAPI_RE.search(s):
            has_db.add(ti)
    have_code = bool(S["code_strings"])
    rules = [(re.compile(rx), lab, lay) for rx, lab, lay in GROUP_RULES]

    # ---- 1. layer per type (with the basis, so the picture can say how sure it is) ----
    info = {}
    for x in T:
        i, full = x["i"], x["full"]
        rule = next(((lab, lay) for rx, lab, lay in rules if rx.search(full)), None)
        if rule:
            info[i] = (rule[1], "manual GROUP_RULES entry", rule[0])
        elif x["is_form"]:
            info[i] = (L_PRES, "form: .Designer.vb file or InitializeComponent present (evidenced)", None)
        elif ".My." in full or full.split(".")[-1].startswith("My") and "." in full and full.rsplit(".", 1)[0].endswith("My"):
            info[i] = (L_GEN, "VB 'My' namespace type (evidenced by name)", None)
        elif sql_count.get(i) or i in has_db:
            info[i] = (L_DATA, "SQL literals or ADO/SqlClient API calls found in its code (evidenced)", None)
        elif not have_code and DATA_NAME_RE.search(full):
            info[i] = (L_DATA, "type name suggests data access; no code in JSON to confirm (Assumption)", None)
        else:
            info[i] = (L_BIZ, "default: not a form and no data-access evidence (Assumption)", None)

    ns_by_layer = defaultdict(set)
    for x in T:
        ns_by_layer[info[x["i"]][0]].add(x["ns"] or "(global)")

    def group_of(x):
        layer, _, manual = info[x["i"]]
        if manual:
            return "g:rule:" + manual, manual, layer
        if GROUP_BY == "layer_namespace" and len(ns_by_layer[layer]) > 1:
            ns = x["ns"] or "(global)"
            return "g:%s|%s" % (layer, ns), "%s - %s" % (SHORT.get(layer, layer), ".".join(ns.split(".")[-2:])), layer
        return "g:" + layer, SHORT.get(layer, layer), layer

    groups = {}
    for x in T:
        gid, glabel, layer = group_of(x)
        g = groups.setdefault(gid, {"id": gid, "label": glabel, "layer": layer, "tier": TIER.get(layer, 1), "types": [], "files": set(),
                                    "methods": 0, "forms": 0, "desc": ""})
        g["types"].append(x["i"])
        g["files"].update(x["src"])
        g["methods"] += len(x["methods"])
        g["forms"] += 1 if x["is_form"] else 0
    glist = sorted(groups.values(), key=lambda g: (g["tier"], g["label"]))
    for g in glist:
        g["loc"] = sum(file_lines.get(f, 0) for f in g["files"])
        g["files"] = len(g["files"])
    gof = {}
    for g in glist:
        for i in g["types"]:
            gof[i] = g["id"]

    # ---- 2. type-to-type edges (type refs + calls) ----
    idx_of = dict((x["id"], x["i"]) for x in T)
    tedge = {}
    for frm, d in G["out_deps"].items():
        for to, n in d.items():
            if frm in idx_of and to in idx_of and frm != to:
                tedge.setdefault((idx_of[frm], idx_of[to]), [0, 0])[0] += n
    for frm, d in G["call_t"].items():
        for to, n in d.items():
            if frm in idx_of and to in idx_of and frm != to:
                tedge.setdefault((idx_of[frm], idx_of[to]), [0, 0])[1] += n
    tedges = [{"s": a, "t": b, "refs": v[0], "calls": v[1], "w": v[0] + v[1]} for (a, b), v in tedge.items()]
    gedge = {}
    for e in tedges:
        ga, gb = gof[e["s"]], gof[e["t"]]
        if ga != gb:
            r = gedge.setdefault((ga, gb), {"refs": 0, "calls": 0, "pairs": 0})
            r["refs"] += e["refs"]
            r["calls"] += e["calls"]
            r["pairs"] += 1
    gedges = [{"s": a, "t": b, "refs": v["refs"], "calls": v["calls"], "pairs": v["pairs"], "w": v["refs"] + v["calls"]} for (a, b), v in gedge.items()]
    intra = Counter()
    for e in tedges:
        if gof[e["s"]] == gof[e["t"]]:
            intra[gof[e["s"]]] += e["w"]

    # ---- 3. external nodes (libraries, database, configuration) ----
    vendors_dep = defaultdict(list)
    for d in ir.get("ExternalDependencies") or []:
        vendors_dep[vendor_from_dep(d)].append(d)
    dep_lower = set(k.lower() for k in vendors_dep)
    ext, xtype, hidden = {}, defaultdict(Counter), Counter()
    for x in T:
        for v, n in G["vendor_use"].get(x["id"], {}).items():
            if HIDE_PRIMITIVE_LIBS and v in PRIMITIVE_LIBS:
                hidden[v] += n
                continue
            eid = "x:" + v
            ext.setdefault(eid, {"id": eid, "label": v, "kind": "lib", "cat": ext_category(v, dep_lower), "detail": [], "total": 0, "source": "TypeDependencyGraph.Edges (unresolved-type targets)"})
            ext[eid]["total"] += n
            xtype[eid][x["i"]] += n
    for v, deps in vendors_dep.items():
        eid = "x:" + v
        if eid not in ext and not any(e["label"].lower() == v.lower() for e in ext.values()):
            ext[eid] = {"id": eid, "label": v, "kind": "lib", "cat": "Third-party (in ExternalDependencies)", "detail": [], "total": 0, "source": "ExternalDependencies"}
        if eid in ext:
            ext[eid]["detail"] = deps[:12]
    crud = sql_tables(S)
    db_types = sorted(set(sql_count) | has_db)
    if db_types:
        ext["db"] = {"id": "db", "label": "Database (engine not evidenced)", "kind": "db", "cat": "Data store",
                     "detail": ["tables from SQL literals: " + (", ".join(sorted(crud)[:15]) or "none extracted"),
                                "connection strings found: %d" % len(S["conns"])] + ["conn: " + c for _, c in S["conns"][:3]],
                     "total": sum(sql_count.values()) or len(db_types), "source": "SQL literals / ADO API calls in Types[].Methods[] code"}
        for i in db_types:
            xtype["db"][i] = sql_count.get(i) or 1
    cfg = ir.get("ConfigurationArtifacts") or []
    if cfg:
        kinds = Counter(gi(c, "Kind", default="?") for c in cfg)
        ext["cfg"] = {"id": "cfg", "label": "Configuration & resources", "kind": "cfg", "cat": "Files",
                      "detail": ["%s x%d" % kv for kv in kinds.most_common()] + [gi(c, "Path", default="?") for c in cfg[:10]],
                      "total": len(cfg), "source": "ConfigurationArtifacts"}
        stems = set(os.path.splitext(os.path.basename(str(gi(c, "Path", default="")).replace("\\", "/")))[0].lower() for c in cfg
                    if str(gi(c, "Kind", default="")).upper() == "RESX")
        for x in T:
            for s_ in x["src"]:
                st = re.sub(r"\.designer$", "", os.path.splitext(os.path.basename(s_.replace("\\", "/")))[0].lower())
                if st in stems:
                    xtype["cfg"][x["i"]] += 1
                    break
    xtype_edges = [{"s": i, "t": eid, "w": n} for eid, c in xtype.items() for i, n in c.items()]
    xg = defaultdict(int)
    for e in xtype_edges:
        xg[(gof[e["s"]], e["t"])] += e["w"]
    xgroup_edges = [{"s": a, "t": b, "w": w} for (a, b), w in xg.items()]

    # ---- 4. per-type facts for tooltips / panel ----
    names = dict((x["i"], x["name"]) for x in T)
    out_c, in_c = defaultdict(Counter), defaultdict(Counter)
    for e in tedges:
        out_c[e["s"]][e["t"]] += e["w"]
        in_c[e["t"]][e["s"]] += e["w"]
    ext_of = defaultdict(Counter)
    for e in xtype_edges:
        ext_of[e["s"]][ext[e["t"]]["label"]] += e["w"]
    types_out = []
    for x in T:
        i = x["i"]
        types_out.append({
            "i": i, "name": x["name"], "full": x["full"], "kind": x["kind"], "layer": info[i][0], "basis": info[i][1], "group": gof[i],
            "form": bool(x["is_form"]), "loc": x["loc"], "methods": len(x["methods"]), "conds": x["nconds"], "files": x["src"],
            "sql": sql_count.get(i, 0), "db": i in has_db, "fanout": sum(out_c[i].values()), "fanin": sum(in_c[i].values()),
            "out": top_pairs(out_c[i], names), "in": top_pairs(in_c[i], names), "ext": [[k, v] for k, v in ext_of[i].most_common(8)],
            "src": "Types[%d]" % i})
    for g in glist:
        g["intra"] = intra.get(g["id"], 0)

    # ---- 5. confidence banner ----
    unresolved, internal = MET.get("UnresolvedEdges"), MET.get("InternalEdges")
    warn = (A.get("Authoritative") is False) or bool(A.get("CompilationErrors"))
    banner = ("Authoritative=%s; compilation errors=%s; internal edges=%s vs unresolved edges=%s. %s" % (
        A.get("Authoritative"), A.get("CompilationErrors"), internal, unresolved,
        "Treat the graph as incomplete: many links could not be resolved, so internal dependencies are probably under-counted." if warn
        else "No analysis warning recorded.")) if A or MET else "AnalysisStatus/Metrics not present in the JSON: confidence unknown."
    model = {
        "meta": {"project": ir.get("ProjectName") or "VB.NET project", "schema": ir.get("SchemaVersion"), "generated": ir.get("GeneratedAtUtc"),
                 "framework": ir.get("TargetFramework"), "banner": banner, "warn": warn, "types": len(T), "files": len(M["files"]),
                 "hidden_libs": dict(hidden), "have_code": have_code, "group_by": GROUP_BY},
        "groups": glist, "types": types_out, "ext": sorted(ext.values(), key=lambda e: (e["kind"] != "db", e["kind"] != "cfg", -e["total"], e["label"])),
        "tEdges": tedges, "gEdges": gedges, "xTypeEdges": xtype_edges, "xGroupEdges": xgroup_edges,
        "crud": dict((t, dict((str(k), "".join(sorted(v))) for k, v in per.items())) for t, per in crud.items()),
    }
    return model


def llm_group_descriptions(model):
    lines = []
    tn = dict((t["i"], t["name"]) for t in model["types"])
    for g in model["groups"]:
        lines.append("%s | layer=%s | types: %s" % (g["id"], g["layer"], ", ".join(tn[i] for i in g["types"][:60])))
    out = ask("groups", "arch-groups",
              "For each group write ONE short sentence on what the group is for, based only on its type names. "
              "Output one line per group in the form: GROUP_ID | sentence (starting with 'Assumption:').", "\n".join(lines))
    by_id = dict((g["id"], g) for g in model["groups"])
    n = 0
    for line in out.splitlines():
        if " | " in line:
            gid, txt = line.split(" | ", 1)
            gid = gid.strip()
            if gid in by_id:
                by_id[gid]["desc"] = txt.strip()
                n += 1
    log("LLM described %d of %d groups" % (n, len(model["groups"])))


# ============================ MARKDOWN ================================
MM_STYLE = [
    "classDef pres fill:#dbeafe,stroke:#3b82f6,color:#111",
    "classDef biz fill:#d1fae5,stroke:#10b981,color:#111",
    "classDef data fill:#fef3c7,stroke:#f59e0b,color:#111",
    "classDef gen fill:#e2e8f0,stroke:#94a3b8,color:#111",
    "classDef ext fill:#f1f5f9,stroke:#64748b,stroke-dasharray:4 3,color:#111",
    "classDef db fill:#f3e8ff,stroke:#a855f7,color:#111",
    "classDef cfg fill:#e0f2fe,stroke:#0ea5e9,color:#111",
]


def ml(s):
    return oneline(re.sub(r'["\[\]{}|<>#;`]', " ", str(s).replace("&", "and")))


def gid_mm(gid):
    return "G_" + re.sub(r"[^0-9A-Za-z]", "_", gid)


def ext_cls(e):
    return {"db": "db", "cfg": "cfg"}.get(e["kind"], "ext")


def ext_node_mm(e, nid):
    lab = ml(trunc(e["label"], 40))
    if e["kind"] == "db":
        return '  %s[("%s")]:::db' % (nid, lab)
    if e["kind"] == "cfg":
        return '  %s["%s"]:::cfg' % (nid, lab)
    return '  %s(["%s"]):::ext' % (nid, lab)


def to_markdown(model):
    meta, groups, types, ext = model["meta"], model["groups"], model["types"], model["ext"]
    tname = dict((t["i"], t) for t in types)
    ext_by_id = dict((e["id"], e) for e in ext)
    # keep externals that matter: db, cfg, then top by weight
    keep = [e for e in ext if e["kind"] in ("db", "cfg")] + [e for e in ext if e["kind"] == "lib" and e["total"] > 0][:MD_MAX_EXTERNALS]
    keep_ids = set(e["id"] for e in keep)

    # ---- overview ----
    ov = ["flowchart TB"]
    for g in groups:
        sub = "%d types, %s LOC" % (len(g["types"]), "{:,}".format(g["loc"]))
        ov.append('  %s["%s<br/>%s"]:::%s' % (gid_mm(g["id"]), ml(g["label"]), sub, LAYER_KEY.get(g["layer"], "biz")))
    for e in keep:
        ov.append(ext_node_mm(e, "X_" + re.sub(r"[^0-9A-Za-z]", "_", e["id"])))
    for e in sorted(model["gEdges"], key=lambda z: -z["w"]):
        ov.append('  %s -->|"%d refs, %d calls"| %s' % (gid_mm(e["s"]), e["refs"], e["calls"], gid_mm(e["t"])))
    for e in sorted(model["xGroupEdges"], key=lambda z: -z["w"]):
        if e["t"] in keep_ids:
            ov.append('  %s -.->|"%d"| %s' % (gid_mm(e["s"]), e["w"], "X_" + re.sub(r"[^0-9A-Za-z]", "_", e["t"])))
    ov += ["  " + s for s in MM_STYLE]
    ov_mm = "```mermaid\n" + "\n".join(ov) + "\n```"

    # ---- layer detail ----
    shown = set()
    dl = ["flowchart TB"]
    for g in groups:
        members = sorted(g["types"], key=lambda i: -(tname[i]["fanin"] + tname[i]["fanout"]))
        vis = members[:MD_MAX_TYPES_PER_LAYER]
        dl.append('  subgraph %s["%s"]' % ("S" + gid_mm(g["id"]), ml(g["label"])))
        for i in vis:
            t = tname[i]
            dl.append('    t%d["%s"]:::%s' % (i, ml(trunc(t["name"], 32)), LAYER_KEY.get(g["layer"], "biz")))
            shown.add(i)
        if len(members) > len(vis):
            dl.append('    more_%s["+%d more types"]:::%s' % (re.sub(r"[^0-9A-Za-z]", "_", g["id"]), len(members) - len(vis), LAYER_KEY.get(g["layer"], "biz")))
        dl.append("  end")
    edges = [e for e in model["tEdges"] if e["s"] in shown and e["t"] in shown]
    edges = sorted(edges, key=lambda z: -z["w"])[:MD_MAX_TYPE_EDGES]
    for e in edges:
        dl.append('  t%d -->|"%d"| t%d' % (e["s"], e["w"], e["t"]))
    dl += ["  " + s for s in MM_STYLE]
    dt_mm = "```mermaid\n" + "\n".join(dl) + "\n```"

    # ---- tables ----
    g_rows = [[g["label"], g["layer"], len(g["types"]), g["forms"], g["methods"], g["loc"], g["intra"]] for g in groups]
    basis = Counter((t["layer"], t["basis"]) for t in types)
    b_rows = [[l, b, n] for (l, b), n in sorted(basis.items())]
    x_rows = [[e["label"], e["cat"], e["total"], trunc(", ".join(e["detail"][:4]), 100) or "-"] for e in ext]
    strong = sorted(model["gEdges"], key=lambda z: -z["w"])[:10]
    gl = dict((g["id"], g["label"]) for g in groups)
    s_rows = [[gl[e["s"]], gl[e["t"]], e["refs"], e["calls"], e["pairs"]] for e in strong]
    open_q = [
        "Layers are inferred (see the basis table). Confirm them with someone who knows the application.",
        "The database engine is not stated anywhere in the JSON." if "db" in ext_by_id else "No database access was detected in the code strings.",
        "External libraries marked 'Unresolved / unknown' could not be resolved by the analysis; re-run it with the reference assemblies.",
    ]
    if not meta["have_code"]:
        open_q.append("The JSON contains no method code, so data-access detection relied on type names only.")
    hid = sum(meta["hidden_libs"].values())
    out = ["# Architecture Graph - %s" % meta["project"],
           "_Generated %s from the IR JSON (schema %s, IR generated %s). Interactive version: `%s`._" % (
               datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), meta["schema"], meta["generated"], OUTPUT_HTML),
           ("> **Confidence warning.** " if meta["warn"] else "> **Confidence.** ") + meta["banner"],
           "## 1. Overview\n\nSolid boxes = parts of the application, dashed = external libraries, arrows = dependencies (refs = type references, calls = method calls).\n\n" + ov_mm,
           "## 2. Detail by layer (top %d types per layer by connectivity, top %d links)\n\n" % (MD_MAX_TYPES_PER_LAYER, MD_MAX_TYPE_EDGES) + dt_mm,
           "## 3. Groups\n\n" + md_table(["Group", "Layer", "Types", "Forms", "Methods", "LOC", "Internal refs"], g_rows),
           "### How each type was placed in a layer\n\n" + md_table(["Layer", "Basis", "Types"], b_rows),
           "## 4. External systems and data\n\n" + md_table(["Name", "Category", "Weight", "Detail"], x_rows) +
           ("\n\n%d references to .NET primitive types (System.Void, Boolean, ...) are hidden as noise." % hid if hid else ""),
           "## 5. Strongest links between groups\n\n" + (md_table(["From", "To", "Type refs", "Calls", "Type pairs"], s_rows) if s_rows else "No links between different groups were found."),
           "## 6. Assumptions and open questions\n\n" + "\n".join("- " + q for q in open_q)]
    return "\n\n".join(out) + "\n"


# ================================ HTML ================================
def to_html(model):
    data = json.dumps(model, ensure_ascii=False, default=str).replace("</", "<\\/")
    return (HTML_TEMPLATE.replace("/*__TITLE__*/", re.sub(r"[<>&]", "", "Architecture Graph - %s" % model["meta"]["project"]))
            .replace("/*__DATA__*/", data))


def main():
    global CACHE_DIR
    if "<PATH" in IR_JSON_PATH or "<PATH" in OUTPUT_DIR:
        sys.exit("Set IR_JSON_PATH and OUTPUT_DIR at the top of the script.")
    if USE_LLM and not DRY_RUN and requests is None:
        sys.exit("pip install requests (or set USE_LLM = False)")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    CACHE_DIR = os.path.join(OUTPUT_DIR, "cache_arch")
    os.makedirs(CACHE_DIR, exist_ok=True)
    log("loading %s" % IR_JSON_PATH)
    with open(IR_JSON_PATH, "r", encoding="utf-8-sig") as f:
        ir = json.load(f)
    log("extracting facts")
    M = build_model(ir)
    S = scan_ir(ir, M)
    G = build_graphs(ir, M)
    model = build_graph_model(ir, M, S, G)
    log("types=%d groups=%d externals=%d type-edges=%d group-edges=%d" % (
        len(model["types"]), len(model["groups"]), len(model["ext"]), len(model["tEdges"]), len(model["gEdges"])))
    if USE_LLM:
        try:
            llm_group_descriptions(model)
        except Exception as e:  # the graph does not depend on the LLM
            log("LLM step skipped: %s" % e)
    md_path = os.path.join(OUTPUT_DIR, OUTPUT_MD)
    html_path = os.path.join(OUTPUT_DIR, OUTPUT_HTML)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(to_markdown(model))
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(to_html(model))
    log("DONE -> %s" % md_path)
    log("DONE -> %s" % html_path)


if __name__ == "__main__":
    main()
