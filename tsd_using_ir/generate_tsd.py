#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_tsd.py  -  VB.NET IR JSON  ->  technical_specification_document.md

Single file, Python 3.8 compatible, only dependency: requests.
Same design and same LLM connection as generate_pud.py.

WHAT THE DOCUMENT IS
  An "as-is" Technical Specification Document (TSD): it specifies HOW the existing system
  is built (architecture, design, data, interfaces, security, deployment, error handling,
  testing) using ONLY the evidence in the IR JSON. Everything inferred is marked
  "Assumption"; everything missing or unclear goes to Open Questions.

HOW IT KEEPS THE LLM EFFICIENT (the whole JSON is NEVER sent to the LLM)
  Stage 0  Python loads the JSON and extracts every fact it can deterministically
           (inventories, metrics, dependency/call graphs, layers, CRUD matrix, error-handling
           statistics, user messages, regex scans, Mermaid diagrams, tables). No LLM involved.
  Stage 1  MAP: one small LLM call per type, fed a compact digest of that type
           (controls grouped by prefix, method signatures, trimmed code snippets).
  Stage 2  MAP: batches of "Conditions" are turned into business rules.
  Stage 3  REDUCE: each TSD section gets its own small LLM call that only receives the
           deterministic facts relevant to that section + one-line type briefs.
           The LLM writes narrative and row-lists; Python builds tables and diagrams.
  Stage 4  Python assembles the final Markdown, writes the Executive Summary last and
           puts it first.
  Every LLM response is cached on disk (OUTPUT_DIR/cache_tsd), so a crash or re-run
  resumes where it stopped and costs nothing for finished calls.

INPUT  : the IR JSON only. Set the two paths below.
OUTPUT : OUTPUT_DIR/technical_specification_document.md
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
OUTPUT_DIR = r"<PATH_TO_OUTPUT_DIRECTORY>"     # e.g. r"/mnt/MyDocuments/.../tsd_output"
IR_BASE_DIR = ""        # optional: folder that contains files/ and types/ (per-item IR). "" = not used
OUTPUT_FILENAME = "technical_specification_document.md"

# --- Local LLM (same pattern as the team's latest_llm_copy.py chat app and generate_pud.py) ---
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
MAX_SECTION_CTX_CHARS = 40000       # budget for type briefs / summaries inside one section prompt
ALGO_METHODS = 3                # how many of the most complex methods get a flowchart
ALGO_CODE_CHARS = 3500          # code sent per algorithm method
SEQ_DIAGRAMS = 5                # max sequence diagrams
SEQ_MAX_MESSAGES = 16           # max messages per sequence diagram

DRY_RUN = os.environ.get("TSD_DRY_RUN") == "1"   # 1 = no LLM calls (stub text), to test the pipeline

# ======================================================================
SYSTEM_PROMPT = (
    "You are a senior solution architect writing ONE section of a Technical Specification Document (TSD) "
    "that documents an EXISTING VB.NET system as-is (how it is built and how it behaves).\n"
    "STRICT RULES:\n"
    "1. Use ONLY the material in the user message. Never invent facts, names, tables, versions or behaviour.\n"
    "2. After every key finding cite its source in square brackets exactly as written in the material, "
    "e.g. [JSON: Types[3].Methods[5]] or [JSON: AnalysisStatus].\n"
    "3. Anything inferred (not literally stated in the material) must start with the word 'Assumption:'.\n"
    "4. If something is unclear, missing or unreadable, do not guess; list it as an open question.\n"
    "5. Write simple, plain Markdown in specification style: short paragraphs and bullets. Do not repeat tables or "
    "diagrams that the material says are already provided; just refer to them.\n"
    "6. Do not write the section heading.\n"
    "7. End your answer with these two blocks, exactly as shown:\n"
    "@@ASSUMPTIONS@@\n- one bullet per assumption you made (or '- none')\n"
    "@@OPEN_QUESTIONS@@\n- one bullet per open question, saying who or what could answer it (or '- none')\n"
)

SYSTEM_PROMPT_TYPE = (
    "You analyse ONE VB.NET type from a legacy project to prepare a technical specification. Use ONLY the digest provided. "
    "Never invent anything. Cite sources in square brackets exactly as written in the digest (e.g. [JSON: Types[3].Methods[5]]). "
    "Prefix inferred statements with 'Assumption:'. If something cannot be determined write 'unclear'. "
    "Be concise and factual."
)

TYPE_TASK = (
    "Summarise this type for a technical specification. Output exactly these lines (write 'none evidenced' when absent):\n"
    "PURPOSE: one sentence.\n"
    "CATEGORY: Form / Data-access / Module-or-helper / Settings-or-resources / Other\n"
    "RESPONSIBILITIES: bullets, each ending with a citation.\n"
    "PUBLIC_INTERFACE: key public methods/properties as 'name(parameters) -> what it does', with citation.\n"
    "KEY_LOGIC: processing steps, algorithms, validations, calculations in the order they happen, with citation.\n"
    "BUSINESS_RULES: bullets (validation, calculations, permissions, status checks) with citation.\n"
    "DATA_ACCESS: SQL, tables, stored procedures, connection handling, with citation.\n"
    "EXTERNAL: files, devices, email, web, printers, 3rd-party controls, with citation.\n"
    "ERROR_HANDLING: Try/Catch, On Error, messages shown to the user, with citation.\n"
    "SECURITY: authentication, authorization, credential or input handling, with citation.\n"
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




# ====================== TSD HELPERS (tables, diagrams) ================
def fit_briefs(M, TS):
    """One-line type briefs, shrunk automatically when the project is large."""
    b = briefs_block(M, TS)
    if len(b) <= MAX_SECTION_CTX_CHARS:
        return b
    out = []
    per = max(80, MAX_SECTION_CTX_CHARS // max(1, len(M["types"])) - 40)
    for T in M["types"]:
        out.append("Types[%d] %s: %s" % (T["i"], T["name"], trunc(purpose_of(TS.get(T["i"])), per)))
    return "\n".join(out)


def fit_summaries(M, TS, only=None):
    n = max(1, len(M["types"]) if only is None else len(only))
    limit = max(200, min(MAX_TYPE_SUMMARY_IN_SECTION, MAX_SECTION_CTX_CHARS // n - 60))
    return full_summaries_block(M, TS, only, limit)


def ask_table(section, tag, task, material, headers, id_prefix=None):
    """LLM writes rows as 'a | b | c' lines; Python builds the Markdown table (robust against bad table syntax)."""
    n = len(headers)
    fmt = ("OUTPUT FORMAT: first, one row per line with exactly %d columns separated by ' | ' (space, pipe, space) in this order: %s. "
           "No header line, no leading or trailing pipe, no markdown table syntax, no numbering. Then, optionally, a short plain-text note."
           % (n, " | ".join(headers)))
    body = ask(section, tag, task + "\n\n" + fmt, material)
    rows, notes = [], []
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("|"):
            s = s.strip("|").strip()
        if not s:
            continue
        parts = [p.strip() for p in s.split(" | ")]
        if len(parts) > n:
            parts = parts[:n - 1] + [" | ".join(parts[n - 1:])]
        if len(parts) == n and not set(s) <= set("-| :") and parts[0].lower() != headers[0].lower():
            rows.append(parts)
        elif s:
            notes.append(s)
    if not rows:
        return body
    hdr = list(headers)
    if id_prefix:
        rows = [["%s-%03d" % (id_prefix, i + 1)] + r for i, r in enumerate(rows)]
        hdr = ["ID"] + hdr
    out = md_table(hdr, rows)
    if notes:
        out += "\n\n" + "\n".join(notes)
    return out


def render_type_summary(txt):
    out = []
    for line in (txt or "").splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^([A-Z_]{4,})\s*:\s*(.*)$", s)
        if m and m.group(1) in ("PURPOSE", "CATEGORY", "RESPONSIBILITIES", "PUBLIC_INTERFACE", "KEY_LOGIC", "BUSINESS_RULES",
                                "DATA_ACCESS", "EXTERNAL", "ERROR_HANDLING", "SECURITY", "UNCLEAR"):
            out.append("- **%s:** %s" % (m.group(1).replace("_", " ").title(), m.group(2)))
        else:
            out.append("  - " + s.lstrip("-*• ").strip())
    return "\n".join(out) or "(no summary)"


def clean_label(s, n=70):
    s = re.sub(r"[\"'`\[\]\(\)\{\}<>|#;&]", " ", str(s))
    return trunc(oneline(s), n).replace('"', "")


def build_flowchart(raw):
    """Parse 'NODE id | text | kind' and 'EDGE a -> b | label' lines into Mermaid. Returns None if unusable."""
    nodes, edges = {}, []
    for line in (raw or "").splitlines():
        s = line.strip()
        m = re.match(r"^NODE\s+([A-Za-z0-9_]+)\s*\|\s*(.+?)\s*\|\s*(start|step|decision|end)\s*$", s, re.I)
        if m:
            nodes[m.group(1)] = (clean_label(m.group(2)), m.group(3).lower())
            continue
        m = re.match(r"^EDGE\s+([A-Za-z0-9_]+)\s*->\s*([A-Za-z0-9_]+)\s*(?:\|\s*(.*))?$", s, re.I)
        if m:
            edges.append((m.group(1), m.group(2), clean_label(m.group(3) or "", 30)))
    edges = [e for e in edges if e[0] in nodes and e[1] in nodes]
    if len(nodes) < 2 or not edges:
        return None
    lines = ["flowchart TD"]
    for k, (lab, kind) in nodes.items():
        if kind == "decision":
            lines.append('  n_%s{"%s"}' % (k, lab))
        elif kind in ("start", "end"):
            lines.append('  n_%s(["%s"])' % (k, lab))
        else:
            lines.append('  n_%s["%s"]' % (k, lab))
    for a, b, lab in edges:
        lines.append(('  n_%s -->|"%s"| n_%s' % (a, lab, b)) if lab else ("  n_%s --> n_%s" % (a, b)))
    return "```mermaid\n" + "\n".join(lines) + "\n```"


def edge_graph(edge_map, title):
    lines = ["flowchart LR"]
    used, n_edges = set(), 0
    flat = sorted(((a, b, n) for a, c in edge_map.items() for b, n in c.items()), key=lambda z: -z[2])[:120]
    for a, b, n in flat:
        used.update([a, b])
        lines.append("  %s -->|%d| %s" % (mid(a), n, mid(b)))
        n_edges += 1
    for u in sorted(used):
        lines.insert(1, '  %s["%s"]' % (mid(u), mlabel(type_short(u))))
    return ("```mermaid\n" + "\n".join(lines) + "\n```") if n_edges else "No internal type-to-type edges were found. [JSON: %s]" % title


# ====================== TSD-SPECIFIC FACTS (no LLM) ====================
DBAPI_RE = re.compile(r"(ADODB\.|\bSqlConnection\b|\bSqlCommand\b|\bSqlDataAdapter\b|\bOleDbConnection\b|\bOleDbCommand\b|"
                      r"\bOdbcConnection\b|\bOracleConnection\b|\bMySqlConnection\b|\bDataAdapter\b|\bExecuteReader\b|"
                      r"\bExecuteNonQuery\b|\bExecuteScalar\b)")
MSG_RE = re.compile(r'\b(?:MsgBox|MessageBox\.Show)\s*\(\s*"((?:[^"]|"")*)"', re.I)
EVENT_RE = re.compile(r"_(Click|Load|KeyPress|KeyDown|Tick|TextChanged|SelectedIndexChanged|CheckedChanged|CellContentClick|FormClosing|ToggleStateChanged|NewFrame)\b")


def tidx_of(p):
    m = re.match(r"Types\[(\d+)\]", p)
    return int(m.group(1)) if m else None


def midx_of(p):
    m = re.match(r"Types\[\d+\]\.Methods\[(\d+)\]", p)
    return int(m.group(1)) if m else None


def clean_table_name(x):
    x = x.replace("[", "").replace("]", "").strip(" ,;()\"")
    x = re.sub(r"(?i)^dbo\.", "", x)
    return x


def scan_tsd(ir, M, S, G):
    X = {}
    code_by_type = defaultdict(list)
    for p, s in S["code_strings"]:
        code_by_type[tidx_of(p)].append((p, s))
    has_sql = set(tidx_of(p) for p, _ in S["sql"])
    has_db = set(ti for ti, items in code_by_type.items() if any(DBAPI_RE.search(s) for _, s in items))
    X.update(code_by_type=code_by_type, has_sql=has_sql, has_db=has_db)

    # layers (deterministic heuristic; label is an Assumption in the document)
    layer = {}
    for T in M["types"]:
        if T["is_form"]:
            layer[T["i"]] = "Presentation"
        elif T["i"] in has_sql or T["i"] in has_db:
            layer[T["i"]] = "Data access"
        else:
            layer[T["i"]] = "Business / shared logic"
    X["layer"] = layer

    # user-visible messages
    msgs, seen = [], set()
    for p, s in S["code_strings"]:
        for lit in MSG_RE.findall(s):
            key = (tidx_of(p), lit)
            if key not in seen and len(lit.strip()) > 1:
                seen.add(key)
                msgs.append((p, trunc(oneline(lit.replace('""', '"')), 160)))
    X["msgs"] = msgs[:150]

    # error handling statistics per type
    eh = {}
    for ti, items in code_by_type.items():
        txt = "\n".join(s for _, s in items)
        eh[ti] = {
            "try": len(re.findall(r"\bTry\b", txt)), "catch": len(re.findall(r"\bCatch\b", txt)),
            "onerror": len(re.findall(r"\bOn\s+Error\b", txt, re.I)), "throw": len(re.findall(r"\bThrow\b", txt)),
            "emptycatch": len(re.findall(r"(?is)\bCatch\b[^\n]*\n\s*End\s+Try", txt)),
            "finally": len(re.findall(r"\bFinally\b", txt)), "using": len(re.findall(r"\bUsing\b", txt)),
        }
    X["eh"] = eh

    # CRUD matrix from SQL literals
    crud = defaultdict(lambda: defaultdict(set))
    procs = defaultdict(set)
    for p, lit in S["sql"]:
        ti = tidx_of(p)
        low = lit.lower().lstrip(" `'\"(")
        names = []
        if low.startswith("select"):
            op = "R"
            names = re.findall(r"\b(?:from|join)\s+([\[\]\w\.]+)", lit, re.I)
        elif low.startswith("insert"):
            op = "C"
            names = re.findall(r"\binto\s+([\[\]\w\.]+)", lit, re.I)
        elif low.startswith("update"):
            op = "U"
            names = re.findall(r"\bupdate\s+([\[\]\w\.]+)", lit, re.I)
        elif low.startswith("delete"):
            op = "D"
            names = re.findall(r"\bfrom\s+([\[\]\w\.]+)", lit, re.I)
        elif low.startswith("exec"):
            for nm in re.findall(r"\bexec(?:ute)?\s+([\[\]\w\.]+)", lit, re.I):
                procs[clean_table_name(nm)].add(ti)
            continue
        else:
            op = "R"
            names = re.findall(r"\b(?:from|join|into)\s+([\[\]\w\.]+)", lit, re.I)
        for nm in names:
            nm = clean_table_name(nm)
            if nm and nm.lower() not in ("select", "where", "set", "values", "the", "a") and not nm[0].isdigit():
                crud[nm][ti].add(op)
    X["crud"] = crud
    X["procs"] = procs

    # methods inventory (generic: any dict shape)
    mrows = []
    for T in M["types"]:
        for j, m in enumerate(T["methods"]):
            if not isinstance(m, dict):
                continue
            span = gi(m, "SourceSpan")
            lines_ = 0
            if isinstance(span, dict):
                try:
                    lines_ = int(gi(span, "EndLine", default=0)) - int(gi(span, "StartLine", default=0)) + 1
                except (TypeError, ValueError):
                    lines_ = 0
            conds = gi(m, "Conditions")
            nc = len(conds) if isinstance(conds, list) else 0
            code = gi(m, "NormalizedCode", "RawCode", "Body", "Code")
            mid_ = gi(m, "Id", default="")
            name = T["mnames"][j] if j < len(T["mnames"]) else ""
            mrows.append({"ti": T["i"], "mi": j, "id": mid_, "name": name, "lines": lines_, "conds": nc,
                          "vis": gi(m, "Visibility", "Accessibility", default=""),
                          "code": code if isinstance(code, str) else "", "score": lines_ + 8 * nc + (len(code) // 80 if isinstance(code, str) else 0)})
    X["methods"] = mrows
    X["sequences"] = build_sequences(ir, M)
    return X


def build_sequences(ir, M):
    """Deterministic Mermaid sequence diagrams from CallGraph.Edges between methods of known types."""
    cg = ir.get("CallGraph") or {}
    ids = set(M["by_id"])
    out_calls = defaultdict(list)
    for idx, e in enumerate(cg.get("Edges") or []):
        frm, to = gi(e, "From"), gi(e, "To")
        if not (isinstance(frm, str) and isinstance(to, str) and frm.startswith("M:") and to.startswith("M:")):
            continue
        if method_type(frm) in ids and method_type(to) in ids:
            try:
                ln = int(gi(e, "Line", default=0) or 0)
            except (TypeError, ValueError):
                ln = 0
            out_calls[frm].append((ln, to, idx))

    def reach(start):
        seen, stack, n = {start}, [start], 0
        depth = {start: 0}
        while stack:
            cur = stack.pop()
            for _, to, _ in out_calls.get(cur, []):
                n += 1
                if to not in seen and depth[cur] < 2:
                    seen.add(to)
                    depth[to] = depth[cur] + 1
                    stack.append(to)
        return n

    cands = []
    for m in out_calls:
        n = reach(m)
        if n >= 3:
            nm = method_name_from_id(m)
            boost = 5 if (EVENT_RE.search(nm) or "main" in nm.lower() or "start" in nm.lower()) else 0
            cands.append((n + boost, m))
    cands.sort(reverse=True)
    chosen, per_type = [], Counter()
    for _, m in cands:
        t = method_type(m)
        if per_type[t] >= 2:
            continue
        per_type[t] += 1
        chosen.append(m)
        if len(chosen) >= SEQ_DIAGRAMS:
            break
    diagrams = []
    for entry in chosen:
        parts, order = {}, []

        def part(t):
            if t not in parts:
                parts[t] = "P%d" % len(parts)
                order.append(t)
            return parts[t]

        msgs, edge_idx, visited = [], [], {entry}

        def walk(mth, depth):
            for ln, to, idx in sorted(out_calls.get(mth, [])):
                if len(msgs) >= SEQ_MAX_MESSAGES:
                    return
                a, b = part(method_type(mth)), part(method_type(to))
                label = re.sub(r"[#;:]", "", method_name_from_id(to)) or "call"
                msgs.append("  %s->>%s: %s" % (a, b, label))
                edge_idx.append(idx)
                if depth < 2 and to not in visited:
                    visited.add(to)
                    walk(to, depth + 1)

        part(method_type(entry))
        walk(entry, 0)
        if not msgs:
            continue
        head = ["sequenceDiagram"] + ["  participant %s as %s" % (parts[t], mlabel(type_short(t))) for t in order]
        diagrams.append({"entry": entry, "mermaid": "```mermaid\n" + "\n".join(head + msgs) + "\n```", "edges": edge_idx[:8]})
    return diagrams


def debt_rows_tsd(ir, M, S, G, X, optmap, asm, A, files, vendors_ext):
    rows = []
    if optmap.get("OptionStrict", "").lower() == "off":
        rows.append(["Option Strict Off (implicit conversions / late binding allowed)", "OptionStrict=Off", "High", "[JSON: Options]"])
    if optmap.get("OptionExplicit", "").lower() == "off":
        rows.append(["Option Explicit Off", "OptionExplicit=Off", "High", "[JSON: Options]"])
    for d in S["debt"]:
        if d["count"]:
            rows.append([d["name"], "%d occurrences; top: %s" % (d["count"], ", ".join("Types[%s] x%d" % kv for kv in d["top"])),
                         d["rating"], "[JSON: Types[].*%s]" % S["code_key_used"]])
    unres_asm = [a for a in asm if gi(a, "Resolved") is False]
    if unres_asm:
        rows.append(["Assembly references not resolved (HintPaths point outside the project)", "%d of %d unresolved" % (len(unres_asm), len(asm)),
                     "High", "[JSON: AssemblyReferences]"])
    if A.get("Authoritative") is False:
        rows.append(["Analysis not authoritative: semantic model incomplete", "CompilationErrors=%s; SemanticCompilation=%s" % (
            A.get("CompilationErrors"), A.get("SemanticCompilation")), "High", "[JSON: AnalysisStatus]"])
    for v, d in sorted(vendors_ext.items()):
        vers = sorted(set(version_from_dep(x) for x in d if version_from_dep(x)))
        if vers:
            rows.append(["Pinned 3rd-party version(s) of %s: %s (Assumption: may be outdated or unsupported)" % (v, ", ".join(vers)),
                         "%d assemblies" % len(d), "Medium", "[JSON: ExternalDependencies]"])
    rows.append(["Target framework %s (Assumption: older framework, verify support status externally)" % ir.get("TargetFramework"), "",
                 "Medium", "[JSON: TargetFramework]"])
    tests = [f for f in files if "test" in str(gi(f, "RelativePath", default="")).lower()]
    rows.append(["Automated tests", "no test files found in Files[]" if not tests else "%d test-like files" % len(tests),
                 "Medium" if not tests else "Low", "[JSON: Files[].RelativePath]"])
    if S["dups"]:
        rows.append(["Duplicated method bodies (identical normalized code)", "%d groups, e.g. %s" % (
            len(S["dups"]), "; ".join(", ".join(g[:3]) for g in S["dups"][:3])), "Medium", "[JSON: Types[].Methods[]]"])
    fan_in = sorted(((len(v), M["by_id"][k]["name"]) for k, v in G["in_deps"].items() if k in M["by_id"]), reverse=True)[:5]
    if fan_in:
        rows.append(["Highly depended-upon types (coupling hot-spots)", ", ".join("%s (%d)" % (n, c) for c, n in fan_in), "Medium",
                     "[JSON: TypeDependencyGraph.Edges]"])
    risky = [ti for ti in (X["has_sql"] | X["has_db"]) if ti is not None and X["eh"].get(ti, {}).get("try", 0) == 0 and not X["eh"].get(ti, {}).get("onerror")]
    if risky:
        rows.append(["Types with database access but no Try/On Error handling", ", ".join("Types[%d]" % i for i in sorted(risky)[:10]),
                     "Medium", "[JSON: Types[].*%s]" % S["code_key_used"]])
    return rows


# ============================ STAGE 3: SECTIONS =======================
def fact_json(d):
    return json.dumps(d, ensure_ascii=False, indent=1, default=str)


def build_all(ir, M, S, G, X, TS, rules_txt, has_conds):
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
    sec, diagrams = {}, []
    briefs = fit_briefs(M, TS)
    forms = [x for x in T if x["is_form"]]
    non_forms = [x for x in T if not x["is_form"]]
    layer = X["layer"]

    unres_groups = Counter()
    for s in unres:
        unres_groups[re.split(r"[.(\s]", s)[0] or s] += 1
    vendors_ext = defaultdict(list)
    for d in ext:
        vendors_ext[vendor_from_dep(d)].append(d)
    layer_counts = Counter(layer.values())
    core_facts = {
        "ProjectName": ir.get("ProjectName"), "ProjectFile": ir.get("ProjectFile"), "Language": ir.get("Language"),
        "RootNamespace": ir.get("RootNamespace"), "TargetFramework": ir.get("TargetFramework"),
        "ProjectSdk": ir.get("ProjectSdk"), "LanguageVersion": ir.get("LanguageVersion"), "Options": opts,
        "Metrics": MET, "AnalysisStatus": dict((k, v) for k, v in A.items() if k not in ("WorkspaceDiagnostics", "MissingReferences")),
        "WorkspaceDiagnostics_count": len(A.get("WorkspaceDiagnostics") or []),
        "MissingReferences_count": len(A.get("MissingReferences") or []),
        "forms_count": len(forms), "other_types_count": len(non_forms),
        "layer_counts_heuristic": dict(layer_counts),
        "ExternalDependency_vendors": dict((k, len(v)) for k, v in vendors_ext.items()),
        "ConfigurationArtifacts_by_kind": dict(Counter(gi(c, "Kind", default="?") for c in cfg)),
        "UnresolvedSymbol_roots_top": unres_groups.most_common(25),
        "sql_literals_found": len(S["sql"]), "connection_strings_found": len(S["conns"]),
        "tables_referenced_in_sql": sorted(X["crud"].keys())[:60], "user_messages_found": len(X["msgs"]),
    }
    core_json = "ALL FACTS BELOW ARE FROM THE JSON; cite as [JSON: <key>].\n" + fact_json(core_facts)
    debt = debt_rows_tsd(ir, M, S, G, X, optmap, asm, A, files, vendors_ext)
    debt_ctx = "\n".join("%s | %s | %s | %s" % tuple(r) for r in debt)

    # ---------- 2 Overview ----------
    proj_type = "WinForms (MyType=WindowsForms) [JSON: Options]" if optmap.get("MyType") == "WindowsForms" else "Not determinable from Options [JSON: Options]"
    ov_table = md_table(["Item", "Value", "Source"], [
        ["Project name", ir.get("ProjectName"), "[JSON: ProjectName]"],
        ["Project file", ir.get("ProjectFile"), "[JSON: ProjectFile]"],
        ["Language / version", "%s / %s" % (ir.get("Language"), ir.get("LanguageVersion") or "not recorded"), "[JSON: Language, LanguageVersion]"],
        ["Root namespace", ir.get("RootNamespace"), "[JSON: RootNamespace]"],
        ["Target framework", ir.get("TargetFramework"), "[JSON: TargetFramework]"],
        ["Project type", proj_type, "[JSON: Options]"],
        ["VB options", ", ".join(opts), "[JSON: Options]"],
        ["Size", "%s files, %s types, %s methods, %s fields" % (MET.get("Files"), MET.get("Types"), MET.get("Methods"), MET.get("Fields")), "[JSON: Metrics]"],
        ["IR generated at", ir.get("GeneratedAtUtc"), "[JSON: GeneratedAtUtc]"],
    ])
    purpose = ask("2 Overview", "sec2-purpose",
                  "Write two short subsections: 'Purpose' (what this specification covers and what the system appears to do) and "
                  "'Background' (what the evidence says about the project: language, framework, UI technology, size, age of the stack). "
                  "Anything beyond type/form names and their summaries is an Assumption.",
                  core_json + "\n\nTYPE BRIEFS:\n" + briefs)
    scope = ask("2 Overview", "sec2-scope",
                "Write 'In scope' (modules/features evidenced by types and forms, as bullets), 'Out of scope / not evidenced' "
                "(e.g. web services or integrations that are absent per Metrics) and 'Goals and non-goals' (goals the design evidently "
                "serves; non-goals = capabilities that are absent). Judgements are Assumptions.",
                core_json + "\n\nTYPE BRIEFS:\n" + briefs)
    gloss = ask_table("2 Overview", "sec2-glossary",
                      "Create a glossary of domain and technical terms that appear in type, form, field or method names. "
                      "Meanings that are not explicit in the material must start with 'Assumption:'.",
                      "TYPE BRIEFS:\n" + briefs + "\n\nFORM CONTROL/FIELD SAMPLE:\n" +
                      "\n".join("Types[%d]: %s" % (x["i"], ", ".join(str(gi(f, "Name", default="")) for f in x["fields"][:25] if isinstance(f, dict))) for x in T[:60]),
                      ["Term", "Meaning", "Source"])
    sec[2] = ("### 2.1 Document control facts\n" + ov_table + "\n\n### 2.2 Purpose and background\n" + purpose +
              "\n\n### 2.3 Scope, goals and non-goals\n" + scope + "\n\n### 2.4 Definitions and glossary\n" + gloss)

    # ---------- 3 Requirements ----------
    fr = ask_table("3 Requirements", "sec3-fr",
                   "Derive the FUNCTIONAL requirements the implemented system satisfies, as 'The system provides/validates/stores ...' statements, "
                   "one per behaviour. Basis column = 'Evidenced' if literally in the material, otherwise 'Assumption'. Keep the citation.",
                   "TYPE SUMMARIES:\n" + fit_summaries(M, TS) + ("\n\nEXTRACTED RULES:\n" + trunc(rules_txt, 20000) if rules_txt else ""),
                   ["Requirement (derived from existing behaviour)", "Source", "Basis"], id_prefix="FR")
    nfr = ask_table("3 Requirements", "sec3-nfr",
                    "Derive NON-FUNCTIONAL characteristics that the evidence shows (platform, performance indicators, security, availability, "
                    "maintainability, compatibility). Category examples: Platform, Performance, Security, Reliability, Maintainability, Compatibility. "
                    "Use 'not evidenced' instead of guessing numbers.",
                    core_json + "\n\nDEBT TABLE (item | evidence | rating | source):\n" + debt_ctx,
                    ["Category", "Characteristic", "Source", "Basis"], id_prefix="NFR")
    cons = [["Runtime / framework", ir.get("TargetFramework"), "[JSON: TargetFramework]"],
            ["UI platform", "Windows-only desktop (Windows Forms)" if optmap.get("MyType") == "WindowsForms" else "not determinable", "[JSON: Options]"],
            ["Language settings", ", ".join(o for o in opts if o.startswith("Option")) or "none recorded", "[JSON: Options]"],
            ["Third-party libraries", ", ".join(sorted(vendors_ext)) or "none", "[JSON: ExternalDependencies]"],
            ["Reference resolution", "%d of %d assembly references unresolved" % (sum(1 for a in asm if gi(a, "Resolved") is False), len(asm)), "[JSON: AssemblyReferences]"],
            ["Analysis confidence", "Authoritative=%s, CompilationErrors=%s" % (A.get("Authoritative"), A.get("CompilationErrors")), "[JSON: AnalysisStatus]"]]
    sec[3] = ("### 3.1 Functional requirements\n" + fr + "\n\n### 3.2 Non-functional requirements\n" + nfr +
              "\n\n### 3.3 Constraints\n" + md_table(["Constraint", "Value", "Source"], cons) +
              "\n\n### 3.4 Assumptions\nAll assumptions are collected in section 13.4.")

    # ---------- 4 Architecture ----------
    lay_edges = Counter()
    for x in T:
        for t2, n in list(G["out_deps"].get(x["id"], {}).items()) + list(G["call_t"].get(x["id"], {}).items()):
            tt = M["by_id"].get(t2)
            if tt and layer[tt["i"]] != layer[x["i"]]:
                lay_edges[(layer[x["i"]], layer[tt["i"]])] += n
    arch = ["flowchart TB"]
    lnames = {"Presentation": "L_PRES", "Business / shared logic": "L_BIZ", "Data access": "L_DATA"}
    for ln, nid in lnames.items():
        members = [x["name"] for x in T if layer[x["i"]] == ln]
        if members:
            arch.append('  %s["%s (%d types)<br/>%s"]' % (nid, ln, len(members), mlabel(trunc(", ".join(members[:5]), 90))))
    ven = Counter()
    for x in T:
        for v, n in G["vendor_use"].get(x["id"], {}).items():
            ven[v] += n
    for v, n in ven.most_common(8):
        arch.append('  %s(["%s (unresolved library)"])' % (mid("V" + v), mlabel(v)))
        for ln, nid in lnames.items():
            cnt = sum(G["vendor_use"].get(x["id"], {}).get(v, 0) for x in T if layer[x["i"]] == ln)
            if cnt:
                arch.append("  %s -->|%d refs| %s" % (nid, cnt, mid("V" + v)))
    for (a, b), n in lay_edges.most_common(12):
        arch.append("  %s -->|%d refs| %s" % (lnames[a], n, lnames[b]))
    if X["has_sql"] or X["has_db"]:
        arch.append('  DB[("Database (engine not evidenced)")]')
        for ln, nid in lnames.items():
            k = sum(1 for x in T if layer[x["i"]] == ln and (x["i"] in X["has_sql"] or x["i"] in X["has_db"]))
            if k:
                arch.append("  %s -->|%d types use SQL/DB API| DB" % (nid, k))
    arch_mm = "```mermaid\n" + "\n".join(arch) + "\n```"
    diagrams.append("Layered architecture diagram: heuristic layer per type (forms = Presentation; types with SQL literals or ADO/SqlClient API calls = Data access; others = Business/shared), TypeDependencyGraph.Edges, CallGraph.Edges, UnresolvedSymbols")
    comp_rows = []
    for x in T:
        outs, ins, cout = G["out_deps"].get(x["id"], {}), G["in_deps"].get(x["id"], {}), G["call_t"].get(x["id"], {})
        deps = sorted(set(type_short(k) for k in list(outs) + list(cout)))
        dependents = sorted(set(type_short(k) for k in ins) | set(type_short(k) for k, v in G["call_t"].items() if x["id"] in v))
        comp_rows.append(["Types[%d]" % x["i"], x["name"], layer[x["i"]], purpose_of(TS.get(x["i"])), x["loc"],
                          trunc(", ".join(deps), 110) or "-", trunc(", ".join(dependents), 110) or "-",
                          "%s (methods=%d, conditions=%d)" % (complexity(x), len(x["methods"]), x["nconds"])])
    comp_tbl = md_table(["Source", "Component", "Layer*", "Responsibility", "LOC", "Depends on", "Used by", "Complexity*"], comp_rows)
    dep_mm = edge_graph(G["out_deps"], "TypeDependencyGraph.Edges")
    call_mm = edge_graph(G["call_t"], "CallGraph.Edges")
    diagrams.append("Type dependency graph: TypeDependencyGraph.Edges where To is a known Types[].Id (top 120 edges)")
    diagrams.append("Call graph (type level): CallGraph.Edges between methods of different known types (top 120 edges)")
    ar_llm = ask("4 Architecture", "sec4",
                 "Specify the architecture: the layers and what each contains, how components interact (refer to the diagrams, already provided), "
                 "architectural patterns evidenced (code-behind with .Designer.vb partial classes, global modules/state, data access inside forms, etc.), "
                 "entry points / startup flow (only what is evidenced) and cross-cutting concerns. Layers are a script heuristic; say so.",
                 core_json + "\n\nTYPE BRIEFS:\n" + briefs + "\n\nLAYER EDGES (from layer, to layer, refs): %s\n\nENTRY-POINT CANDIDATES (by name):\n%s" % (
                     [(a, b, n) for (a, b), n in lay_edges.most_common(12)],
                     fit_summaries(M, TS, set(x["i"] for x in T if "MyApplication" in x["full"] or any(n.lower() in ("main", "application_startup") for n in x["mnames"]))) or "none found by name"))
    sec[4] = ("### 4.1 Layered architecture\n" + arch_mm +
              "\n\n*Layer = script heuristic (Assumption): forms -> Presentation; types containing SQL literals or ADO/SqlClient calls -> Data access; the rest -> Business / shared logic.\n" +
              "\n\n### 4.2 Component inventory\n" + comp_tbl +
              "\n\n*Complexity = script heuristic (Assumption) from LOC, method count and condition count. LOC = lines of the files the type is declared in (partial files counted for each type). [JSON: Files[].Lines]\n" +
              "\n\n### 4.3 Type dependency graph\n" + dep_mm + "\n\n### 4.4 Call graph (type level)\n" + call_mm +
              "\n\nGraph status: TypeDependencyGraph=%s, CallGraph=%s; internal call edges=%s, unresolved edges=%s [JSON: AnalysisStatus, Metrics]" % (
                  A.get("TypeDependencyGraph"), A.get("CallGraph"), MET.get("InternalEdges"), MET.get("UnresolvedEdges")) +
              "\n\n### 4.5 Architecture narrative\n" + ar_llm)

    # ---------- 5 Technology stack ----------
    stack_rows = [
        [".NET Framework / runtime", ir.get("TargetFramework"), "[JSON: TargetFramework]"],
        ["Language", "%s (LanguageVersion: %s)" % (ir.get("Language"), ir.get("LanguageVersion") or "not recorded"), "[JSON: Language]"],
        ["UI framework", "Windows Forms" if optmap.get("MyType") == "WindowsForms" else "unknown", "[JSON: Options]"],
        ["Data access", "ADODB referenced in code" if unres_groups.get("ADODB") else ("ADO.NET-style API calls in %d types" % len(X["has_db"]) if X["has_db"] else "no ADO/ORM evidence"), "[JSON: UnresolvedSymbols, Types]"],
        ["NuGet packages", "none declared" if not ir.get("PackageReferences") else str(len(ir.get("PackageReferences"))), "[JSON: PackageReferences]"],
        ["Project references", "none" if not ir.get("ProjectReferences") else str(len(ir.get("ProjectReferences"))), "[JSON: ProjectReferences]"],
        ["Build / analysis loader", A.get("LoaderMode"), "[JSON: AnalysisStatus.LoaderMode]"],
    ]
    for v, deps in sorted(vendors_ext.items()):
        vers = sorted(set(version_from_dep(d) for d in deps if version_from_dep(d)))
        stack_rows.append(["3rd-party: " + v, "%d assemblies%s" % (len(deps), (", version(s) " + ", ".join(vers)) if vers else ""), "[JSON: ExternalDependencies]"])
    asm_rows = [[gi(a, "Name", default="?"), trunc(gi(a, "HintPath", default=""), 90), gi(a, "Resolved", default="")] for a in asm[:60]]
    st_llm = ask("5 Technology Stack", "sec5",
                 "Comment on the technology stack: what is evidenced, COM/ActiveX evidence (say none if none), unresolved or legacy components, and "
                 "version risks. Tables are already provided.",
                 core_json + "\n\nUNRESOLVED SYMBOL ROOTS:\n" + str(unres_groups.most_common(40)) +
                 "\n\nEXTERNAL DEPENDENCIES: " + trunc(", ".join(ext), 2500))
    sec[5] = ("### 5.1 Stack summary\n" + md_table(["Layer", "Technology / version", "Source"], stack_rows) +
              "\n\n### 5.2 Assembly references (first 60 of %d) [JSON: AssemblyReferences]\n" % len(asm) +
              (md_table(["Name", "HintPath", "Resolved"], asm_rows) if asm_rows else "none") +
              "\n\n### 5.3 Unresolved symbol roots (top 25) [JSON: UnresolvedSymbols]\n" +
              md_table(["Root", "Occurrences"], unres_groups.most_common(25)) + "\n\n### 5.4 Notes\n" + st_llm)

    # ---------- 6 Detailed design ----------
    type_tbl = md_table(["Source", "Type", "Kind", "Base class", "Files", "LOC", "Fields", "Methods", "Category", "Responsibility"],
                        [["Types[%d]" % x["i"], x["full"], x["kind"], x["base"] or "-", ", ".join(x["src"]), x["loc"], len(x["fields"]),
                          len(x["methods"]), category_of(TS.get(x["i"])), purpose_of(TS.get(x["i"]))] for x in T])
    cards = []
    for x in T:
        ctrls = sum(1 for f in x["fields"] if isinstance(f, dict) and str(gi(f, "SourceFile", default="")).lower().endswith(".designer.vb"))
        cards.append("#### Types[%d] %s (%s, %s)\n*Files: %s | Base: %s | Interfaces: %s | Fields: %d (designer controls: %d) | Methods: %d | LOC: %d [JSON: Types[%d]]*\n\n%s" % (
            x["i"], x["full"], x["kind"], layer[x["i"]], ", ".join(x["src"]) or "n/a", x["base"] or "unknown", ", ".join(x["ifaces"]) or "none",
            len(x["fields"]), ctrls, len(x["methods"]), x["loc"], x["i"], render_type_summary(TS.get(x["i"]))))
    top = sorted(X["methods"], key=lambda r: -r["score"])[:25]
    km_tbl = md_table(["Source", "Method", "Lines", "Conditions", "Visibility", "Signature"],
                      [["Types[%d].Methods[%d]" % (r["ti"], r["mi"]), r["name"], r["lines"] or "?", r["conds"], r["vis"] or "-",
                        trunc(r["id"][2:] if r["id"].startswith("M:") else r["id"], 110)] for r in top]) if top else "No method-level data in the JSON (see Open Questions)."
    if has_conds and rules_txt:
        rules_tbl = ask_table("6 Detailed Design", "sec6-rules",
                              "Consolidate these extracted business rules. Merge duplicates, keep citations. Do not add rules that are not in the material.",
                              "EXTRACTED RULES:\n" + trunc(rules_txt, 60000), ["Rule", "Where implemented (type.method)", "Source"], id_prefix="BR")
    else:
        rules_tbl = ("No `Conditions` entries were found inside Types[].Methods[] in this JSON, so rules cannot be listed from conditions. "
                     "Rules mentioned in type summaries are in 6.2. [JSON: Metrics.Conditions=%s]" % MET.get("Conditions"))
    # key algorithms with flowcharts
    algo_parts = []
    cand = [r for r in sorted(X["methods"], key=lambda r: -r["score"]) if r["code"] and r["name"] not in ("InitializeComponent", "Dispose")][:ALGO_METHODS]
    for r in cand:
        raw = ask("6 Detailed Design", "algo-%d-%d" % (r["ti"], r["mi"]),
                  "Describe this method's algorithm. Output exactly:\nSUMMARY: 2-3 sentences with citation.\nINPUTS_OUTPUTS: parameters, return, data touched, with citation.\n"
                  "Then the control flow as lines (max 14 nodes), exactly in this format and nothing else:\n"
                  "NODE id | short text without punctuation | start or step or decision or end\nEDGE id -> id | optional label\n"
                  "ids are short alphanumerics such as a1, a2.",
                  "METHOD: [JSON: Types[%d].Methods[%d]] %s in %s | lines=%s conditions=%d\nCODE:\n%s" % (
                      r["ti"], r["mi"], r["name"], M["types"][r["ti"]]["full"], r["lines"] or "?", r["conds"], trunc(r["code"], ALGO_CODE_CHARS)))
        fc = build_flowchart(raw)
        text_lines = []
        for l in raw.splitlines():
            if not l.strip() or re.match(r"^\s*(NODE|EDGE)\b", l.strip(), re.I):
                continue
            mm_ = re.match(r"^\s*(SUMMARY|INPUTS_OUTPUTS)\s*:\s*(.*)$", l)
            text_lines.append(("- **%s:** %s" % (mm_.group(1).replace("_", " / ").title(), mm_.group(2))) if mm_ else l)
        if fc:
            diagrams.append("Flowchart of Types[%d].Methods[%d]: LLM-extracted NODE/EDGE list rendered by Python (verify against code)" % (r["ti"], r["mi"]))
        algo_parts.append("#### %s [JSON: Types[%d].Methods[%d]]\n%s\n\n%s" % (
            M["types"][r["ti"]]["name"] + "." + r["name"], r["ti"], r["mi"], "\n".join(text_lines), fc or "(no usable flowchart produced for this method)"))
    algos = "\n\n".join(algo_parts) if algo_parts else "No method with source code was available in the JSON, so no algorithm flow can be specified. [JSON: Types[].Methods[]]"
    seqs = []
    for k, sd in enumerate(X["sequences"]):
        seqs.append("#### Sequence %d: %s\n%s\n\nSource: [JSON: CallGraph.Edges[%s]]" % (
            k + 1, method_label(sd["entry"]), sd["mermaid"], ",".join(str(i) for i in sd["edges"])))
        diagrams.append("Sequence diagram %d: CallGraph.Edges starting at %s (depth 2, ordered by Line)" % (k + 1, sd["entry"]))
    seq_txt = "\n\n".join(seqs) if seqs else "No sequence diagrams could be derived: no internal method-to-method edges between known types in CallGraph.Edges. [JSON: CallGraph, Metrics.InternalEdges=%s]" % MET.get("InternalEdges")
    sec[6] = ("### 6.1 Module / class overview\n" + type_tbl + "\n\n### 6.2 Class design (one card per type)\n" + "\n\n".join(cards) +
              "\n\n### 6.3 Key methods (top 25 by size and conditions; script heuristic)\n" + km_tbl +
              "\n\n### 6.4 Business logic and rules\n" + rules_tbl + "\n\n### 6.5 Key algorithms and flowcharts\n" + algos +
              "\n\n### 6.6 Sequence diagrams (from call graph)\n" + seq_txt)

    # ---------- 7 Data design ----------
    sql_tbl = md_table(["Source", "SQL literal (trimmed)"], [[p, "`" + s.replace("`", "'") + "`"] for p, s in S["sql"][:40]]) if S["sql"] else "No SQL literals were detected in code strings."
    conn_tbl = md_table(["Source", "Connection string (secrets masked)"], [[p, "`" + s + "`"] for p, s in S["conns"]]) if S["conns"] else "No connection strings were detected in code strings."
    sp_tbl = md_table(["Source", "Value"], [[p, v] for p, k, v in S["kv_hits"][:30]]) if S["kv_hits"] else "No stored-procedure/SQL keys were found in the JSON structure."
    crud_rows = []
    for tbl, per in sorted(X["crud"].items()):
        crud_rows.append([tbl, "; ".join("%s: %s" % (M["types"][ti]["name"] if ti is not None and ti < len(M["types"]) else "?", "".join(sorted(ops))) for ti, ops in sorted(per.items(), key=lambda z: -1 if z[0] is None else z[0]))])
    crud_tbl = md_table(["Table (from SQL literals)", "Accessed by (C=insert R=select U=update D=delete)"], crud_rows[:80]) if crud_rows else "No table names could be extracted from SQL literals."
    proc_rows = [[n, ", ".join(M["types"][ti]["name"] for ti in sorted(t for t in tis if t is not None) if ti < len(M["types"]))] for n, tis in sorted(X["procs"].items())]
    dacc = [[("Types[%d]" % x["i"]), x["name"], layer[x["i"]], "yes" if x["i"] in X["has_sql"] else "-", "yes" if x["i"] in X["has_db"] else "-"] for x in T if x["i"] in X["has_sql"] or x["i"] in X["has_db"]]
    dacc_tbl = md_table(["Source", "Type", "Layer*", "SQL literals", "DB API calls"], dacc) if dacc else "No type with SQL literals or DB API calls was detected."
    data_ctx = (core_json + "\n\nSQL LITERALS (%d total, first 120 shown):\n" % len(S["sql"]) + "\n".join("%s %s" % (p, s) for p, s in S["sql"][:120]) +
                "\n\nSTRUCTURED SQL/STORED-PROC KEYS:\n" + "\n".join("%s %s" % (p, v) for p, k, v in S["kv_hits"][:60]) +
                "\n\nCONNECTION STRINGS (masked):\n" + "\n".join("%s %s" % (p, s) for p, s in S["conns"]) +
                "\n\nCommandType.StoredProcedure occurrences in code: %d" % S["sp_hint_count"] +
                "\n\nTYPES WITH DATA ACCESS (by script scan): " + ", ".join("Types[%d] %s" % (x["i"], x["name"]) for x in T if x["i"] in X["has_sql"] or x["i"] in X["has_db"]) +
                "\n\nTYPE BRIEFS:\n" + briefs)
    da_llm = ask("7 Data Design", "sec7",
                 "Specify the data design: database(s) and engine (only if evidenced), connection handling, entities/tables and relationships, stored procedures "
                 "(Metrics.StoredProcedureCalls is given), data flows between forms and the data layer, retention/migration concerns and any file-based storage. "
                 "Tables of raw evidence are already provided.", data_ctx)
    er_block = ""
    if S["sql"]:
        er_raw = call_llm("TASK:\nProduce ONE Mermaid erDiagram containing ONLY tables and columns that appear in these SQL statements. "
                          "Add relationships only if a JOIN or foreign key proves them. Output only the mermaid code (starting with erDiagram). "
                          "If you cannot determine any table, output the single word NONE.\n\nSQL:\n" + "\n".join(s for _, s in S["sql"][:150]),
                          SYSTEM_PROMPT_TYPE, "er")
        mm = re.search(r"(erDiagram.*?)(?:```|$)", er_raw, re.S)
        if mm and "NONE" not in er_raw[:20]:
            er_block = "```mermaid\n" + mm.group(1).strip() + "\n```"
            diagrams.append("ER diagram: LLM-derived from SQL literals only (verify)")
    if not er_block:
        er_block = "No ER diagram can be produced: no table structure is evidenced in the JSON."
    sec[7] = ("### 7.1 Data design narrative\n" + da_llm + "\n\n### 7.2 Entity-relationship diagram\n" + er_block +
              "\n\n### 7.3 Table access matrix (CRUD)\n" + crud_tbl + "\n\n### 7.4 Stored procedures called via EXEC\n" +
              (md_table(["Procedure", "Used by"], proc_rows) if proc_rows else "none detected in SQL literals") +
              "\n\n### 7.5 Types that access data\n" + dacc_tbl + "\n\n### 7.6 SQL embedded in code (first 40)\n" + sql_tbl +
              "\n\n### 7.7 Connection strings\n" + conn_tbl + "\n\n### 7.8 Stored procedure / SQL keys in JSON\n" + sp_tbl +
              "\n\nMetrics: StoredProcedureCalls=%s [JSON: Metrics.StoredProcedureCalls]" % MET.get("StoredProcedureCalls"))

    # ---------- 8 Interfaces and APIs ----------
    form_rows = []
    for x in forms:
        handlers = [n for n in x["mnames"] if EVENT_RE.search(n)]
        ctrls = sum(1 for f in x["fields"] if isinstance(f, dict) and str(gi(f, "SourceFile", default="")).lower().endswith(".designer.vb"))
        form_rows.append(["Types[%d]" % x["i"], x["name"], ctrls, len(handlers), purpose_of(TS.get(x["i"]))])
    forms_tbl = md_table(["Source", "Form", "Controls", "Event handlers", "Purpose"], form_rows) if form_rows else "No forms detected."
    api_rows = []
    for r in X["methods"]:
        t = M["types"][r["ti"]]
        if t["is_form"] or r["name"] in ("InitializeComponent", "Dispose"):
            continue
        if r["vis"] and str(r["vis"]).lower() not in ("public", "friend"):
            continue
        api_rows.append(["Types[%d].Methods[%d]" % (r["ti"], r["mi"]), t["name"], trunc(r["id"][2:] if r["id"].startswith("M:") else (r["id"] or r["name"]), 120), r["vis"] or "not recorded"])
    api_tbl = md_table(["Source", "Type", "Signature", "Visibility"], api_rows[:150]) if api_rows else "No non-form methods found."
    ext_rows = [[v, len(d), ", ".join(sorted(set(version_from_dep(x) for x in d if version_from_dep(x)))) or "-", trunc(", ".join(d), 140)] for v, d in sorted(vendors_ext.items())]
    hints = ["mail", "smtp", "gmail", "http", "web", "api", "pay", "sms", "print", "camera", "video", "com", "ftp", "socket", "serial", "barcode", "excel", "pdf", "browser"]
    integ_types = [x for x in T if any(h in x["full"].lower() for h in hints)]
    msg_tbl = md_table(["Source", "Message shown to user (trimmed)"], [[p, s] for p, s in X["msgs"][:60]]) if X["msgs"] else "No MsgBox/MessageBox.Show literals were detected."
    if_llm = ask("8 Interfaces and APIs", "sec8",
                 "Specify the interfaces: user interface (forms), internal programmatic interfaces, external systems and web services/APIs "
                 "(Metrics.WebServices/WebOperations given), COM/DLL calls, file import/export, e-mail, printers, devices, protocols, authentication, "
                 "request/response formats and error codes. Only what is evidenced; otherwise state 'not evidenced'. Tables are already provided.",
                 core_json + "\n\nINTEGRATION-LIKE TYPES (by name):\n" + (fit_summaries(M, TS, set(x["i"] for x in integ_types)) if integ_types else "none") +
                 "\n\nUNRESOLVED SYMBOL ROOTS:\n" + str(unres_groups.most_common(40)) + "\n\nUSER MESSAGES FOUND: %d" % len(X["msgs"]))
    sec[8] = ("### 8.1 User interface (forms)\n" + forms_tbl + "\n\n### 8.2 Internal programmatic interfaces (non-form types; first 150 of %d)\n" % len(api_rows) + api_tbl +
              "\n\n### 8.3 External libraries and systems [JSON: ExternalDependencies]\n" + (md_table(["Vendor/library", "Assemblies", "Version(s)", "Names"], ext_rows) if ext_rows else "none") +
              "\n\n### 8.4 Types whose names suggest an integration (Assumption: name-based)\n" +
              (md_table(["Source", "Type", "Purpose"], [["Types[%d]" % x["i"], x["full"], purpose_of(TS.get(x["i"]))] for x in integ_types]) if integ_types else "none") +
              "\n\n### 8.5 User-visible messages (first 60 of %d)\n" % len(X["msgs"]) + msg_tbl + "\n\n### 8.6 Interface narrative\n" + if_llm)

    # ---------- 9 Security ----------
    sec_ev = []
    for kw, cnt in S["security"].items():
        sec_ev.append("%s: %s" % (kw, ", ".join("Types[%d] x%d" % (i, n) for i, n in cnt.most_common(5))))
    sec_tbl = md_table(["Keyword in code", "Where (top types)"], [[kw, ", ".join("Types[%s] x%d" % (i, n) for i, n in cnt.most_common(5))] for kw, cnt in S["security"].items()]) if S["security"] else "No security-related identifiers were found in code."
    sec_debt = [r for r in debt if any(k in r[0].lower() for k in ("sql built", "hard-coded", "option strict", "variant", "late", "adodb", "createobject"))]
    sc_llm = ask("9 Security", "sec9",
                 "Specify the security design as evidenced: authentication, authorization/roles, credential and password handling, connection-string handling, "
                 "input validation and SQL-injection exposure (string-concatenated SQL), data protection, auditing and compliance. All judgements are Assumptions "
                 "unless explicit. Tables are already provided.",
                 core_json + "\n\nKEYWORD EVIDENCE (identifier counts in code):\n" + ("\n".join(sec_ev) or "none") +
                 "\n\nCONNECTION STRINGS FOUND (masked): %d\n\nSECURITY-RELEVANT DEBT ROWS:\n%s\n\nTYPE BRIEFS:\n%s" % (
                     len(S["conns"]), "\n".join(" | ".join(map(str, r)) for r in sec_debt) or "none", briefs))
    sec[9] = ("### 9.1 Evidence from code\n" + sec_tbl + "\n\n### 9.2 Security-relevant findings (rule-based ratings, Assumption)\n" +
              (md_table(["Item", "Evidence", "Rating", "Source"], sec_debt) if sec_debt else "none detected") + "\n\n### 9.3 Security specification\n" + sc_llm)

    # ---------- 10 Infrastructure and deployment ----------
    cfg_tbl = md_table(["Path", "Kind"], [[gi(c, "Path", default="?"), gi(c, "Kind", default="?")] for c in cfg[:80]]) if cfg else "none"
    my_types = [x for x in T if x["full"].split(".")[-1].startswith("My") or ".My." in x["full"]]
    infra_rows = [["Target runtime", ir.get("TargetFramework"), "[JSON: TargetFramework]"],
                  ["Platform", "Windows desktop (WinForms)" if optmap.get("MyType") == "WindowsForms" else "not determinable", "[JSON: Options]"],
                  ["Project SDK / format", ir.get("ProjectSdk") or "classic .vbproj (no SDK)", "[JSON: ProjectSdk]"],
                  ["Installer / publish profile", "not present in JSON", "-"],
                  ["Environments (dev/test/prod)", "not present in JSON", "-"],
                  ["CI/CD", "not present in JSON", "-"],
                  ["Hosting / servers", "not present in JSON", "-"]]
    in_llm = ask("10 Infrastructure and Deployment", "sec10",
                 "Specify infrastructure and deployment as far as evidenced: runtime requirements, configuration files and settings, how connection strings/settings "
                 "are provided, environments, installer/deployment model, CI/CD, hosting and scaling. State 'not evidenced' for anything absent.",
                 core_json + "\n\nCONFIG ARTIFACTS: %d total; kinds=%s [JSON: ConfigurationArtifacts]\nMY/SETTINGS TYPES: %s\nConnection strings found: %d (see section 7)" % (
                     len(cfg), dict(Counter(gi(c, "Kind", default="?") for c in cfg)), ", ".join(x["full"] for x in my_types) or "none", len(S["conns"])))
    sec[10] = ("### 10.1 Infrastructure facts\n" + md_table(["Item", "Value", "Source"], infra_rows) +
               "\n\n### 10.2 Configuration artifacts (first 80 of %d) [JSON: ConfigurationArtifacts]\n" % len(cfg) + cfg_tbl + "\n\n### 10.3 Narrative\n" + in_llm)

    # ---------- 11 Error handling and logging ----------
    eh_rows = []
    for ti in sorted(k for k in X["eh"] if k is not None and k < len(T)):
        e = X["eh"][ti]
        if any(e.values()):
            eh_rows.append(["Types[%d]" % ti, T[ti]["name"], e["try"], e["catch"], e["finally"], e["using"], e["throw"], e["onerror"], e["emptycatch"]])
    eh_tbl = md_table(["Source", "Type", "Try", "Catch", "Finally", "Using", "Throw", "On Error", "Empty Catch"], eh_rows) if eh_rows else "No structured or legacy error handling was detected in code."
    logs = ", ".join("%s x%d" % kv for kv in S["logging_hits"].most_common()) or "none detected"
    no_eh = sorted(ti for ti in (X["has_sql"] | X["has_db"]) if ti is not None and not (X["eh"].get(ti, {}).get("try") or X["eh"].get(ti, {}).get("onerror")))
    eh_llm = ask("11 Error Handling and Logging", "sec11",
                 "Specify error handling and logging: exception strategy (Try/Catch vs On Error), how errors are reported to the user, logging mechanisms, "
                 "monitoring/alerting (state 'not evidenced' if none), and gaps (e.g. data access without error handling). Use the counts provided.",
                 core_json + "\n\nTotals: Try blocks=%d, Catch clauses=%d; logging API hits: %s\nUser message literals found: %d (sample below)\n%s\n\nTYPES WITH DB ACCESS BUT NO Try/On Error: %s" % (
                     S["trycatch"], S["catch"], logs, len(X["msgs"]), "\n".join("%s %s" % (p, s) for p, s in X["msgs"][:30]),
                     ", ".join("Types[%d]" % ti for ti in no_eh) or "none"))
    sec[11] = ("### 11.1 Error-handling statistics per type (from code scan)\n" + eh_tbl + "\n\nTotals: Try=%d, Catch=%d; logging API hits: %s [JSON: Types[].*%s]\n" % (
        S["trycatch"], S["catch"], logs, S["code_key_used"]) + "\n### 11.2 Narrative\n" + eh_llm)

    # ---------- 12 Testing strategy ----------
    tests = [f for f in files if "test" in str(gi(f, "RelativePath", default="")).lower()]
    heavy = sorted(T, key=lambda x: -(x["nconds"] + len(x["methods"])))[:8]
    testab = md_table(["Source", "Type", "Layer*", "Methods", "Conditions", "Has SQL/DB calls", "Testability note (Assumption)"],
                      [["Types[%d]" % x["i"], x["name"], layer[x["i"]], len(x["methods"]), x["nconds"], "yes" if (x["i"] in X["has_sql"] or x["i"] in X["has_db"]) else "no",
                        "business logic and data access in a form class; hard to unit test" if (x["is_form"] and (x["i"] in X["has_sql"] or x["i"] in X["has_db"] or x["nconds"] > 0)) else "-"]
                       for x in heavy]) if heavy else "none"
    ts_llm = ask("12 Testing Strategy", "sec12",
                 "Write: (a) 'Current test status' from the evidence (test files in Files[], none found if none); (b) a RECOMMENDED testing strategy (every item an Assumption) "
                 "covering unit, integration, UI/regression (characterisation tests before migration), performance and acceptance, tied to the business rules and data access "
                 "found. Be concrete about which types to test first. Do not invent test tools that are not in the material.",
                 core_json + "\n\nTEST-LIKE FILES: %d\n\nMOST COMPLEX TYPES:\n%s\n\nDEBT TABLE:\n%s\n\nTYPE BRIEFS:\n%s" % (
                     len(tests), "\n".join("Types[%d] %s methods=%d conditions=%d" % (x["i"], x["name"], len(x["methods"]), x["nconds"]) for x in heavy), debt_ctx, briefs))
    ac = ask_table("12 Testing Strategy", "sec12-ac",
                   "Write acceptance criteria in Given/When/Then style for the behaviours evidenced in the rules and summaries. Basis = 'Evidenced' or 'Assumption'.",
                   ("EXTRACTED RULES:\n" + trunc(rules_txt, 20000) + "\n\n" if rules_txt else "") + "TYPE BRIEFS:\n" + briefs,
                   ["Acceptance criterion", "Source", "Basis"], id_prefix="AC")
    sec[12] = ("### 12.1 Current test status\n" + ("No test files were found in Files[]. [JSON: Files[].RelativePath]" if not tests else "%d test-like files found [JSON: Files[].RelativePath]" % len(tests)) +
               "\n\n### 12.2 Testability of the most complex types (script heuristic)\n" + testab + "\n\n### 12.3 Recommended strategy\n" + ts_llm +
               "\n\n### 12.4 Acceptance criteria derived from existing behaviour\n" + ac)

    # ---------- 13 Risks, dependencies, open issues ----------
    risk_tbl = md_table(["Item", "Evidence", "Rating", "Source"], debt)
    rk_llm = ask("13 Risks, Dependencies, and Open Issues", "sec13",
                 "Write a short risk narrative: the top 5 risks, why each matters, and any rating you would change (justify as Assumption). The risk table is already provided.",
                 core_json + "\n\nRISK TABLE:\n" + debt_ctx)
    sec[13] = ("Ratings are rule-based by this script (Assumption) and should be reviewed.\n\n### 13.1 Risk register\n" + risk_tbl + "\n\n### 13.2 Risk narrative\n" + rk_llm)

    det_oq = [
        ("Owner / stakeholders / contacts", "Not in JSON. Ask: project owner, repository metadata or team lead."),
        ("Lifecycle status (active, maintenance, retired)", "Not in JSON. Ask: product owner."),
        ("Deployment model, installer and environments", "No installer, publish or environment information in JSON. Ask: build/release engineer."),
        ("Database engine and schema", "Only SQL literals/connection strings found in code are evidenced (section 7). Ask: DBA / configuration on the server."),
        ("Missing framework and 3rd-party references", "%s compilation errors; Authoritative=%s. Re-run the analysis with .NET Framework reference assemblies and the DLLs on the HintPaths. [JSON: AnalysisStatus]" % (A.get("CompilationErrors"), A.get("Authoritative"))),
        ("Non-functional targets (performance, availability, volumes)", "No numeric targets in JSON. Ask: product owner / operations."),
        ("Authentication and authorization model", "Only identifier evidence in code (section 9). Ask: application owner / security team."),
    ]
    if T and not any(x["methods"] for x in T):
        det_oq.append(("Method-level IR missing", "Types[].Methods was empty; set IR_BASE_DIR to the folder with the per-type IR (IrFile) to enrich the design sections."))
    if not has_conds:
        det_oq.append(("Conditions not present in Types[]", "Business rules could not be mined. Provide per-method IR with Conditions or the source code."))
    dep_list = ["Local LLM server %s (model %s) used to generate this document" % (LLM_BASE_URL, LLM_MODEL),
                "IR JSON generated at %s [JSON: GeneratedAtUtc]" % ir.get("GeneratedAtUtc"),
                "Analysis loader: %s [JSON: AnalysisStatus.LoaderMode]" % A.get("LoaderMode")] + ["3rd-party library: %s" % v for v in sorted(vendors_ext)]
    meta = {"diagrams": diagrams, "det_oq": det_oq, "dep_list": dep_list, "core_facts": core_facts, "forms": forms, "A": A, "MET": MET,
            "briefs": briefs, "vendors_ext": vendors_ext, "debt_rows": debt, "layer_counts": layer_counts}
    return sec, meta


# ============================ ASSEMBLY ================================
SECTION_TITLES = {
    1: "Executive Summary", 2: "Overview", 3: "Requirements", 4: "Architecture", 5: "Technology Stack", 6: "Detailed Design",
    7: "Data Design", 8: "Interfaces and APIs", 9: "Security", 10: "Infrastructure and Deployment", 11: "Error Handling and Logging",
    12: "Testing Strategy", 13: "Risks, Dependencies, and Open Issues", 14: "Appendices and Revision History",
}

ROOT_KEY_USE = {
    "SchemaVersion": "Header/Appendix", "ProjectName": "Overview", "ProjectFile": "Overview", "Language": "Overview/Stack", "RootNamespace": "Overview",
    "TargetFramework": "Overview/Stack/Infrastructure/Risks", "ProjectSdk": "Infrastructure", "LanguageVersion": "Stack", "Options": "Overview/Constraints/Risks",
    "GlobalImports": "Appendix", "DefineConstants": "not used (empty)", "ProjectReferences": "Stack", "PackageReferences": "Stack",
    "AssemblyReferences": "Stack/Risks", "ConfigurationArtifacts": "Infrastructure", "Files": "Detailed design/Appendix/Testing", "Types": "Architecture, Design, Data, Interfaces, Security, Errors",
    "Namespaces": "Appendix", "ExternalDependencies": "Stack/Interfaces/Risks", "UnresolvedSymbols": "Architecture/Stack/Interfaces",
    "TypeDependencyGraph": "Architecture", "CallGraph": "Architecture/Sequence diagrams", "AnalysisStatus": "Constraints/Risks/Open issues",
    "Metrics": "Executive summary/Overview/Data", "GeneratedAtUtc": "Overview/Revision history",
}


def exec_summary(ir, M, TS, sec, meta, X):
    MET, A = meta["MET"], meta["A"]
    health = ("Analysis status: Syntax=%s, SemanticCompilation=%s, TypeDependencyGraph=%s, CallGraph=%s, Authoritative=%s, CompilationErrors=%s; "
              "unresolved edges=%s vs internal edges=%s [JSON: AnalysisStatus, Metrics]" % (
                  A.get("Syntax"), A.get("SemanticCompilation"), A.get("TypeDependencyGraph"), A.get("CallGraph"), A.get("Authoritative"),
                  A.get("CompilationErrors"), MET.get("UnresolvedEdges"), MET.get("InternalEdges")))
    ctx = ("FACTS:\n" + fact_json(meta["core_facts"]) + "\n\nHEALTH LINE:\n" + health + "\n\nTYPE BRIEFS:\n" + meta["briefs"] +
           "\n\nTABLES REFERENCED IN SQL: %s\n\nRISK ROWS (item | evidence | rating | source):\n" % ", ".join(sorted(X["crud"].keys())[:30]) +
           "\n".join(" | ".join(map(str, r)) for r in meta["debt_rows"][:14]))
    txt = ask("1 Executive Summary", "sec1",
              "Write a ONE-PAGE executive summary (max ~350 words) of this technical specification with these labelled parts: System and purpose, "
              "Architecture, Technology stack, Data, Interfaces, Security, Size and complexity, Key risks, Overall health. Cite sources.", ctx)
    return "**Generated facts at a glance**\n\n" + md_table(["Metric", "Value"], [[k, v] for k, v in MET.items()]) + "\n\n" + txt


def appendix(ir, M, meta, S):
    files = ir.get("Files") or []
    rows = []
    for k, f in enumerate(files):
        rows.append(["Files[%d]" % k, gi(f, "RelativePath", default=""), gi(f, "Lines", default=""), gi(f, "CompilationRole", default=""),
                     gi(f, "IsIncludedInCompilation", default=""), ", ".join(type_short(x) for x in (gi(f, "DeclaredTypes", default=[]) or [])),
                     len(gi(f, "Diagnostics", default=[]) or [])])
    inv = md_table(["Source", "File", "Lines", "Role", "In compilation", "Declared types", "Diagnostics"], rows) if rows else "none"
    codes = Counter()
    for f in files:
        for d in gi(f, "Diagnostics", default=[]) or []:
            m = re.match(r"(BC\d+)", str(d))
            if m:
                codes[m.group(1)] += 1
    now = datetime.datetime.now().strftime("%Y-%m-%d")
    rev = md_table(["Version", "Date", "Author", "Change"], [["0.1 (draft)", now, "generate_tsd.py (LLM: %s)" % LLM_MODEL, "Initial generated draft from IR JSON; needs human review"]])
    appr = md_table(["Role", "Name", "Date", "Signature"], [["Technical owner", "to be completed", "", ""], ["Reviewer", "to be completed", "", ""], ["Approver", "to be completed", "", ""]])
    refs = ["- IR JSON: `%s` (schema %s, generated %s) [JSON: SchemaVersion, GeneratedAtUtc]" % (os.path.basename(IR_JSON_PATH), ir.get("SchemaVersion"), ir.get("GeneratedAtUtc")),
            "- Companion document: Project Understanding Document (generate_pud.py), if produced from the same IR",
            "- Source-citation convention: `[JSON: path]` points into the IR file; `Types[i]` = i-th entry (0-based) of `Types`"]
    parts = ["### 14.1 Revision history\n" + rev, "### 14.2 Approvals\n" + appr, "### 14.3 References\n" + "\n".join(refs),
             "### 14.4 Full file inventory\n" + inv,
             "### 14.5 Compiler diagnostic codes across files [JSON: Files[].Diagnostics]\n" + (md_table(["Code", "Count"], codes.most_common(15)) if codes else "none"),
             "### 14.6 Diagram sources\n" + "\n".join("- " + d for d in meta["diagrams"]),
             "### 14.7 Namespaces [JSON: Namespaces]\n" + ", ".join(ir.get("Namespaces") or []),
             "### 14.8 Global imports [JSON: GlobalImports]\n" + ", ".join(ir.get("GlobalImports") or []),
             "### 14.9 Glossary\nSee section 2.4."]
    return "\n\n".join(parts)


def coverage(ir, M, S, X, meta):
    rows = [[k, "used", ROOT_KEY_USE.get(k, "NOT MAPPED: key not interpreted by this script")] for k in ir.keys()]
    notes = []
    inline_methods = sum(len(x["methods"]) for x in M["types"])
    notes.append("Types[].Methods entries read: %d (Metrics.Methods=%s)" % (inline_methods, (ir.get("Metrics") or {}).get("Methods")))
    notes.append("Code strings scanned (key %s): %d" % (S["code_key_used"], len(S["code_strings"])))
    notes.append("Conditions found: %d (Metrics.Conditions=%s)" % (len(S["conds"]), (ir.get("Metrics") or {}).get("Conditions")))
    notes.append("SQL literals: %d; tables extracted: %d; sequence diagrams derived: %d" % (len(S["sql"]), len(X["crud"]), len(X["sequences"])))
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
    CACHE_DIR = os.path.join(OUTPUT_DIR, "cache_tsd")
    os.makedirs(CACHE_DIR, exist_ok=True)
    log("loading %s" % IR_JSON_PATH)
    with open(IR_JSON_PATH, "r", encoding="utf-8-sig") as f:
        ir = json.load(f)
    log("stage 0: deterministic extraction")
    M = build_model(ir)
    S = scan_ir(ir, M)
    G = build_graphs(ir, M)
    X = scan_tsd(ir, M, S, G)
    log("types=%d files=%d sql=%d conds=%d tables=%d sequences=%d" % (len(M["types"]), len(M["files"]), len(S["sql"]), len(S["conds"]), len(X["crud"]), len(X["sequences"])))
    log("stage 1: per-type summaries (LLM)")
    TS = summarize_types(M)
    log("stage 2: business-rule mining (LLM)")
    rules_txt, has_conds = business_rules(S)
    log("stage 3: section generation (LLM)")
    sec, meta = build_all(ir, M, S, G, X, TS, rules_txt, has_conds)
    log("stage 4: executive summary + assembly")
    sec[1] = exec_summary(ir, M, TS, sec, meta, X)
    a_lines = ["- %s (section %s)" % (t, s.split()[0]) for s, t in ASSUMPTIONS]
    q_lines = ["- **%s** - %s" % (t, d) for t, d in meta["det_oq"]] + ["- %s (section %s)" % (t, s.split()[0]) for s, t in OPEN_QUESTIONS]
    sec[13] += ("\n\n### 13.3 Dependencies\n" + "\n".join("- " + d for d in meta["dep_list"]) +
                "\n\n### 13.4 Assumptions\n" + ("\n".join(dict.fromkeys(a_lines)) or "none recorded") +
                "\n\n### 13.5 Open issues and questions (who/what could answer)\n" + "\n".join(dict.fromkeys(q_lines)))
    sec[14] = appendix(ir, M, meta, S)
    out = ["# Technical Specification Document - %s" % ir.get("ProjectName", "VB.NET project"),
           "_Generated %s from `%s` (IR schema %s, IR generated %s). LLM: %s. As-is specification of the existing system._" % (
               datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), os.path.basename(IR_JSON_PATH), ir.get("SchemaVersion"),
               ir.get("GeneratedAtUtc"), LLM_MODEL),
           "_Conventions: `[JSON: path]` = source in the IR file; `Assumption:` = inferred, not stated; unclear items are in section 13.5._"]
    for n in range(1, 15):
        out.append("## %d. %s\n\n%s" % (n, SECTION_TITLES[n], sec.get(n, "")))
    out.append("## Coverage Check\n\n" + coverage(ir, M, S, X, meta))
    path = os.path.join(OUTPUT_DIR, OUTPUT_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(out) + "\n")
    log("DONE -> %s (%d chars)%s" % (path, sum(len(x) for x in out), "; LLM errors: %d" % len(LLM_ERRORS) if LLM_ERRORS else ""))


if __name__ == "__main__":
    main()
