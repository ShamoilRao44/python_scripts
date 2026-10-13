#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_pud.py  -  VB.NET IR JSON  ->  project_understanding_document.md

Single file, Python 3.8 compatible, only dependency: requests.

HOW IT KEEPS THE LLM EFFICIENT (the whole JSON is NEVER sent to the LLM)
  Stage 0  Python loads the JSON and extracts every fact it can deterministically
           (inventories, metrics, dependency/call graphs, regex scans, Mermaid
           diagrams, tables). No LLM involved.
  Stage 1  MAP: one small LLM call per type, fed a compact digest of that type
           (controls grouped by prefix, method signatures, trimmed code snippets).
  Stage 2  MAP: batches of "Conditions" are turned into business rules.
  Stage 3  REDUCE: each PUD section gets its own LLM call that only receives the
           deterministic facts relevant to that section + one-line type briefs.
  Stage 4  Python assembles the final Markdown (deterministic tables/diagrams are
           inserted by Python, the LLM only writes narrative), then writes the
           Executive Summary last and puts it first.
  Every LLM response is cached on disk (OUTPUT_DIR/cache), so a crash or re-run
  resumes where it stopped and costs nothing for finished calls.

INPUT  : the IR JSON only (no images). Set the two paths below.
OUTPUT : OUTPUT_DIR/project_understanding_document.md
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
except ImportError:  # only needed for real LLM calls
    requests = None

# =============================== CONFIG ===============================
IR_JSON_PATH = r"<PATH_TO_IR_JSON_FILE>"       # e.g. r"/mnt/MyDocuments/.../project.ir.json"
OUTPUT_DIR = r"<PATH_TO_OUTPUT_DIRECTORY>"     # e.g. r"/mnt/MyDocuments/.../pud_output"
IR_BASE_DIR = ""        # optional: folder that contains files/ and types/ (per-item IR). "" = not used
OUTPUT_FILENAME = "project_understanding_document.md"

# --- Local LLM (same pattern as the team's latest_llm_copy.py chat app) ---
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://172.31.8.100:9005/v1/chat/completions")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3-coder-next-awq")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "<PUT_API_KEY_HERE>")   # prefer env var over hard-coding
LLM_TIMEOUT = 1500
LLM_TEMPERATURE = 0.1
LLM_MAX_TOKENS = None       # e.g. 4096 to cap answers; None = server default
LLM_RETRIES = 3

# --- Prompt budgets (characters; ~4 chars per token) ---
MAX_PROMPT_CHARS = 90000        # hard guard per call (model context is capped at 250k tokens)
MAX_TYPE_DIGEST_CHARS = 24000   # per-type digest sent to the type-summary call
MAX_CODE_SNIPPET_CHARS = 700    # per-method code snippet (auto-shrinks if digest is too big)
COND_BATCH_CHARS = 30000        # size of each business-rule batch
MAX_TYPE_SUMMARY_IN_SECTION = 900   # chars of a type summary re-used inside section prompts

DRY_RUN = os.environ.get("PUD_DRY_RUN") == "1"   # 1 = no LLM calls (stub text), to test the pipeline

# ======================================================================
SYSTEM_PROMPT = (
    "You are a senior legacy-modernization analyst writing ONE section of a Project Understanding "
    "Document (PUD) for a VB.NET project.\n"
    "STRICT RULES:\n"
    "1. Use ONLY the material in the user message. Never invent facts, names, tables, versions or behaviour.\n"
    "2. After every key finding cite its source in square brackets exactly as written in the material, "
    "e.g. [JSON: Types[3].Methods[5]] or [JSON: AnalysisStatus].\n"
    "3. Anything inferred (not literally stated in the material) must start with the word 'Assumption:'.\n"
    "4. If something is unclear, missing or unreadable, do not guess; list it as an open question.\n"
    "5. Write simple, plain Markdown: short paragraphs and bullets. Do not repeat tables or diagrams that "
    "the material says are already provided; just refer to them.\n"
    "6. Do not write the section heading.\n"
    "7. End your answer with these two blocks, exactly as shown:\n"
    "@@ASSUMPTIONS@@\n- one bullet per assumption you made (or '- none')\n"
    "@@OPEN_QUESTIONS@@\n- one bullet per open question, saying who or what could answer it (or '- none')\n"
)

SYSTEM_PROMPT_TYPE = (
    "You analyse ONE VB.NET type from a legacy project. Use ONLY the digest provided. Never invent anything. "
    "Cite sources in square brackets exactly as written in the digest (e.g. [JSON: Types[3].Methods[5]]). "
    "Prefix inferred statements with 'Assumption:'. If something cannot be determined write 'unclear'. "
    "Be concise and factual."
)

TYPE_TASK = (
    "Summarise this type. Output exactly these lines (write 'none evidenced' when absent):\n"
    "PURPOSE: one sentence.\n"
    "CATEGORY: Form / Data-access / Module-or-helper / Settings-or-resources / Other\n"
    "BEHAVIOUR: bullets, each ending with a citation.\n"
    "BUSINESS_RULES: bullets (validation, calculations, permissions, status checks) with citation.\n"
    "DATA_ACCESS: SQL, tables, stored procedures, connection handling, with citation.\n"
    "EXTERNAL: files, devices, email, web, printers, 3rd-party controls, with citation.\n"
    "UNCLEAR: what cannot be determined from the digest."
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
        ("Late binding: 'As Object' declarations", r"\bAs\s+Object\b", "Medium"),
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
    m = re.search(r"[._]v?(\d+(?:[._]\d+)+)", name)
    return m.group(1).replace("_", ".") if m else ""


def complexity(T):
    score = T["loc"] / 400.0 + len(T["methods"]) / 15.0 + T["nconds"] / 15.0
    return "High" if score >= 4 else ("Medium" if score >= 1.5 else "Low")


# ============================ STAGE 1: TYPES ==========================
def describe_member(m, j, code_len, tindex, skip_code=False):
    if not isinstance(m, dict):
        return "  M%d: %s" % (j, trunc(m, 200)), None
    mid_ = gi(m, "Id", default="")
    label = method_label(mid_) if mid_ else gi(m, "Name", default="?")
    span = gi(m, "SourceSpan")
    where = ""
    if isinstance(span, dict):
        where = " L%s-%s" % (gi(span, "StartLine", default="?"), gi(span, "EndLine", default="?"))
    code = gi(m, "NormalizedCode", "RawCode", "Body", "Code")
    skipk = ("id", "name", "rawcode", "normalizedcode", "body", "code", "sourcefile", "sourcespan", "sourcetext")
    parts = []
    for k, v in m.items():
        if k.lower() in skipk or v in (None, "", [], {}, False):
            continue
        parts.append("%s=%s" % (k, compact(v, 110)))
    head = "  M%d: %s%s | %s" % (j, label, where, trunc(" | ".join(parts), 520))
    snippet = None
    if isinstance(code, str) and code.strip() and not skip_code and code_len > 0:
        snippet = "      code: " + trunc(oneline(code), code_len)
    return head, snippet


def type_digest(T, code_len):
    lines = []
    lines.append("Types[%d] %s | kind=%s | modifiers=%s" % (T["i"], T["full"], T["kind"], ",".join(map(str, T["mods"]))))
    lines.append("source files: %s | LOC of declared files: %d" % (", ".join(T["src"]) or "n/a", T["loc"]))
    lines.append("base class: %s | interfaces: %s | attributes: %s" % (T["base"] or "unknown", ", ".join(T["ifaces"]) or "none",
                                                                      trunc(", ".join(map(str, T["attrs"])), 160) or "none"))
    controls, other = [], []
    for j, f in enumerate(T["fields"]):
        if isinstance(f, dict):
            sf = str(gi(f, "SourceFile", default=""))
            nm = gi(f, "Name", default="?")
            tt = gi(f, "Type")
            ttxt = gi(tt, "Text", default="?") if isinstance(tt, dict) else str(tt)
            if sf.lower().endswith(".designer.vb") and nm != "components":
                controls.append((nm, ttxt))
            else:
                other.append((j, nm, ttxt, gi(f, "Visibility", default=""), gi(f, "InitializerCode")))
    if controls:
        cc = Counter(t.split(".")[-1] for _, t in controls)
        pre = Counter((re.match(r"^([a-z]+)(?=[A-Z_0-9])", n) or [None, "(other)"])[1] if re.match(r"^([a-z]+)(?=[A-Z_0-9])", n) else "(other)" for n, _ in controls)
        lines.append("designer controls: %d total; by control type: %s; by name prefix: %s [JSON: Types[%d].Fields]" % (
            len(controls), ", ".join("%s x%d" % kv for kv in cc.most_common(10)), ", ".join("%s x%d" % kv for kv in pre.most_common(8)), T["i"]))
        lines.append("control names (first 40): " + ", ".join(n for n, _ in controls[:40]))
    if other:
        lines.append("fields (non-designer):")
        for j, nm, tt, vis, init in other[:60]:
            lines.append("  F%d: %s : %s %s%s" % (j, nm, tt, vis, (" = " + trunc(oneline(init), 80)) if init else ""))
        if len(other) > 60:
            lines.append("  ... +%d more fields" % (len(other) - 60))
    if T["props"]:
        lines.append("properties: " + trunc(", ".join(compact(p, 60) for p in T["props"][:20]), 500))
    lines.append("methods (%d) [JSON: Types[%d].Methods]:" % (len(T["methods"]), T["i"]))
    for j, m in enumerate(T["methods"]):
        nm = T["mnames"][j] if j < len(T["mnames"]) else ""
        head, snip = describe_member(m, j, code_len, T["i"], skip_code=(nm in ("InitializeComponent", "Dispose")))
        lines.append(head.replace("  M%d:" % j, "  M%d:" % j))
        if snip:
            lines.append(snip)
    return "\n".join(lines)


def fit_digest(T):
    n = MAX_CODE_SNIPPET_CHARS
    while True:
        d = type_digest(T, n)
        if len(d) <= MAX_TYPE_DIGEST_CHARS or n <= 60:
            return trunc(d, MAX_TYPE_DIGEST_CHARS * 2) if len(d) > MAX_TYPE_DIGEST_CHARS * 2 else d
        n = n // 2


def summarize_types(M):
    summaries = {}
    total = len(M["types"])
    for T in M["types"]:
        log("type summary %d/%d: %s" % (T["i"] + 1, total, T["full"]))
        digest = fit_digest(T)
        prompt = "TASK:\n%s\n\nDIGEST:\n%s" % (TYPE_TASK, digest)
        summaries[T["i"]] = call_llm(prompt, SYSTEM_PROMPT_TYPE, "type-%d" % T["i"])
    return summaries


def purpose_of(txt):
    m = re.search(r"PURPOSE:\s*(.+)", txt or "")
    return trunc(m.group(1).strip(), 220) if m else trunc(oneline(txt or "n/a"), 160)


def category_of(txt):
    m = re.search(r"CATEGORY:\s*(.+)", txt or "")
    return trunc(m.group(1).strip(), 40) if m else ""


def briefs_block(M, TS):
    out = []
    for T in M["types"]:
        out.append("Types[%d] %s (%s, %s%d LOC, %d methods): %s" % (
            T["i"], T["full"], T["kind"], "form, " if T["is_form"] else "", T["loc"], len(T["methods"]), purpose_of(TS.get(T["i"]))))
    return "\n".join(out)


def full_summaries_block(M, TS, only=None, limit=MAX_TYPE_SUMMARY_IN_SECTION):
    out = []
    for T in M["types"]:
        if only is not None and T["i"] not in only:
            continue
        out.append("--- Types[%d] %s ---\n%s" % (T["i"], T["full"], trunc(TS.get(T["i"], ""), limit)))
    return "\n".join(out)


def business_rules(S):
    conds = S["conds"]
    if not conds:
        return "", False
    batches, cur, size = [], [], 0
    for c in conds:
        if size + len(c) > COND_BATCH_CHARS and cur:
            batches.append(cur)
            cur, size = [], 0
        cur.append(c)
        size += len(c) + 1
    if cur:
        batches.append(cur)
    rules = []
    for bi, b in enumerate(batches):
        log("business-rule batch %d/%d" % (bi + 1, len(batches)))
        prompt = ("TASK:\nFrom these code conditions extract business rules (validation, calculation, permission, status, "
                  "limits). One bullet per rule: plain-language rule, then the citation exactly as given at the start of "
                  "the line, then the method name. Skip purely technical conditions (null checks, UI toggles) unless they "
                  "express a business rule. Never invent.\n\nCONDITIONS:\n" + "\n".join(b))
        rules.append(call_llm(prompt, SYSTEM_PROMPT_TYPE, "rules-%d" % bi))
    return "\n".join(rules), True


# ============================ STAGE 3: SECTIONS =======================
def fact_json(d):
    return json.dumps(d, ensure_ascii=False, indent=1, default=str)


def build_all(ir, M, S, G, TS, rules_txt, has_conds):
    T = M["types"]
    A = ir.get("AnalysisStatus") or {}
    MET = ir.get("Metrics") or {}
    opts = ir.get("Options") or []
    optmap = dict(o.split("=", 1) for o in opts if isinstance(o, str) and "=" in o)
    ext = ir.get("ExternalDependencies") or []
    asm = ir.get("AssemblyReferences") or []
    cfg = ir.get("ConfigurationArtifacts") or []
    unres = ir.get("UnresolvedSymbols") or []
    files = ir.get("Files") or []
    sec = {}
    diagrams = []
    briefs = briefs_block(M, TS)
    forms = [x for x in T if x["is_form"]]
    non_forms = [x for x in T if not x["is_form"]]

    unres_groups = Counter()
    for s in unres:
        unres_groups[re.split(r"[.(\s]", s)[0] or s] += 1
    vendors_ext = defaultdict(list)
    for d in ext:
        vendors_ext[vendor_from_dep(d)].append(d)
    core_facts = {
        "ProjectName": ir.get("ProjectName"), "ProjectFile": ir.get("ProjectFile"), "Language": ir.get("Language"),
        "RootNamespace": ir.get("RootNamespace"), "TargetFramework": ir.get("TargetFramework"),
        "ProjectSdk": ir.get("ProjectSdk"), "LanguageVersion": ir.get("LanguageVersion"), "Options": opts,
        "Metrics": MET, "AnalysisStatus": dict((k, v) for k, v in A.items() if k not in ("WorkspaceDiagnostics", "MissingReferences")),
        "WorkspaceDiagnostics_count": len(A.get("WorkspaceDiagnostics") or []),
        "MissingReferences_count": len(A.get("MissingReferences") or []),
        "forms_count": len(forms), "other_types_count": len(non_forms),
        "ExternalDependency_vendors": dict((k, len(v)) for k, v in vendors_ext.items()),
        "ConfigurationArtifacts_by_kind": dict(Counter(gi(c, "Kind", default="?") for c in cfg)),
        "UnresolvedSymbol_roots_top": unres_groups.most_common(25),
    }
    core_json = "ALL FACTS BELOW ARE FROM THE JSON; cite as [JSON: <key>].\n" + fact_json(core_facts)

    # ---------- 2 Overview ----------
    proj_type = "WinForms (MyType=WindowsForms) [JSON: Options]" if optmap.get("MyType") == "WindowsForms" else "Not determinable from Options [JSON: Options]"
    ov_table = md_table(["Item", "Value", "Source"], [
        ["Project name", ir.get("ProjectName"), "[JSON: ProjectName]"],
        ["Project file", ir.get("ProjectFile"), "[JSON: ProjectFile]"],
        ["Language / version", "%s / %s" % (ir.get("Language"), ir.get("LanguageVersion") or "not recorded"), "[JSON: Language, LanguageVersion]"],
        ["Root namespace", ir.get("RootNamespace"), "[JSON: RootNamespace]"],
        ["Target framework", ir.get("TargetFramework"), "[JSON: TargetFramework]"],
        ["Project SDK", ir.get("ProjectSdk") or "none (classic .vbproj)", "[JSON: ProjectSdk]"],
        ["Project type", proj_type, "[JSON: Options]"],
        ["VB options", ", ".join(opts), "[JSON: Options]"],
        ["Owner", "Not present in JSON (see Open Questions)", "-"],
        ["Lifecycle status", "Not present in JSON (see Open Questions)", "-"],
        ["IR generated at", ir.get("GeneratedAtUtc"), "[JSON: GeneratedAtUtc]"],
    ])
    ov_llm = ask("2 Project Overview", "sec2",
                 "Write (a) the business problem the application appears to solve and (b) its goals, in 2 short paragraphs. "
                 "Everything beyond type/form names and their summaries is an Assumption.",
                 core_json + "\n\nTYPE BRIEFS:\n" + briefs)
    sec[2] = ov_table + "\n\n" + ov_llm

    # ---------- 3 Scope ----------
    sc_llm = ask("3 Scope", "sec3",
                 "Write: 'In scope' (modules/features evidenced by types and forms, as bullets), 'Out of scope / not evidenced' "
                 "(e.g. web services, integrations that are absent per Metrics), and 'User roles' (only roles evidenced, e.g. by "
                 "type names or login logic; otherwise Assumption).",
                 core_json + "\n\nTYPE BRIEFS:\n" + briefs)
    sec[3] = sc_llm

    # ---------- 4 Stakeholders ----------
    em = S["emails"]
    sec[4] = ("No owner, team, author or contact fields exist in the JSON schema.\n\n" +
              ("E-mail addresses found in the JSON: " + ", ".join(em) if em else "No e-mail addresses were found anywhere in the JSON.") +
              "\n\nSee Open Questions for who could confirm ownership. [JSON: whole file scanned]")

    # ---------- 5 Functional ----------
    form_rows = []
    for x in forms:
        handlers = [n for n, m in zip(x["mnames"], x["methods"]) if re.search(r"_(Click|Load|KeyPress|KeyDown|Tick|TextChanged|SelectedIndexChanged|CheckedChanged|CellContentClick|FormClosing|ToggleStateChanged|NewFrame)\b", n)]
        ctrls = sum(1 for f in x["fields"] if isinstance(f, dict) and str(gi(f, "SourceFile", default="")).lower().endswith(".designer.vb"))
        form_rows.append(["Types[%d]" % x["i"], x["name"], ", ".join(x["src"]), ctrls, len(handlers), purpose_of(TS.get(x["i"]))])
    forms_tbl = md_table(["Source", "Form", "Files", "Controls", "Event handlers", "Purpose (LLM summary)"], form_rows) if form_rows else "No forms detected."
    wf = ask("5 Functional Understanding", "sec5-workflows",
             "Describe the main business processes / user workflows (as numbered flows between forms), plus reports and batch jobs "
             "(say 'none evidenced' if there are none). Use form names and cite type summaries.",
             core_json + "\n\nTYPE SUMMARIES:\n" + full_summaries_block(M, TS))
    if has_conds and rules_txt:
        rules_section = ask("5 Functional Understanding", "sec5-rules",
                            "Consolidate these extracted business rules into a table with columns: Rule | Where implemented (type/method) | Source. "
                            "Merge duplicates, keep citations. Do not add rules that are not in the material.",
                            "EXTRACTED RULES:\n" + trunc(rules_txt, 60000))
    else:
        rules_section = ("No `Conditions` entries were found inside Types[].Methods[] in this JSON, so rules cannot be listed from conditions. "
                         "Rules mentioned in type summaries are in the workflow text above. [JSON: Metrics.Conditions=%s]" % MET.get("Conditions"))
    gl = ask("5 Functional Understanding", "sec5-glossary",
             "Create a glossary table (Term | Meaning | Source) of domain terms that appear in type, form, field or method names. "
             "Meanings that are not explicit in the material must start with 'Assumption:'.",
             "TYPE BRIEFS:\n" + briefs + "\n\nFORM CONTROL/FIELD SAMPLE:\n" +
             "\n".join("Types[%d]: %s" % (x["i"], ", ".join(str(gi(f, "Name", default="")) for f in x["fields"][:25] if isinstance(f, dict))) for x in T[:60]))
    sec[5] = ("### 5.1 Screens / forms\n" + forms_tbl + "\n\n### 5.2 Business processes and workflows\n" + wf +
              "\n\n### 5.3 Business rules\n" + rules_section + "\n\n### 5.4 Glossary\n" + gl)

    # ---------- 6 Architecture ----------
    ven_forms, ven_other = Counter(), Counter()
    for x in T:
        for v, n in G["vendor_use"].get(x["id"], {}).items():
            (ven_forms if x["is_form"] else ven_other)[v] += 1
    arch = ["flowchart LR", '  UI["Forms (%d)"]' % len(forms), '  OTH["Non-form types (%d)"]' % len(non_forms)]
    nonform_links = Counter()
    for x in forms:
        for t2, n in G["out_deps"].get(x["id"], {}).items():
            tt = M["by_id"][t2]
            if not tt["is_form"]:
                nonform_links[tt["name"]] += n
    for x in forms:
        for t2, n in G["call_t"].get(x["id"], {}).items():
            tt = M["by_id"][t2]
            if not tt["is_form"]:
                nonform_links[tt["name"]] += n
    for nm, n in nonform_links.most_common(8):
        arch.append('  %s["%s"]' % (mid("NF" + nm), mlabel(nm)))
        arch.append("  UI -->|%d refs| %s" % (n, mid("NF" + nm)))
    for v, n in (ven_forms + ven_other).most_common(10):
        arch.append('  %s(["%s (unresolved)"])' % (mid("V" + v), mlabel(v)))
        if ven_forms.get(v):
            arch.append("  UI -->|%d forms| %s" % (ven_forms[v], mid("V" + v)))
        if ven_other.get(v):
            arch.append("  OTH -->|%d types| %s" % (ven_other[v], mid("V" + v)))
    if unres_groups.get("ADODB"):
        arch.append('  ADO(["ADODB classic data access"])')
        arch.append("  OTH --> ADO")
        arch.append("  UI --> ADO")
    arch_mm = "```mermaid\n" + "\n".join(arch) + "\n```"
    diagrams.append("Architecture diagram: TypeDependencyGraph.Edges (unresolved-type targets grouped by namespace root), CallGraph.Edges, UnresolvedSymbols")
    stack_rows = [
        [".NET Framework / runtime", ir.get("TargetFramework"), "[JSON: TargetFramework]"],
        ["Language", "%s (LanguageVersion: %s)" % (ir.get("Language"), ir.get("LanguageVersion") or "not recorded"), "[JSON: Language]"],
        ["UI framework", "Windows Forms" if optmap.get("MyType") == "WindowsForms" else "unknown", "[JSON: Options]"],
        ["Data access", "ADODB referenced in code" if unres_groups.get("ADODB") else "no ADO/ORM evidence in UnresolvedSymbols", "[JSON: UnresolvedSymbols]"],
        ["NuGet packages", "none declared" if not ir.get("PackageReferences") else str(len(ir.get("PackageReferences"))), "[JSON: PackageReferences]"],
        ["Project references", "none" if not ir.get("ProjectReferences") else str(len(ir.get("ProjectReferences"))), "[JSON: ProjectReferences]"],
    ]
    for v, deps in sorted(vendors_ext.items()):
        vers = sorted(set(version_from_dep(d) for d in deps if version_from_dep(d)))
        stack_rows.append(["3rd-party: " + v, "%d assemblies%s" % (len(deps), (", version(s) " + ", ".join(vers)) if vers else ""), "[JSON: ExternalDependencies]"])
    stack_tbl = md_table(["Layer", "Technology / version", "Source"], stack_rows)
    ar_llm = ask("6 Technical Architecture", "sec6",
                 "Describe the layers and component interactions (refer to the Mermaid diagram, which is already provided), the data-access "
                 "approach, COM/ActiveX evidence (say none if none), and the deployment model (say 'not evidenced' unless stated).",
                 core_json + "\n\nTYPE BRIEFS:\n" + briefs + "\n\nCONFIG ARTIFACTS:\n" + trunc(", ".join(gi(c, "Path", default="?") for c in cfg), 1500))
    sec[6] = "### 6.1 Architecture diagram\n" + arch_mm + "\n\n### 6.2 Technology stack\n" + stack_tbl + "\n\n### 6.3 Narrative\n" + ar_llm

    # ---------- 7 Solution structure ----------
    folders = defaultdict(lambda: [0, 0])
    for f in files:
        rp = gi(f, "RelativePath", default="")
        d = os.path.dirname(rp.replace("\\", "/")) or "(project root)"
        folders[d][0] += 1
        folders[d][1] += gi(f, "Lines", default=0) or 0
    fold_tbl = md_table(["Folder", "Files", "Lines"], [[k, v[0], v[1]] for k, v in sorted(folders.items())])
    type_rows = [["Types[%d]" % x["i"], x["full"], x["kind"], ", ".join(x["src"]), x["loc"], category_of(TS.get(x["i"])), purpose_of(TS.get(x["i"]))] for x in T]
    type_tbl = md_table(["Source", "Type", "Kind", "Files", "LOC", "Category", "Responsibility"], type_rows)
    entry = [(x["i"], n) for x in T for n in x["mnames"] if n.lower() in ("main", "application_startup", "mysettings") or "startup" in n.lower()]
    entry_types = sorted(set(i for i, _ in entry) | set(x["i"] for x in T if "MyApplication" in x["full"]))
    st_llm = ask("7 Solution and Code Structure", "sec7",
                 "Explain (a) entry points and startup flow (only what is evidenced; otherwise open question), (b) the build system / references / "
                 "packages, (c) coding patterns and conventions (code-behind with .Designer.vb partial classes, global modules/state, etc.). Tables are already provided.",
                 core_json + "\n\nENTRY-POINT CANDIDATE TYPES:\n" + (full_summaries_block(M, TS, set(entry_types)) or "none found by name") +
                 "\n\nTYPE BRIEFS:\n" + briefs + "\n\nAssemblyReferences: %d entries, unresolved=%d; HintPath folders: %s [JSON: AssemblyReferences]" % (
                     len(asm), sum(1 for a in asm if gi(a, "Resolved") is False),
                     "; ".join(sorted(set(os.path.dirname(str(gi(a, "HintPath", default="")).replace("\\\\", "/").replace("\\", "/")) for a in asm))[:5])))
    sec[7] = ("### 7.1 Folder layout\n" + fold_tbl + "\n\n### 7.2 Every type and its responsibility\n" + type_tbl +
              "\n\n### 7.3 Entry points, build, patterns\n" + st_llm)

    # ---------- 8 Components ----------
    comp_rows = []
    for x in T:
        outs = G["out_deps"].get(x["id"], {})
        ins = G["in_deps"].get(x["id"], {})
        cout = G["call_t"].get(x["id"], {})
        deps = sorted(set(type_short(k) for k in list(outs) + list(cout)))
        dependents = sorted(set(type_short(k) for k in ins) | set(type_short(k) for k, v in G["call_t"].items() if x["id"] in v))
        comp_rows.append(["Types[%d]" % x["i"], x["name"], x["kind"], purpose_of(TS.get(x["i"])), x["loc"],
                          trunc(", ".join(deps), 120) or "-", trunc(", ".join(dependents), 120) or "-",
                          "%s (methods=%d, conditions=%d)" % (complexity(x), len(x["methods"]), x["nconds"])])
    comp_tbl = md_table(["Source", "Component", "Type", "Purpose", "LOC", "Depends on", "Used by", "Complexity*"], comp_rows)

    def edge_graph(edge_map, title):
        lines = ["flowchart LR"]
        used = set()
        n_edges = 0
        flat = sorted(((a, b, n) for a, c in edge_map.items() for b, n in c.items()), key=lambda z: -z[2])[:120]
        for a, b, n in flat:
            used.update([a, b])
            lines.append("  %s -->|%d| %s" % (mid(a), n, mid(b)))
            n_edges += 1
        for u in used:
            lines.insert(1, '  %s["%s"]' % (mid(u), mlabel(type_short(u))))
        return ("```mermaid\n" + "\n".join(lines) + "\n```") if n_edges else "No internal type-to-type edges were found. [JSON: %s]" % title

    dep_mm = edge_graph(G["out_deps"], "TypeDependencyGraph.Edges")
    call_mm = edge_graph(G["call_t"], "CallGraph.Edges")
    diagrams.append("Type dependency graph: TypeDependencyGraph.Edges where To is a known Types[].Id (top 120 edges)")
    diagrams.append("Call graph (type level): CallGraph.Edges between methods of different known types (top 120 edges)")
    sec[8] = ("*Complexity is a script heuristic (Assumption): based on LOC, method count and condition count.\n"
              "LOC = sum of lines of the files the type is declared in (partial files are counted for each type). [JSON: Files[].Lines]\n\n" +
              comp_tbl + "\n\n### 8.1 Type dependency graph\n" + dep_mm + "\n\n### 8.2 Call graph (type level)\n" + call_mm +
              "\n\nGraph status: TypeDependencyGraph=%s, CallGraph=%s; internal call edges=%s, unresolved edges=%s [JSON: AnalysisStatus, Metrics]" % (
                  A.get("TypeDependencyGraph"), A.get("CallGraph"), MET.get("InternalEdges"), MET.get("UnresolvedEdges")))

    # ---------- 9 Data ----------
    sql_tbl = md_table(["Source", "SQL literal (trimmed)"], [[p, "`" + s.replace("`", "'") + "`"] for p, s in S["sql"][:40]]) if S["sql"] else "No SQL literals were detected in code strings."
    conn_tbl = md_table(["Source", "Connection string (secrets masked)"], [[p, "`" + s + "`"] for p, s in S["conns"]]) if S["conns"] else "No connection strings were detected in code strings."
    sp_tbl = md_table(["Source", "Value"], [[p, v] for p, k, v in S["kv_hits"][:30]]) if S["kv_hits"] else "No stored-procedure/SQL keys were found in the JSON structure."
    data_ctx = (core_json + "\n\nSQL LITERALS (%d total, first 120 shown):\n" % len(S["sql"]) +
                "\n".join("%s %s" % (p, s) for p, s in S["sql"][:120]) +
                "\n\nSTRUCTURED SQL/STORED-PROC KEYS:\n" + "\n".join("%s %s" % (p, v) for p, k, v in S["kv_hits"][:60]) +
                "\n\nCONNECTION STRINGS (masked):\n" + "\n".join("%s %s" % (p, s) for p, s in S["conns"]) +
                "\n\nCommandType.StoredProcedure occurrences in code: %d" % S["sp_hint_count"] +
                "\n\nTYPE BRIEFS (look for data-access types):\n" + briefs)
    da_llm = ask("9 Data Understanding", "sec9",
                 "Describe the database(s) and type (only if evidenced), connection handling, entities/tables and relationships, stored procedures "
                 "(Metrics.StoredProcedureCalls is given), data flows between forms and the data layer, and any file-based storage. Tables of raw "
                 "SQL/connection evidence are already provided.", data_ctx)
    er_block = ""
    if S["sql"]:
        er_raw = call_llm("TASK:\nProduce ONE Mermaid erDiagram containing ONLY tables and columns that appear in these SQL statements. "
                          "Add relationships only if a JOIN or foreign key proves them. Output only the mermaid code (starting with erDiagram). "
                          "If you cannot determine any table, output the single word NONE.\n\nSQL:\n" +
                          "\n".join(s for _, s in S["sql"][:150]), SYSTEM_PROMPT_TYPE, "er")
        mm = re.search(r"(erDiagram.*?)(?:```|$)", er_raw, re.S)
        if mm and "NONE" not in er_raw[:20]:
            er_block = "```mermaid\n" + mm.group(1).strip() + "\n```"
            diagrams.append("ER diagram: LLM-derived from SQL literals only (verify)")
    if not er_block:
        er_block = "No ER diagram can be produced: no table structure is evidenced in the JSON."
    sec[9] = ("### 9.1 Narrative\n" + da_llm + "\n\n### 9.2 ER diagram\n" + er_block + "\n\n### 9.3 SQL embedded in code (first 40)\n" + sql_tbl +
              "\n\n### 9.4 Connection strings\n" + conn_tbl + "\n\n### 9.5 Stored procedure / SQL keys in JSON\n" + sp_tbl +
              "\n\nMetrics: StoredProcedureCalls=%s [JSON: Metrics.StoredProcedureCalls]" % MET.get("StoredProcedureCalls"))

    # ---------- 10 Integrations ----------
    hints = ["mail", "smtp", "gmail", "http", "web", "api", "pay", "sms", "print", "camera", "video", "com", "ftp", "socket", "serial", "barcode", "excel", "pdf", "browser"]
    integ_types = [x for x in T if any(h in x["full"].lower() for h in hints)]
    integ_rows = [["Types[%d]" % x["i"], x["full"], purpose_of(TS.get(x["i"]))] for x in integ_types]
    ext_rows = [[v, len(d), ", ".join(sorted(set(version_from_dep(x) for x in d if version_from_dep(x)))) or "-", trunc(", ".join(d), 140)] for v, d in sorted(vendors_ext.items())]
    int_llm = ask("10 Integrations and Interfaces", "sec10",
                  "List external systems, web services/APIs (Metrics.WebServices/WebOperations given), COM/DLL calls, file import/export, e-mail, printers, "
                  "devices (e.g. camera), payment or messaging, protocols and authentication. Only what is evidenced; otherwise state 'not evidenced'. "
                  "Tables are already provided.",
                  core_json + "\n\nINTEGRATION-LIKE TYPES (by name):\n" + (full_summaries_block(M, TS, set(x["i"] for x in integ_types)) or "none") +
                  "\n\nUNRESOLVED SYMBOL ROOTS:\n" + str(unres_groups.most_common(40)))
    sec[10] = ("### 10.1 Third-party / external assemblies [JSON: ExternalDependencies]\n" +
               (md_table(["Vendor/library", "Assemblies", "Version(s)", "Names"], ext_rows) if ext_rows else "none") +
               "\n\n### 10.2 Types whose names suggest an integration (Assumption: name-based)\n" +
               (md_table(["Source", "Type", "Purpose"], integ_rows) if integ_rows else "none") + "\n\n### 10.3 Narrative\n" + int_llm)

    # ---------- 11 Configuration ----------
    cfg_tbl = md_table(["Path", "Kind"], [[gi(c, "Path", default="?"), gi(c, "Kind", default="?")] for c in cfg[:80]]) if cfg else "none"
    my_types = [x for x in T if x["full"].split(".")[-1].startswith("My") or ".My." in x["full"]]
    logs = ", ".join("%s x%d" % kv for kv in S["logging_hits"].most_common()) or "none detected"
    cf_llm = ask("11 Configuration and Environments", "sec11",
                 "Summarise configuration (app.config, settings, resources), connection-string handling, environments (state 'not evidenced' if none), "
                 "deployment/installer details (likely not evidenced), logging and error handling. Use the counts provided.",
                 core_json + "\n\nCONFIG ARTIFACTS: %d total; kinds=%s [JSON: ConfigurationArtifacts]\nMY/SETTINGS TYPES: %s\n"
                 "Try blocks in code: %d; Catch clauses: %d; logging API hits: %s\nConnection strings found: %d (see Data section)" % (
                     len(cfg), dict(Counter(gi(c, 'Kind', default='?') for c in cfg)), ", ".join(x["full"] for x in my_types) or "none",
                     S["trycatch"], S["catch"], logs, len(S["conns"])))
    sec[11] = ("### 11.1 Configuration artifacts (first 80 of %d) [JSON: ConfigurationArtifacts]\n" % len(cfg) + cfg_tbl + "\n\n### 11.2 Narrative\n" + cf_llm)

    # ---------- 12 Non-functional ----------
    sec_ev = []
    for kw, cnt in S["security"].items():
        sec_ev.append("%s: %s" % (kw, ", ".join("Types[%d] x%d" % (i, n) for i, n in cnt.most_common(5))))
    nf_llm = ask("12 Non-Functional Aspects", "sec12",
                 "Assess security (authentication, authorization, credential handling, SQL concatenation risk), performance concerns, scalability, "
                 "reliability (error handling) and compliance. Only evidence-based; all judgements are Assumptions unless explicit.",
                 core_json + "\n\nKEYWORD EVIDENCE (identifier counts in code):\n" + ("\n".join(sec_ev) or "none") +
                 "\n\nDEBT SCAN:\n" + "\n".join("%s: %d" % (d["name"], d["count"]) for d in S["debt"]) +
                 "\n\nTYPE BRIEFS:\n" + briefs)
    sec[12] = nf_llm

    # ---------- 13 Debt ----------
    drows = []
    if optmap.get("OptionStrict", "").lower() == "off":
        drows.append(["Option Strict Off (implicit conversions / late binding allowed)", "OptionStrict=Off", "High", "[JSON: Options]"])
    if optmap.get("OptionExplicit", "").lower() == "off":
        drows.append(["Option Explicit Off", "OptionExplicit=Off", "High", "[JSON: Options]"])
    for d in S["debt"]:
        if d["count"]:
            drows.append([d["name"], "%d occurrences; top: %s" % (d["count"], ", ".join("Types[%s] x%d" % kv for kv in d["top"])), d["rating"], "[JSON: Types[].*%s]" % S["code_key_used"]])
    unres_asm = [a for a in asm if gi(a, "Resolved") is False]
    if unres_asm:
        drows.append(["Assembly references not resolved (HintPaths point outside the project)", "%d of %d unresolved" % (len(unres_asm), len(asm)), "High", "[JSON: AssemblyReferences]"])
    if A.get("Authoritative") is False:
        drows.append(["Analysis not authoritative: semantic model incomplete", "CompilationErrors=%s; SemanticCompilation=%s" % (A.get("CompilationErrors"), A.get("SemanticCompilation")), "High", "[JSON: AnalysisStatus]"])
    for v, d in sorted(vendors_ext.items()):
        vers = sorted(set(version_from_dep(x) for x in d if version_from_dep(x)))
        if vers:
            drows.append(["Pinned 3rd-party version(s) of %s: %s (Assumption: may be outdated or unsupported)" % (v, ", ".join(vers)), "%d assemblies" % len(d), "Medium", "[JSON: ExternalDependencies]"])
    drows.append(["Target framework %s (Assumption: older framework, verify support status externally)" % ir.get("TargetFramework"), "", "Medium", "[JSON: TargetFramework]"])
    tests = [f for f in files if "test" in str(gi(f, "RelativePath", default="")).lower()]
    drows.append(["Automated tests", "no test files found in Files[]" if not tests else "%d test-like files" % len(tests), "Medium" if not tests else "Low", "[JSON: Files[].RelativePath]"])
    if S["dups"]:
        drows.append(["Duplicated method bodies (identical normalized code)", "%d groups, e.g. %s" % (len(S["dups"]), "; ".join(", ".join(g[:3]) for g in S["dups"][:3])), "Medium", "[JSON: Types[].Methods[]]"])
    fan_in = sorted(((len(v), M["by_id"][k]["name"]) for k, v in G["in_deps"].items()), reverse=True)[:5]
    if fan_in:
        drows.append(["Highly depended-upon types (coupling hot-spots)", ", ".join("%s (%d)" % (n, c) for c, n in fan_in), "Medium", "[JSON: TypeDependencyGraph.Edges]"])
    debt_tbl = md_table(["Item", "Evidence", "Rating", "Source"], drows)
    top_ctx = "\n".join("%s | %s | %s | %s" % tuple(r) for r in drows)
    db_llm = ask("13 Current State, Technical Debt, Risks", "sec13",
                 "Write a short risk narrative: the top 5 risks, why each matters for migration, and any rating you would change (justify as Assumption). "
                 "The debt table is already provided.", core_json + "\n\nDEBT TABLE:\n" + top_ctx)
    sec[13] = ("Ratings are rule-based by this script (Assumption) and should be reviewed.\n\n" + debt_tbl + "\n\n" + db_llm)

    # ---------- 14 Modernization ----------
    order = G["tg_order"]
    order_txt = ", ".join("%d. %s" % (i + 1, type_short(x)) for i, x in enumerate(order[:60])) or "not present"
    md_llm = ask("14 Modernization Considerations", "sec14",
                 "Give: reusable vs rewrite (table: Component | Reuse/Rewrite | Reason | Source), candidate target stack (Assumption), a suggested migration order "
                 "(you may reuse the tool-suggested order given), quick wins, and blockers. Be concrete about the unresolved references and Option Strict Off.",
                 core_json + "\n\nTOOL-SUGGESTED TYPE MIGRATION ORDER [JSON: TypeDependencyGraph.SuggestedMigrationOrder]:\n" + order_txt +
                 "\n\nSCC (cycles): %s\n\nDEBT TABLE:\n%s\n\nTYPE BRIEFS:\n%s" % (G["tg_scc"], top_ctx, briefs))
    sec[14] = ("Tool-suggested type migration order (from the IR, `Partial` status so treat with care) [JSON: TypeDependencyGraph.SuggestedMigrationOrder]:\n\n" +
               order_txt + "\n\n" + md_llm)

    # ---------- 15 Assumptions / open questions ----------
    det_oq = [
        ("Owner / stakeholders / contacts", "Not in JSON. Ask: project owner, repository metadata or team lead."),
        ("Lifecycle status (active, maintenance, retired)", "Not in JSON. Ask: product owner."),
        ("Deployment model and installer", "No installer or publish information in JSON. Ask: build/release engineer."),
        ("Database engine and schema", "No definitive evidence unless SQL/connection strings appear in Section 9. Ask: DBA / app config on the server."),
        ("Missing framework and 3rd-party references", "%s compilation errors; Authoritative=%s. Re-run the analysis with .NET Framework reference assemblies and the DLLs on the HintPaths. [JSON: AnalysisStatus]" % (A.get("CompilationErrors"), A.get("Authoritative"))),
    ]
    if T and not any(x["methods"] for x in T):
        det_oq.append(("Method-level IR missing", "Types[].Methods was empty; set IR_BASE_DIR to the folder with the per-type IR (IrFile) to enrich the digests."))
    if not has_conds:
        det_oq.append(("Conditions not present in Types[]", "Business rules could not be mined. Provide per-method IR with Conditions or the source code."))
    dep_list = ["Local LLM server %s (model %s)" % (LLM_BASE_URL, LLM_MODEL), "IR JSON generated at %s [JSON: GeneratedAtUtc]" % ir.get("GeneratedAtUtc"),
                "Analysis loader: %s [JSON: AnalysisStatus.LoaderMode]" % A.get("LoaderMode")] + ["3rd-party library: %s" % v for v in sorted(vendors_ext)]

    sec_meta = {"diagrams": diagrams, "det_oq": det_oq, "dep_list": dep_list, "core_facts": core_facts, "forms": forms, "A": A, "MET": MET,
                "briefs": briefs, "vendors_ext": vendors_ext, "debt_rows": drows}
    return sec, sec_meta


# ============================ ASSEMBLY ================================
SECTION_TITLES = {
    1: "Executive Summary", 2: "Project Overview", 3: "Scope", 4: "Stakeholders and Contacts", 5: "Functional Understanding",
    6: "Technical Architecture", 7: "Solution and Code Structure", 8: "Component Inventory and Dependencies", 9: "Data Understanding",
    10: "Integrations and Interfaces", 11: "Configuration and Environments", 12: "Non-Functional Aspects",
    13: "Current State, Technical Debt, and Risks", 14: "Modernization Considerations",
    15: "Assumptions, Dependencies, and Open Questions", 16: "Appendices",
}

ROOT_KEY_USE = {
    "SchemaVersion": "Overview", "ProjectName": "Overview", "ProjectFile": "Overview", "Language": "Overview/Stack", "RootNamespace": "Overview",
    "TargetFramework": "Overview/Stack/Debt", "ProjectSdk": "Stack", "LanguageVersion": "Stack", "Options": "Overview/Debt",
    "GlobalImports": "Appendix (counted only)", "DefineConstants": "not used (empty)", "ProjectReferences": "Stack", "PackageReferences": "Stack",
    "AssemblyReferences": "Structure/Debt", "ConfigurationArtifacts": "Configuration", "Files": "Structure/Appendix", "Types": "Types, Functional, Data, Debt",
    "Namespaces": "Appendix", "ExternalDependencies": "Stack/Integrations/Debt", "UnresolvedSymbols": "Architecture/Integrations",
    "TypeDependencyGraph": "Architecture/Components/Modernization", "CallGraph": "Components", "AnalysisStatus": "Health/Debt/Open questions",
    "Metrics": "Executive summary/Data", "GeneratedAtUtc": "Overview",
}


def exec_summary(ir, M, TS, sec, meta):
    MET, A = meta["MET"], meta["A"]
    health = ("Analysis status: Syntax=%s, SemanticCompilation=%s, TypeDependencyGraph=%s, CallGraph=%s, Authoritative=%s, CompilationErrors=%s; "
              "unresolved edges=%s vs internal edges=%s [JSON: AnalysisStatus, Metrics]" % (
                  A.get("Syntax"), A.get("SemanticCompilation"), A.get("TypeDependencyGraph"), A.get("CallGraph"), A.get("Authoritative"),
                  A.get("CompilationErrors"), MET.get("UnresolvedEdges"), MET.get("InternalEdges")))
    ctx = ("FACTS:\n" + fact_json(meta["core_facts"]) + "\n\nHEALTH LINE:\n" + health + "\n\nTYPE BRIEFS:\n" + meta["briefs"] +
           "\n\nDEBT ROWS (item | evidence | rating | source):\n" + "\n".join(" | ".join(map(str, r)) for r in meta["debt_rows"][:14]))
    txt = ask("1 Executive Summary", "sec1",
              "Write a ONE-PAGE executive summary (max ~350 words) with these labelled parts: Purpose, Business domain, Main users, Tech stack, "
              "Size and complexity, Overall health. Cite sources.", ctx)
    return "**Generated facts at a glance**\n\n" + md_table(["Metric", "Value"], [[k, v] for k, v in MET.items()]) + "\n\n" + txt


def appendix(ir, M, meta, S):
    files = ir.get("Files") or []
    rows = []
    for k, f in enumerate(files):
        diags = len(gi(f, "Diagnostics", default=[]) or [])
        rows.append(["Files[%d]" % k, gi(f, "RelativePath", default=""), gi(f, "Lines", default=""), gi(f, "CompilationRole", default=""),
                     gi(f, "IsIncludedInCompilation", default=""), ", ".join(type_short(x) for x in (gi(f, "DeclaredTypes", default=[]) or [])), diags])
    inv = md_table(["Source", "File", "Lines", "Role", "In compilation", "Declared types", "Diagnostics"], rows) if rows else "none"
    codes = Counter()
    for f in files:
        for d in gi(f, "Diagnostics", default=[]) or []:
            m = re.match(r"(BC\d+)", str(d))
            if m:
                codes[m.group(1)] += 1
    parts = ["### 16.1 Full file inventory\n" + inv,
             "### 16.2 Compiler diagnostic codes across files [JSON: Files[].Diagnostics]\n" +
             (md_table(["Code", "Count"], codes.most_common(15)) if codes else "none"),
             "### 16.3 Diagram sources\n" + "\n".join("- " + d for d in meta["diagrams"]),
             "### 16.4 Namespaces [JSON: Namespaces]\n" + ", ".join(ir.get("Namespaces") or []),
             "### 16.5 Global imports [JSON: GlobalImports]\n" + ", ".join(ir.get("GlobalImports") or []),
             "### 16.6 Glossary\nSee section 5.4."]
    return "\n\n".join(parts)


def coverage(ir, M, S, meta):
    rows = []
    for k in ir.keys():
        rows.append([k, "used", ROOT_KEY_USE.get(k, "NOT MAPPED: key not interpreted by this script")])
    notes = []
    inline_methods = sum(len(x["methods"]) for x in M["types"])
    notes.append("Types[].Methods entries read: %d (Metrics.Methods=%s)" % (inline_methods, (ir.get("Metrics") or {}).get("Methods")))
    notes.append("Code strings scanned (key %s): %d" % (S["code_key_used"], len(S["code_strings"])))
    notes.append("Conditions found: %d (Metrics.Conditions=%s)" % (len(S["conds"]), (ir.get("Metrics") or {}).get("Conditions")))
    notes.append("Per-type IR files %s" % ("read from IR_BASE_DIR" if IR_BASE_DIR else "not used (IR_BASE_DIR empty)"))
    notes.append("Images: this script consumes the JSON only; no images were interpreted.")
    notes.append("LLM errors: %s" % (("%d (see log)" % len(LLM_ERRORS)) if LLM_ERRORS else "none"))
    return md_table(["JSON root key", "Status", "Where used"], rows) + "\n\n" + "\n".join("- " + n for n in notes)


def main():
    global CACHE_DIR
    if "<PATH" in IR_JSON_PATH or "<PATH" in OUTPUT_DIR:
        sys.exit("Set IR_JSON_PATH and OUTPUT_DIR at the top of the script.")
    if not DRY_RUN and requests is None:
        sys.exit("pip install requests")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    CACHE_DIR = os.path.join(OUTPUT_DIR, "cache")
    os.makedirs(CACHE_DIR, exist_ok=True)
    log("loading %s" % IR_JSON_PATH)
    with open(IR_JSON_PATH, "r", encoding="utf-8-sig") as f:
        ir = json.load(f)
    log("stage 0: deterministic extraction")
    M = build_model(ir)
    S = scan_ir(ir, M)
    G = build_graphs(ir, M)
    log("types=%d files=%d sql=%d conds=%d" % (len(M["types"]), len(M["files"]), len(S["sql"]), len(S["conds"])))
    log("stage 1: per-type summaries (LLM)")
    TS = summarize_types(M)
    log("stage 2: business-rule mining (LLM)")
    rules_txt, has_conds = business_rules(S)
    log("stage 3: section generation (LLM)")
    sec, meta = build_all(ir, M, S, G, TS, rules_txt, has_conds)
    log("stage 4: executive summary + assembly")
    sec[1] = exec_summary(ir, M, TS, sec, meta)
    # section 15
    a_lines = ["- %s (section %s)" % (t, s.split()[0]) for s, t in ASSUMPTIONS]
    q_lines = ["- **%s** - %s" % (t, d) for t, d in meta["det_oq"]] + ["- %s (section %s)" % (t, s.split()[0]) for s, t in OPEN_QUESTIONS]
    sec[15] = ("### 15.1 Assumptions\n" + ("\n".join(dict.fromkeys(a_lines)) or "none recorded") +
               "\n\n### 15.2 Dependencies\n" + "\n".join("- " + d for d in meta["dep_list"]) +
               "\n\n### 15.3 Open questions (who/what could answer)\n" + "\n".join(dict.fromkeys(q_lines)))
    sec[16] = appendix(ir, M, meta, S)
    out = ["# Project Understanding Document - %s" % ir.get("ProjectName", "VB.NET project"),
           "_Generated %s from `%s` (IR schema %s, IR generated %s). LLM: %s._" % (
               datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), os.path.basename(IR_JSON_PATH), ir.get("SchemaVersion"),
               ir.get("GeneratedAtUtc"), LLM_MODEL),
           "_Conventions: `[JSON: path]` = source in the IR file; `Assumption:` = inferred, not stated; unclear items are in section 15._"]
    for n in range(1, 17):
        out.append("## %d. %s\n\n%s" % (n, SECTION_TITLES[n], sec.get(n, "")))
    out.append("## Coverage Check\n\n" + coverage(ir, M, S, meta))
    path = os.path.join(OUTPUT_DIR, OUTPUT_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(out) + "\n")
    log("DONE -> %s (%d chars)%s" % (path, sum(len(x) for x in out), "; LLM errors: %d" % len(LLM_ERRORS) if LLM_ERRORS else ""))


if __name__ == "__main__":
    main()
