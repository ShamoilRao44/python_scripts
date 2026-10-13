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
        low = " ".jo
