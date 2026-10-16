#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_brd.py  -  all_rules.json  ->  business_rules_document.md

Single file, Python 3.8 compatible, only dependency: requests.
Same design and same local-LLM connection as generate_tsd.py / generate_pud.py.

WHAT THE DOCUMENT IS
  A standard Business Rules Document (BRD) for an EXISTING application, built ONLY from the
  consolidated business rules in all_rules.json (atomic -> component-intermediate -> file level
  rules, each with conditions, outcome, inputs/outputs, evidence, confidence, review flags).
  Everything inferred is marked "Assumption"; everything unclear goes to Open Questions.

HOW IT KEEPS THE LLM EFFICIENT (the whole JSON is NEVER sent to the LLM)
  Stage 0  Python loads the JSON and does everything deterministic: normalising rules, the
           derivedFrom hierarchy (which rules consolidate which), grouping by category / module,
           validation / workflow / data / security views, statistics, traceability, quality checks,
           data dictionary and all tables + the Mermaid diagram.
  Stage 1  The LLM only writes short narratives. Each call gets a compact digest of ONE group of
           rules (a category, a module, a view) - never the full file - and cites [Rule: <ruleId>].
  Stage 2  Python assembles the final Markdown; the Executive Summary is written last and put first.
  Every LLM response is cached on disk (OUTPUT_DIR/cache_brd), so a crash or re-run resumes where it
  stopped and costs nothing for finished calls.

INPUT  : all_rules.json only. Set the two paths below.
OUTPUT : OUTPUT_DIR/business_rules_document.md
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
RULES_JSON_PATH = r"<PATH_TO_ALL_RULES_JSON_FILE>"   # e.g. r"/mnt/.../br outputs/br/lineage/all_rules.json"
OUTPUT_DIR = r"<PATH_TO_OUTPUT_DIRECTORY>"           # e.g. r"/mnt/.../docs"
OUTPUT_FILENAME = "business_rules_document.md"

# "top" = the main catalogue lists only top-level rules (rules that no other rule is derived from, i.e. the
#         consolidated ones); the lower-level rules they were built from stay in the appendix and traceability.
# "all" = the main catalogue lists every rule at every level.
RULE_VIEW = "top"

# --- Local LLM (same pattern as the team's latest_llm_copy.py chat app, generate_pud.py, generate_tsd.py) ---
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://172.31.8.100:9005/v1/chat/completions")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3-coder-next-awq")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "<PUT_API_KEY_HERE>")   # prefer env var over hard-coding
LLM_TIMEOUT = 1500
LLM_TEMPERATURE = 0.1
LLM_MAX_TOKENS = None       # e.g. 4096 to cap answers; None = server default
LLM_RETRIES = 3

# --- Prompt budgets (characters; ~4 chars per token) ---
MAX_PROMPT_CHARS = 90000        # hard guard per call (model context is capped at 250k tokens)
MAX_RULES_CHARS = 30000         # rule digest sent in one narrative call
MAX_FACTS_CHARS = 6000          # facts block sent with every call
LOW_CONFIDENCE = 0.8            # rules below this confidence are listed in "items requiring review"
TOP_FIELDS = 80                 # data-dictionary rows / glossary candidates
MAX_TABLE_STATEMENT = 800       # chars of a rule statement shown in a table cell

DRY_RUN = os.environ.get("BRD_DRY_RUN") == "1"   # 1 = no LLM calls (stub text), to test the pipeline

# ======================================================================
SYSTEM_PROMPT = (
    "You are a senior business analyst writing ONE section of a Business Rules Document (BRD) for an EXISTING "
    "legacy application. The rules in the material were extracted from its source code.\n"
    "STRICT RULES:\n"
    "1. Use ONLY the material in the user message. Never invent rules, limits, thresholds, roles, screens, tables or behaviour.\n"
    "2. After every key statement cite its source rule exactly as [Rule: <ruleId>], e.g. [Rule: FR-25dcdc3bcbb0b3d3]. "
    "You may cite several: [Rule: AR-1][Rule: AR-2].\n"
    "3. Anything inferred (not literally stated in the rules) must start with the word 'Assumption:'.\n"
    "4. If something is unclear, missing or contradictory, do not guess; list it as an open question.\n"
    "5. Write in plain business language for business readers (short paragraphs and bullets). Describe what the business "
    "requires, not how the code works; mention screen or field names only when needed to identify the rule. "
    "Do not repeat tables that the material says are already provided.\n"
    "6. Do not write the section heading and do not use Markdown headings (#); use bold text for sub-titles.\n"
    "7. End your answer with these two blocks, exactly as shown:\n"
    "@@ASSUMPTIONS@@\n- one bullet per assumption you made (or '- none')\n"
    "@@OPEN_QUESTIONS@@\n- one bullet per open question, saying who or what could answer it (or '- none')\n"
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


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def as_strs(v):
    out = []
    for x in as_list(v):
        if isinstance(x, str):
            s = oneline(x)
        else:
            s = oneline(json.dumps(x, ensure_ascii=False, default=str))
        if s:
            out.append(s)
    return out


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def md_table(headers, rows):
    def esc(x):
        return str(x).replace("|", "\\|").replace("\n", " ")
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(esc(c) for c in r) + " |")
    return "\n".join(out)


def mid(s):
    return "n" + re.sub(r"[^0-9A-Za-z]", "_", str(s))


def clean_label(s, n=60):
    s = re.sub(r"[\"'`\[\]\(\)\{\}<>|#;&]", " ", str(s))
    return trunc(oneline(s), n).replace('"', "")


def pct(a, b):
    return "%d (%.0f%%)" % (a, 100.0 * a / b) if b else "%d" % a


# ============================== LLM ===================================
CACHE_DIR = None
LLM_ERRORS = []
ASSUMPTIONS = []      # (section, text)
OPEN_QUESTIONS = []   # (section, text)


def call_llm(prompt, system, tag):
    if DRY_RUN:
        return "[DRY RUN stub for %s]\n@@ASSUMPTIONS@@\n- stub assumption\n@@OPEN_QUESTIONS@@\n- stub question" % tag
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


def split_markers(text):
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


def demote_headings(text):
    """LLM headings would pollute the PDF table of contents; turn them into bold lines."""
    return re.sub(r"(?m)^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", r"**\1**", text)


def ask(section, tag, task, material):
    prompt = "TASK:\n%s\n\nMATERIAL (the only information you may use):\n%s" % (task, material)
    raw = call_llm(prompt, SYSTEM_PROMPT, tag)
    body, a, q = split_markers(raw)
    for x in a:
        ASSUMPTIONS.append((section, x))
    for x in q:
        OPEN_QUESTIONS.append((section, x))
    return demote_headings(body)


def ask_table(section, tag, task, material, headers):
    """LLM writes rows as 'a | b | c' lines; Python builds the Markdown table (robust against bad table syntax)."""
    n = len(headers)
    fmt = ("OUTPUT FORMAT: one row per line with exactly %d columns separated by ' | ' (space, pipe, space) in this order: %s. "
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
    out = md_table(headers, rows)
    if notes:
        out += "\n\n" + "\n".join(notes)
    return out


# ============================ STAGE 0: MODEL ==========================
UNKNOWN = "(unknown)"
LEVEL_ORDER = {"file": 0, "intermediate": 1, "atomic": 2, "other": 3}
LEVEL_LABEL = {"file": "File", "intermediate": "Component (intermediate)", "atomic": "Atomic", "other": "Other"}
KNOWN_KEYS = ("ruleid", "level", "title", "statement", "category", "rulekind", "conditions", "outcome", "expression",
              "inputs", "outputs", "relatedsymbols", "tags", "evidence", "derivedfrom", "sourcecontexts", "confidence",
              "needsreview", "reviewreasons", "provenance")

DATA_RE = re.compile(r"\b(select|insert|update|delete|sql|quer(?:y|ies)|database|recordset|connection|table|stored procedure|records?)\b", re.I)
SEC_RE = re.compile(r"\b(login|log in|password|admin|authenticat\w*|authoriz\w*|permission\w*|credential\w*|roles?|session|access)\b", re.I)


def norm_level(lv, rid):
    s = str(lv or "").lower()
    if "intermediate" in s or "component" in s:
        return "intermediate"
    if "file" in s:
        return "file"
    if "atomic" in s:
        return "atomic"
    p = str(rid).split("-")[0].upper()
    return {"FR": "file", "CIR": "intermediate", "AR": "atomic"}.get(p, "other")


def parse_symbol(sym):
    s = str(sym or "")
    if s[:2] in ("M:", "T:", "F:", "P:"):
        s = s[2:]
    parts = s.split("(")[0].split(".")
    if len(parts) >= 3:
        return ".".join(parts[:-2]), parts[-2], parts[-1]
    if len(parts) == 2:
        return "", parts[0], parts[1]
    return "", "", s


def cat_label(c):
    return re.sub(r"[-_]+", " ", str(c)).strip().title() or "Uncategorized"


def load_rules(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    raw = None
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        v = gi(data, "rules")
        if isinstance(v, list):
            raw = v
        else:
            for k, v in data.items():
                if isinstance(v, list) and v and isinstance(v[0], dict) and gi(v[0], "ruleId") is not None:
                    raw = v
                    break
    if raw is None:
        sys.exit("No 'rules' list found in %s" % path)
    return data, raw


def build_model(root, raw):
    rules = []
    for i, x in enumerate(raw):
        if not isinstance(x, dict):
            continue
        rid = str(gi(x, "ruleId", "id", default="RULE-%d" % i))
        ev = [e for e in as_list(gi(x, "evidence", default=[])) if isinstance(e, dict)]
        prim = None
        for e in ev:
            if str(gi(e, "role", default="")).lower().startswith("target"):
                prim = e
                break
        if prim is None and ev:
            prim = ev[0]
        module = str(gi(prim, "file", default="")) if prim is not None else ""
        if not module and gi(x, "file"):
            module = str(gi(x, "file"))
        sym = str(gi(prim, "symbolId", default="")) if prim is not None else ""
        ns, cls, mem = parse_symbol(sym)
        prov = gi(x, "provenance", default={})
        derived = [str(d) for d in as_list(gi(x, "derivedFrom", default=[])) if d]
        rules.append({
            "i": i, "id": rid, "level": norm_level(gi(x, "level"), rid), "level_raw": str(gi(x, "level", default="")),
            "title": oneline(gi(x, "title", default="")), "statement": oneline(gi(x, "statement", default="")),
            "category": oneline(gi(x, "category", default="uncategorized")).lower() or "uncategorized",
            "kind": oneline(gi(x, "ruleKind", default="")).lower(),
            "conditions": as_strs(gi(x, "conditions")), "outcome": oneline(gi(x, "outcome", default="")),
            "expression": oneline(gi(x, "expression", default="")),
            "inputs": as_strs(gi(x, "inputs")), "outputs": as_strs(gi(x, "outputs")),
            "symbols": as_strs(gi(x, "relatedSymbols")), "tags": [t.lower() for t in as_strs(gi(x, "tags"))],
            "evidence": ev, "derived": [d for d in derived if d != rid],
            "contexts": as_strs(gi(x, "sourceContexts")), "conf": to_float(gi(x, "confidence")),
            "review": bool(gi(x, "needsReview", default=False)), "reasons": as_strs(gi(x, "reviewReasons")),
            "model": str(gi(prov, "model", default="")) if isinstance(prov, dict) else "",
            "pv": str(gi(prov, "promptVersion", default="")) if isinstance(prov, dict) else "",
            "module": module or UNKNOWN, "ns": ns, "cls": cls, "member": mem, "symbol": sym,
            "keys": [k for k in x.keys()],
        })
    return rules


def where_of(r, limit=2):
    pref = [e for e in r["evidence"] if str(gi(e, "role", default="")).lower().startswith("target")] or r["evidence"]
    seen, out = set(), []
    for e in pref:
        f, a, b = gi(e, "file", default="?"), gi(e, "startLine"), gi(e, "endLine")
        s = "%s L%s-%s" % (f, a, b) if a is not None and b is not None else str(f)
        if s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= limit:
            break
    return "; ".join(out) or "n/a"


def blob(r):
    return " ".join([r["title"], r["statement"], r["outcome"], " ".join(r["conditions"]), " ".join(r["tags"]),
                     " ".join(r["inputs"]), r["kind"], r["category"]])


def is_validation(r):
    t = " ".join([r["category"], r["kind"]] + r["tags"])
    return any(k in t for k in ("valid", "format", "constraint", "required"))


def is_workflow(r):
    return any(k in " ".join([r["category"], r["kind"]] + r["tags"]) for k in ("workflow", "process"))


def is_data(r):
    return bool(DATA_RE.search(blob(r))) or any("database" in t or t == "data" for t in r["tags"])


def is_security(r):
    return bool(SEC_RE.search(blob(r)))


def build_facts(root, rules):
    F = {}
    by_id = {}
    for r in rules:
        by_id.setdefault(r["id"], r)
    for _ in range(3):   # a rule without evidence takes the module of the rules it was derived from
        for r in rules:
            if r["module"] == UNKNOWN:
                c = Counter(by_id[s]["module"] for s in r["derived"] if s in by_id and by_id[s]["module"] != UNKNOWN)
                if c:
                    r["module"] = c.most_common(1)[0][0]
    used_by = defaultdict(list)
    for r in rules:
        for s in r["derived"]:
            used_by[s].append(r["id"])
    for r in rules:
        r["support"] = r["id"] in used_by
    view = [r for r in rules if not r["support"]] if RULE_VIEW == "top" else list(rules)
    if not view:
        view = list(rules)
    view.sort(key=lambda r: (r["module"].lower(), r["category"], r["title"].lower(), r["id"]))
    for k, r in enumerate(view):
        r["ref"] = "BR-%03d" % (k + 1)
    for r in rules:
        r.setdefault("ref", "")
    ns = Counter(r["ns"] for r in rules if r["ns"])
    F["project"] = ns.most_common(1)[0][0] if ns else "Application"
    F["by_id"], F["used_by"], F["view"] = by_id, used_by, view
    mods = defaultdict(list)
    for r in rules:
        mods[r["module"]].append(r)
    F["modules"] = mods
    cats = defaultdict(list)
    for r in view:
        cats[r["category"]].append(r)
    F["cats"] = cats
    F["root"] = root
    lv = Counter(r["level"] for r in rules)
    F["facts_txt"] = trunc(json.dumps({
        "project": F["project"], "total_rules": len(rules), "catalogue_rules": len(view), "catalogue_mode": RULE_VIEW,
        "rules_by_level": dict(lv), "catalogue_rules_by_category": dict((cat_label(k), len(v)) for k, v in cats.items()),
        "rules_by_module": dict((k, len(v)) for k, v in sorted(mods.items())[:60]),
        "rules_flagged_needs_review": sum(1 for r in rules if r["review"]),
    }, ensure_ascii=False, indent=1), MAX_FACTS_CHARS)
    return F


# ============================ DIGESTS & TABLES ========================
def rule_digest(r):
    parts = ["[Rule: %s] (%s level; %s/%s) %s" % (r["id"], r["level"], cat_label(r["category"]), r["kind"] or "-", r["title"] or "(no title)")]
    if r["statement"]:
        parts.append("  statement: " + trunc(r["statement"], 500))
    if r["conditions"]:
        parts.append("  conditions: " + trunc(" ; ".join(r["conditions"]), 400))
    if r["outcome"]:
        parts.append("  outcome: " + trunc(r["outcome"], 300))
    if r["inputs"]:
        parts.append("  inputs: " + trunc(", ".join(r["inputs"]), 160))
    parts.append("  module: %s | source: %s" % (r["module"], where_of(r, 1)))
    return "\n".join(parts)


def digest_block(rs, budget=MAX_RULES_CHARS):
    ordered = sorted(rs, key=lambda r: (1 if r["support"] else 0, LEVEL_ORDER.get(r["level"], 3), r["id"]))
    out, size, used = [], 0, 0
    for r in ordered:
        d = rule_digest(r)
        if size + len(d) > budget and out:
            break
        out.append(d)
        size += len(d) + 1
        used += 1
    if used < len(ordered):
        out.append("...[%d more rules not shown in this prompt; do not speculate about them]" % (len(ordered) - used))
    return "\n".join(out)


def conds_cell(r, n=6):
    cs = r["conditions"]
    if not cs:
        return "-"
    txt = "<br>".join("%d) %s" % (i + 1, trunc(c, 240)) for i, c in enumerate(cs[:n]))
    if len(cs) > n:
        txt += "<br>+%d more" % (len(cs) - n)
    return txt


def rule_cell(r):
    return "**%s**<br>`%s`" % (r["title"] or "(no title)", r["id"])


def rules_table(rs, with_module=False):
    hdr = ["Ref", "Rule", "Statement", "Conditions", "Outcome", "Source"]
    if with_module:
        hdr.insert(2, "Module")
    rows = []
    for r in rs:
        row = [r["ref"] or "-", rule_cell(r), trunc(r["statement"], MAX_TABLE_STATEMENT) or "-", conds_cell(r),
               trunc(r["outcome"], 400) or "-", where_of(r)]
        if with_module:
            row.insert(2, r["module"])
        rows.append(row)
    return md_table(hdr, rows) if rows else "No rules in this group."


def validation_table(rs):
    rows = []
    for r in rs:
        expr = ("<br>Expression: `%s`" % r["expression"].replace("`", "'")) if r["expression"] else ""
        rows.append([r["ref"] or "-", rule_cell(r), trunc(", ".join(r["inputs"]), 200) or "-",
                     conds_cell(r) + expr, trunc(r["outcome"], 400) or "-", where_of(r)])
    return md_table(["Ref", "Rule", "Field(s) / input", "Validation condition", "Outcome", "Source"], rows) if rows else "No rules in this group."


def io_table(rs):
    rows = [[r["ref"] or "-", rule_cell(r), conds_cell(r), trunc(r["outcome"], 400) or "-",
             trunc(", ".join(r["inputs"]), 160) or "-", trunc(", ".join(r["outputs"]), 160) or "-", where_of(r)] for r in rs]
    return md_table(["Ref", "Rule", "Trigger / conditions", "Outcome", "Inputs", "Outputs", "Source"], rows) if rows else "No rules in this group."


def narrative(section, tag, task, rs, F, extra=""):
    if not rs:
        return "No rules in this group, so nothing can be specified here."
    material = "FACTS:\n%s\n\n%sRULES (the only evidence; consolidated top-level rules come first):\n%s" % (F["facts_txt"], extra, digest_block(rs))
    return ask(section, tag, task, material)


# ============================ SECTIONS ================================
def stats_rows(rules, F):
    view = F["view"]
    confs = [r["conf"] for r in rules if r["conf"] is not None]
    return [
        ["Total rules in all_rules.json", len(rules)],
        ["Rules listed in the main catalogue (%s)" % ("top-level only" if RULE_VIEW == "top" else "all levels"), len(view)],
        ["Supporting lower-level rules (derived into others)", sum(1 for r in rules if r["support"])],
        ["Modules / source files", len([m for m in F["modules"] if m != UNKNOWN])],
        ["Categories (catalogue)", len(F["cats"])],
        ["Rules needing review (needsReview=true)", sum(1 for r in rules if r["review"])],
        ["Rules below confidence %.2f" % LOW_CONFIDENCE, sum(1 for c in confs if c < LOW_CONFIDENCE)],
        ["Rules without evidence", sum(1 for r in rules if not r["evidence"])],
    ]


def sec_intro(rules, F):
    view = F["view"]
    tags = Counter(t for r in rules for t in r["tags"])
    titles = "\n".join("[Rule: %s] %s / %s" % (r["id"], r["module"], r["title"]) for r in view[:150])
    mod_tbl = md_table(["Module / file", "Form or class", "All rules", "Catalogue rules"],
                       [[m, (Counter(r["cls"] for r in rs if r["cls"]).most_common(1) or [("-", 0)])[0][0], len(rs),
                         sum(1 for r in rs if r in view)] for m, rs in sorted(F["modules"].items())])
    purpose = ask("2 Introduction", "sec2-purpose",
                  "Write two short subsections as bold sub-titles: 'Purpose' (what this document is for: it records the business rules the existing "
                  "application enforces, so they can be validated and carried into a modernised system) and 'Business context' (what business the rules "
                  "show the application supports, only as far as evidenced by rule titles, modules and tags; anything else is an Assumption).",
                  "FACTS:\n%s\n\nTAG FREQUENCY: %s\n\nCATALOGUE RULE TITLES (first 150):\n%s" % (F["facts_txt"], tags.most_common(25), titles))
    scope = ask("2 Introduction", "sec2-scope",
                "Write 'In scope' (business areas and screens evidenced by the modules and categories, as bullets) and 'Out of scope / not evidenced' "
                "(business areas one would normally expect but for which no rule exists, e.g. calculation, reporting, integrations; judgements are Assumptions). "
                "Also one short paragraph 'Limitations' stating that the rules were extracted from source code by an automated process and need business validation.",
                "FACTS:\n%s\n\nCATEGORIES: %s\n\nMODULES: %s" % (F["facts_txt"], [cat_label(c) for c in F["cats"]], sorted(F["modules"])))
    method = ("The rules come from `%s`, a consolidation of the rules extracted file by file. Rules exist at three levels: **atomic** (one decision in one code block), "
              "**component-intermediate** (atomic rules combined per component) and **file** (the consolidated business rules of a whole file). The `derivedFrom` field links each rule to the lower-level rules it was built from. "
              "Rule extraction model(s): %s." % (os.path.basename(RULES_JSON_PATH),
                                                  ", ".join(sorted(set("%s (prompt %s)" % (r["model"] or "n/a", r["pv"] or "n/a") for r in rules))) or "n/a"))
    guide = ("- **Ref** (BR-nnn) is a number assigned by this document to each catalogue rule; **Rule ID** is the original id in all_rules.json.\n"
             "- A rule can appear in more than one view (for example a login rule is both an access rule and a data rule).\n"
             "- **Source** gives the file and line range of the code the rule was extracted from.\n"
             "- `[Rule: id]` in the narrative points to the supporting rule.")
    return ("### 2.1 Purpose and business context\n" + purpose + "\n\n### 2.2 Scope\n" + scope + "\n\n### 2.3 Source and method\n" + method +
            "\n\n### 2.4 Modules covered\n" + mod_tbl + "\n\n### 2.5 How to read this document\n" + guide)


def module_category_graph(view):
    pairs = Counter((r["module"], r["category"]) for r in view)
    top = pairs.most_common(120)
    if not top:
        return "No data for a diagram."
    lines = ["flowchart LR"]
    mods = sorted(set(m for (m, _), _ in top))
    cats = sorted(set(c for (_, c), _ in top))
    for m in mods:
        lines.append('  %s["%s"]' % (mid("M" + m), clean_label(m)))
    for c in cats:
        lines.append('  %s(["%s"])' % (mid("C" + c), clean_label(cat_label(c))))
    for (m, c), n in top:
        lines.append("  %s -->|%d| %s" % (mid("M" + m), n, mid("C" + c)))
    return "```mermaid\n" + "\n".join(lines) + "\n```"


def sec_stats(rules, F):
    view = F["view"]
    lv = Counter(r["level"] for r in rules)
    lv_tbl = md_table(["Level", "Meaning", "Rules", "Share"],
                      [[LEVEL_LABEL[k], {"file": "consolidated rules of a whole file", "intermediate": "atomic rules combined per component",
                                         "atomic": "single decision in one code block", "other": "level not recognised"}[k], lv[k], pct(lv[k], len(rules))]
                       for k in sorted(lv, key=lambda k: LEVEL_ORDER[k])])
    cat_all = Counter(r["category"] for r in rules)
    cat_tbl = md_table(["Category", "All rules", "Catalogue rules"],
                       [[cat_label(c), cat_all[c], len(F["cats"].get(c, []))] for c in sorted(cat_all, key=lambda c: -cat_all[c])])
    kind = Counter(r["kind"] or "(none)" for r in rules)
    kind_tbl = md_table(["Rule kind", "Rules"], [[k, n] for k, n in kind.most_common()])
    mod_tbl = md_table(["Module / file", "All rules", "Catalogue rules", "Needs review"],
                       [[m, len(rs), sum(1 for r in rs if r in view), sum(1 for r in rs if r["review"])] for m, rs in sorted(F["modules"].items())])
    confs = [r["conf"] for r in rules if r["conf"] is not None]
    conf_txt = ("minimum %.2f, average %.2f, %d rules below 1.00" % (min(confs), sum(confs) / len(confs), sum(1 for c in confs if c < 1.0))) if confs else "no confidence values in the file"
    prov = Counter((r["model"] or "n/a", r["pv"] or "n/a") for r in rules)
    prov_tbl = md_table(["Extraction model", "Prompt version", "Rules"], [[m, p, n] for (m, p), n in prov.most_common()])
    roles = Counter(str(gi(e, "role", default="?")) for r in rules for e in r["evidence"])
    role_tbl = md_table(["Evidence role", "Count"], [[k, n] for k, n in roles.most_common()]) if roles else "no evidence entries"
    tags = Counter(t for r in rules for t in r["tags"])
    tag_tbl = md_table(["Tag", "Rules"], [[t, n] for t, n in tags.most_common(20)]) if tags else "no tags"
    return ("### 3.1 Facts at a glance\n" + md_table(["Metric", "Value"], stats_rows(rules, F)) + "\n\n### 3.2 Rules by level\n" + lv_tbl +
            "\n\n### 3.3 Rules by category\n" + cat_tbl + "\n\n### 3.4 Rules by module\n" + mod_tbl + "\n\n### 3.5 Rules by kind\n" + kind_tbl +
            "\n\n### 3.6 Confidence and provenance\nConfidence: %s.\n\n" % conf_txt + prov_tbl + "\n\n" + role_tbl +
            "\n\n### 3.7 Module to category map (catalogue rules)\n" + module_category_graph(view) + "\n\n### 3.8 Most frequent tags\n" + tag_tbl)


def sec_categories(rules, F):
    parts = []
    for k, c in enumerate(sorted(F["cats"], key=lambda c: (-len(F["cats"][c]), c))):
        rs = F["cats"][c]
        log("category %d/%d: %s (%d rules)" % (k + 1, len(F["cats"]), c, len(rs)))
        nar = narrative("4 Business Rules by Category", "cat-%s" % c,
                        "Describe in 3-6 sentences what business policy this group of rules (category '%s') establishes. Then list the DISTINCT business "
                        "rules in plain language as bullets, merging duplicates, each ending with its [Rule: id] citation. Do not repeat the table." % cat_label(c), rs, F)
        parts.append("### 4.%d %s (%d rules)\n%s\n\n%s" % (k + 1, cat_label(c), len(rs), nar, rules_table(rs, with_module=True)))
    return "\n\n".join(parts) or "No rules."


def sec_modules(rules, F):
    view_ids = set(r["id"] for r in F["view"])
    parts = []
    mods = sorted(F["modules"].items())
    for k, (m, rs) in enumerate(mods):
        top = [r for r in rs if r["id"] in view_ids]
        cls = (Counter(r["cls"] for r in rs if r["cls"]).most_common(1) or [("", 0)])[0][0]
        log("module %d/%d: %s (%d rules)" % (k + 1, len(mods), m, len(rs)))
        nar = narrative("5 Business Rules by Module", "mod-%s" % m,
                        "Write for the module '%s': **Purpose** (one short paragraph: what this screen/module does for the business, only as evidenced), "
                        "**Business rules summary** (bullets, plain language, each with [Rule: id]) and **Dependencies** (data, other screens, messages shown to users; "
                        "only if evidenced). Do not repeat the table." % m, rs, F)
        parts.append("### 5.%d %s%s - %d rules (%d in catalogue)\n%s\n\n%s" % (
            k + 1, m, (" (form/class: %s)" % cls) if cls else "", len(rs), len(top), nar, rules_table(top)))
    return "\n\n".join(parts) or "No rules."


def sec_validation(F):
    rs = [r for r in F["view"] if is_validation(r)]
    nar = narrative("6 Validation and Input Rules", "sec6",
                    "Summarise the validation and input rules: which fields are mandatory, which characters or formats are allowed, what the user sees when a rule is "
                    "violated, and any pattern repeated across screens. Group by pattern, bullets with [Rule: id]. Do not repeat the table.", rs, F)
    return "Rules selected by category, kind or tags containing validation / format / constraint / required (%d rules).\n\n%s\n\n%s" % (len(rs), nar, validation_table(rs))


def sec_workflow(F):
    rs = [r for r in F["view"] if is_workflow(r)]
    nar = narrative("7 Workflows and Process Rules", "sec7",
                    "Describe the business workflows and process rules in the order a user would meet them (trigger, steps, result), only as evidenced. "
                    "Bullets with [Rule: id]. Do not repeat the table.", rs, F)
    return "Rules selected by category, kind or tags containing workflow / process (%d rules).\n\n%s\n\n%s" % (len(rs), nar, io_table(rs))


def sec_data(F):
    rs = [r for r in F["view"] if is_data(r)]
    nar = narrative("8 Data and Database Rules", "sec8",
                    "Describe the business rules that involve stored data: what is looked up, inserted, updated or deleted, which conditions apply, and what happens "
                    "on success or failure. Name tables or fields only if they appear in the rules. Bullets with [Rule: id]. Do not repeat the table.", rs, F)
    return "Rules whose text or tags refer to database access, queries, records or connections (%d rules).\n\n%s\n\n%s" % (len(rs), nar, io_table(rs))


def sec_security(F):
    rs = [r for r in F["view"] if is_security(r)]
    nar = narrative("9 Security and Access Rules", "sec9",
                    "Describe the access-control rules: who can sign in or do what, how credentials or roles are checked, special users (for example administrators), "
                    "and what happens on failure. Judgements about adequacy are Assumptions. Bullets with [Rule: id]. Do not repeat the table.", rs, F)
    return "Rules whose text or tags refer to login, credentials, roles, permissions or access (%d rules).\n\n%s\n\n%s" % (len(rs), nar, rules_table(rs, with_module=True))


def sec_glossary(rules, F):
    names = Counter()
    roles = defaultdict(set)
    mods = defaultdict(Counter)
    sample = {}
    for r in rules:
        for kind, items in (("input", r["inputs"]), ("output", r["outputs"])):
            for n in items:
                names[n] += 1
                roles[n].add(kind)
                mods[n][r["module"]] += 1
                sample.setdefault(n, r["title"])
    top = names.most_common(TOP_FIELDS)
    dd = md_table(["Data element (as in code)", "Used as", "Rules", "Main modules", "Example rule"],
                  [[n, "/".join(sorted(roles[n])), c, ", ".join(m for m, _ in mods[n].most_common(3)), trunc(sample[n], 90)] for n, c in top]) if top else "No inputs/outputs in the rules."
    gloss = ask_table("10 Business Glossary", "sec10-glossary",
                      "Create a business glossary: business terms and screen/field names that appear in rule titles, statements, inputs and outputs, with their plain-language meaning. "
                      "A meaning that is not explicit in the rules must start with 'Assumption:'. Cite the rule id in the Source column as [Rule: id].",
                      "FACTS:\n%s\n\nDATA ELEMENTS (name | rules | example rule):\n%s\n\nRULE TITLES:\n%s" % (
                          F["facts_txt"], "\n".join("%s | %d | %s" % (n, c, trunc(sample[n], 80)) for n, c in top),
                          "\n".join("[Rule: %s] %s" % (r["id"], r["title"]) for r in F["view"][:200])),
                      ["Term", "Meaning", "Source"])
    return "### 10.1 Data dictionary (top %d data elements)\n%s\n\n### 10.2 Business glossary\n%s" % (len(top), dd, gloss)


def sec_trace(rules, F):
    by_id = F["by_id"]
    rows = []
    for r in F["view"]:
        ds = []
        for s in r["derived"][:6]:
            ds.append("%s (%s)" % (s, by_id[s]["level"] if s in by_id else "not in file"))
        if len(r["derived"]) > 6:
            ds.append("+%d more" % (len(r["derived"]) - 6))
        rows.append([r["ref"], r["id"], LEVEL_LABEL[r["level"]], where_of(r, 3), trunc(r["symbol"][2:] if r["symbol"].startswith("M:") else r["symbol"], 90) or "-",
                     "<br>".join(ds) or "- (leaf rule)", ", ".join(r["contexts"][:3]) or "-"])
    tbl = md_table(["Ref", "Rule ID", "Level", "Source (file, lines)", "Code symbol", "Derived from", "Source contexts"], rows) if rows else "none"
    depth_rows = []
    for lvl in sorted(set(r["level"] for r in rules), key=lambda k: LEVEL_ORDER[k]):
        rs = [r for r in rules if r["level"] == lvl]
        depth_rows.append([LEVEL_LABEL[lvl], len(rs), sum(1 for r in rs if r["derived"]), sum(1 for r in rs if r["support"])])
    hier = md_table(["Level", "Rules", "Derived from other rules", "Used by a higher-level rule"], depth_rows)
    return ("### 11.1 Hierarchy summary\nThe atomic -> component-intermediate -> file chain is stored in `derivedFrom`.\n\n" + hier +
            "\n\n### 11.2 Traceability matrix (catalogue rules)\n" + tbl)


def quality_checks(rules, F):
    by_id = F["by_id"]
    dup_ids = [k for k, n in Counter(r["id"] for r in rules).items() if n > 1]
    unresolved = sorted(set(s for r in rules for s in r["derived"] if s not in by_id))
    no_ev = [r for r in rules if not r["evidence"]]
    no_stmt = [r for r in rules if not r["statement"]]
    no_cond_out = [r for r in rules if not r["conditions"] and not r["outcome"]]
    groups = defaultdict(list)
    for r in rules:
        k = re.sub(r"[^a-z0-9]", "", r["statement"].lower())
        if k:
            groups[(r["level"], k)].append(r["id"])
    dups = [v for v in groups.values() if len(v) > 1]
    prefix_bad = [r["id"] for r in rules if r["level"] != "other" and norm_level("", r["id"]) not in ("other", r["level"])]
    rows = [
        ["Duplicate rule ids", len(dup_ids), ", ".join(dup_ids[:5]) or "-"],
        ["derivedFrom ids that are not in the file", len(unresolved), ", ".join(unresolved[:5]) or "-"],
        ["Rules without evidence (no source lines)", len(no_ev), ", ".join(r["id"] for r in no_ev[:5]) or "-"],
        ["Rules without a statement", len(no_stmt), ", ".join(r["id"] for r in no_stmt[:5]) or "-"],
        ["Rules with neither conditions nor outcome", len(no_cond_out), ", ".join(r["id"] for r in no_cond_out[:5]) or "-"],
        ["Groups of rules with identical statements (same level)", len(dups), "; ".join(", ".join(g[:3]) for g in dups[:3]) or "-"],
        ["Level does not match id prefix (FR/CIR/AR)", len(prefix_bad), ", ".join(prefix_bad[:5]) or "-"],
    ]
    return rows, unresolved, no_ev, dups


def sec_review(rules, F):
    rows_q, unresolved, no_ev, dups = quality_checks(rules, F)
    cand = []
    for r in rules:
        reasons = list(r["reasons"])
        if r["review"]:
            reasons.insert(0, "needsReview=true")
        if r["conf"] is not None and r["conf"] < LOW_CONFIDENCE:
            reasons.append("confidence %.2f" % r["conf"])
        if not r["evidence"]:
            reasons.append("no evidence")
        if not r["statement"]:
            reasons.append("no statement")
        if reasons:
            cand.append([r["ref"] or "-", r["id"], LEVEL_LABEL[r["level"]], r["module"], trunc(r["title"], 80),
                         "-" if r["conf"] is None else "%.2f" % r["conf"], trunc("; ".join(reasons), 200)])
    tbl = md_table(["Ref", "Rule ID", "Level", "Module", "Title", "Confidence", "Reasons"], cand) if cand else "No rule is flagged: needsReview is false for all rules, all confidences are at or above %.2f, and every rule has evidence." % LOW_CONFIDENCE
    return ("### 12.1 Rules flagged for review (%d)\n%s\n\n### 12.2 Quality checks on the rule set\n%s" % (
        len(cand), tbl, md_table(["Check", "Count", "Examples"], rows_q))), len(cand), unresolved


def sec_appendix(rules, F, review_n):
    rows = []
    for r in sorted(rules, key=lambda r: (r["module"].lower(), LEVEL_ORDER[r["level"]], r["id"])):
        rows.append([r["id"], r["ref"] or "-", LEVEL_LABEL[r["level"]], cat_label(r["category"]), trunc(r["title"], 90), r["module"], len(r["derived"]),
                     "-" if r["conf"] is None else "%.2f" % r["conf"]])
    idx = md_table(["Rule ID", "Ref", "Level", "Category", "Title", "Module", "Derived from (count)", "Confidence"], rows) if rows else "none"
    now = datetime.datetime.now().strftime("%Y-%m-%d")
    rev = md_table(["Version", "Date", "Author", "Change"], [["0.1 (draft)", now, "generate_brd.py (LLM: %s)" % LLM_MODEL, "Initial generated draft from all_rules.json; needs business review"]])
    appr = md_table(["Role", "Name", "Date", "Signature"], [["Business owner", "to be completed", "", ""], ["Business analyst", "to be completed", "", ""], ["Approver", "to be completed", "", ""]])
    keys = Counter(k for r in rules for k in r["keys"])
    cov = md_table(["Rule field", "Rules containing it", "Status"],
                   [[k, n, "used" if k.lower() in KNOWN_KEYS else "NOT MAPPED: not interpreted by this script"] for k, n in sorted(keys.items())])
    root_keys = ", ".join(F["root"].keys()) if isinstance(F["root"], dict) else "(root is a list)"
    notes = ["- Root keys of the JSON: %s" % root_keys, "- Rules read: %d; LLM errors: %s" % (len(rules), ("%d (see log)" % len(LLM_ERRORS)) if LLM_ERRORS else "none"),
             "- This script consumes the JSON only; no source code or images were interpreted."]
    return ("### 14.1 Full rule index (all levels)\n" + idx + "\n\n### 14.2 Revision history\n" + rev + "\n\n### 14.3 Approvals\n" + appr +
            "\n\n### 14.4 Coverage check\n" + cov + "\n\n" + "\n".join(notes))


def sec_open(rules, F, review_n, unresolved):
    det_a = ["Every rule was extracted from source code by an automated LLM process and has not been validated by the business (see provenance in section 3.6).",
             "Rule levels are taken from the `level` field; the catalogue shows %s." % ("only top-level rules (rules no other rule is derived from)" if RULE_VIEW == "top" else "all rules"),
             "Section views (validation, workflow, data, security) are keyword-based selections by this script; a rule may appear in several."]
    det_q = [("Business owner and approvers", "Not present in all_rules.json. Ask: product owner."),
             ("Business meaning of screen and field names", "Names such as text-box ids come from code (section 10.1); confirm their meaning with business users.")]
    if review_n:
        det_q.append(("Rules flagged for review", "%d rules are listed in section 12.1; ask a business analyst to confirm or correct them." % review_n))
    if unresolved:
        det_q.append(("derivedFrom references to rules that are not in the file", "%d ids (section 12.2); re-run the consolidation so every derived rule is present." % len(unresolved)))
    if not any("calculat" in c for c in F["cats"]):
        det_q.append(("Calculation rules", "No category containing 'calculation' exists; confirm with the business whether prices, totals, tax or discounts are computed anywhere."))
    if not any(is_workflow(r) for r in F["view"]):
        det_q.append(("Workflow rules", "No workflow rules were extracted; confirm the end-to-end processes with the business."))
    a_lines = ["- " + x for x in det_a] + ["- %s (section %s)" % (t, s.split()[0]) for s, t in ASSUMPTIONS]
    q_lines = ["- **%s** - %s" % (t, d) for t, d in det_q] + ["- %s (section %s)" % (t, s.split()[0]) for s, t in OPEN_QUESTIONS]
    return ("### 13.1 Assumptions\n" + "\n".join(dict.fromkeys(a_lines)) + "\n\n### 13.2 Open questions (who/what could answer)\n" + "\n".join(dict.fromkeys(q_lines)))


def exec_summary(rules, F):
    view = F["view"]
    per_cat = []
    for c, rs in sorted(F["cats"].items(), key=lambda kv: -len(kv[1])):
        per_cat.append("%s (%d): %s" % (cat_label(c), len(rs), " | ".join("[Rule: %s] %s" % (r["id"], trunc(r["title"], 70)) for r in rs[:4])))
    txt = ask("1 Executive Summary", "sec1",
              "Write a ONE-PAGE executive summary (max ~300 words) of this Business Rules Document with these bold labelled parts: Application and purpose, "
              "Scope of the rules, Main rule groups (what the business enforces), Data and access rules, Quality and confidence of the rules, Key open points. Cite rules.",
              "FACTS:\n%s\n\nGROUPS AND EXAMPLE RULES:\n%s\n\nFLAGGED FOR REVIEW: %d" % (F["facts_txt"], "\n".join(per_cat), sum(1 for r in rules if r["review"])))
    return "**Generated facts at a glance**\n\n" + md_table(["Metric", "Value"], stats_rows(rules, F)) + "\n\n" + txt


SECTION_TITLES = {
    1: "Executive Summary", 2: "Introduction", 3: "Rule Inventory and Statistics", 4: "Business Rules by Category", 5: "Business Rules by Module",
    6: "Validation and Input Rules", 7: "Workflows and Process Rules", 8: "Data and Database Rules", 9: "Security and Access Rules",
    10: "Business Glossary and Data Dictionary", 11: "Rule Hierarchy and Traceability", 12: "Items Requiring Review and Quality Checks",
    13: "Assumptions and Open Questions", 14: "Appendices",
}


def main():
    global CACHE_DIR
    if "<PATH" in RULES_JSON_PATH or "<PATH" in OUTPUT_DIR:
        sys.exit("Set RULES_JSON_PATH and OUTPUT_DIR at the top of the script.")
    if not DRY_RUN and requests is None:
        sys.exit("pip install requests")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    CACHE_DIR = os.path.join(OUTPUT_DIR, "cache_brd")
    os.makedirs(CACHE_DIR, exist_ok=True)
    del ASSUMPTIONS[:]
    del OPEN_QUESTIONS[:]
    del LLM_ERRORS[:]
    log("loading %s" % RULES_JSON_PATH)
    root, raw = load_rules(RULES_JSON_PATH)
    rules = build_model(root, raw)
    if not rules:
        sys.exit("all_rules.json contains no rules")
    F = build_facts(root, rules)
    log("rules=%d catalogue=%d modules=%d categories=%d" % (len(rules), len(F["view"]), len(F["modules"]), len(F["cats"])))
    sec = {}
    log("sections 2-3 (introduction, statistics)")
    sec[2] = sec_intro(rules, F)
    sec[3] = sec_stats(rules, F)
    log("section 4 (by category)")
    sec[4] = sec_categories(rules, F)
    log("section 5 (by module)")
    sec[5] = sec_modules(rules, F)
    log("sections 6-9 (validation, workflow, data, security)")
    sec[6] = sec_validation(F)
    sec[7] = sec_workflow(F)
    sec[8] = sec_data(F)
    sec[9] = sec_security(F)
    log("sections 10-12 (glossary, traceability, review)")
    sec[10] = sec_glossary(rules, F)
    sec[11] = sec_trace(rules, F)
    sec[12], review_n, unresolved = sec_review(rules, F)
    sec[14] = sec_appendix(rules, F, review_n)
    log("executive summary + assembly")
    sec[1] = exec_summary(rules, F)
    sec[13] = sec_open(rules, F, review_n, unresolved)
    out = ["# Business Rules Document - %s" % F["project"],
           "_Generated %s from `%s`. LLM: %s. As-is business rules of the existing application, extracted from its source code._" % (
               datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), os.path.basename(RULES_JSON_PATH), LLM_MODEL),
           "_Conventions: `[Rule: id]` = source rule in all_rules.json; `Assumption:` = inferred, not stated in the rules; unclear items are in section 13._"]
    for n in range(1, 15):
        out.append("## %d. %s\n\n%s" % (n, SECTION_TITLES[n], sec.get(n, "")))
    path = os.path.join(OUTPUT_DIR, OUTPUT_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(out) + "\n")
    log("DONE -> %s (%d chars)%s" % (path, sum(len(x) for x in out), "; LLM errors: %d" % len(LLM_ERRORS) if LLM_ERRORS else ""))


if __name__ == "__main__":
    main()
