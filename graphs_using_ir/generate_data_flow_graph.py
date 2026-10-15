#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_data_flow_graph.py  -  VB.NET IR JSON  ->  data_flow_graph.md  +  data_flow_graph.html

Single file, Python 3.8 compatible, standard library only. No LLM, no Graphviz, no internet needed
(same style as generate_dependency_graph.py / generate_api_graph.py: two path variables at the top, deterministic output).

WHAT IT ANSWERS
  Where does data come from, where does it go, and who moves it?
  - Which types READ or WRITE which database tables (and stored procedures), with a CRUD matrix per table
  - Which types read or write files, HTTP endpoints, mail, printers, camera/video, settings/config, registry, clipboard
  - Per method: which UI controls feed it (inputs), what it reads, what it writes, which controls it fills (outputs)
  - Which global / shared variables carry data between types (module and Shared fields), and who writes or reads them

HOW IT IS BUILT (all deterministic, evidence = JSON path of the method or field)
  Tables      = parsed from SQL string literals in code: SELECT/JOIN = read, INSERT = create, UPDATE = update, DELETE = delete,
                EXEC = stored procedure. Literals split by concatenation (e.g. "select * from " & ...) are joined when they
                end in FROM/INTO/UPDATE/JOIN. SQL assembled at run time from variables cannot be seen (stated in the output).
  Other stores= regex over code (File.*, StreamReader/Writer, WebClient, SmtpClient, PrintDocument, My.Settings, ...).
  UI inputs   = control.Text / SelectedValue / Checked ... that are read; outputs = control.Text = ..., DataSource = ...
  Globals     = fields of Modules or marked Shared, matched by name in other types' code (Assumption: name match only).
  Confidence banner from AnalysisStatus/Metrics, as in the other scripts.

INPUT  : the IR JSON only. Set the two paths below.
OUTPUT : data_flow_graph.md (Mermaid + tables) and data_flow_graph.html (one self-contained page, SVG graphs, filter box).
"""
import os
import re
import sys
import json
import html
import math
import datetime
from collections import Counter, defaultdict

# =============================== CONFIG ===============================
IR_JSON_PATH = r"<PATH_TO_IR_JSON_FILE>"       # e.g. r"/mnt/MyDocuments/.../project.ir.json"
OUTPUT_DIR = r"<PATH_TO_OUTPUT_DIRECTORY>"     # e.g. r"/mnt/MyDocuments/.../graph_output"
IR_BASE_DIR = ""        # optional: folder that contains files/ and types/ (per-item IR). "" = not used
OUTPUT_MD = "data_flow_graph.md"
OUTPUT_HTML = "data_flow_graph.html"

MAX_TABLE_ROWS = 150    # rows per table in the .md (the .html shows all)
MD_MAX_EDGES = 50       # Mermaid diagram limit
GRAPH_MAX_NODES = 30    # nodes per side in the SVG graphs
MIN_GLOBAL_NAME_LEN = 4  # shorter shared-field names are too ambiguous to match by name

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


def compact(v, maxlen=200):
    if v is None:
        return "null"
    if isinstance(v, (bool, int, float)):
        return str(v)
    if isinstance(v, str):
        return trunc(oneline(v), maxlen)
    return trunc(oneline(json.dumps(v, ensure_ascii=False, default=str)), maxlen)


def md_table(headers, rows):
    def esc(x):
        return str(x).replace("|", "\\|").replace("\n", " ")
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(esc(c) for c in r) + " |")
    return "\n".join(out)


def mid(s):
    return "n" + re.sub(r"[^0-9A-Za-z]", "_", str(s))


def ml(s):
    return oneline(re.sub(r'[\"\[\]{}|<>#;`]', " ", str(s).replace("&", "and")))


def type_short(tid):
    s = tid[2:] if tid[:2] in ("T:", "M:", "F:") else tid
    return s.split(".")[-1]


def method_name_from_id(mid_):
    head = mid_[2:] if mid_.startswith("M:") else mid_
    return head.split("(")[0].split(".")[-1]


# ============================ PATTERNS ================================
def R(rx):
    return re.compile(rx, re.I)


LIT_RE = re.compile(r'"((?:[^"]|"")*)"')
NAME = r"([\[\]\w\.#$]+)"
SQL_START = re.compile(r"^\s*(select|insert\s+into|update|delete|exec(?:ute)?|with|truncate\s+table|merge)\b", re.I)
SQL_OPEN_END = re.compile(r"\b(from|into|update|join|table|exec|execute)\s*$", re.I)
RX_FROM = re.compile(r"\b(?:from|join)\s+" + NAME, re.I)
RX_INTO = re.compile(r"\binsert\s+into\s+" + NAME, re.I)
RX_UPD = re.compile(r"\bupdate\s+" + NAME + r"\s+set\b", re.I)
RX_DEL = re.compile(r"\bdelete\s+(?:from\s+)?" + NAME, re.I)
RX_EXEC = re.compile(r"\bexec(?:ute)?\s+" + NAME, re.I)
STOP_NAMES = set("the a an your this that list here which you it one set select where and or not null values value top distinct".split())

DB_API = R(r"\bADODB\.|\b(SqlConnection|SqlCommand|SqlDataAdapter|OleDbConnection|OleDbCommand|OleDbDataAdapter|OdbcConnection|OracleConnection|MySqlConnection|ExecuteReader|ExecuteNonQuery|ExecuteScalar)\b")
SP_FLAG = R(r"CommandType\.StoredProcedure")

IO = [  # (store label, direction R/W, regex)
    ("File system", "R", R(r"\bFile\.(ReadAll\w*|OpenRead|OpenText|Exists)\b|\bStreamReader\b|\bDirectory\.(GetFiles|GetDirectories|EnumerateFiles)\b|My\.Computer\.FileSystem\.(ReadAll\w*|OpenText\w*)|\bOpenFileDialog\b")),
    ("File system", "W", R(r"\bFile\.(WriteAll\w*|AppendAll\w*|Create\w*|Copy|Move|Delete)\b|\bStreamWriter\b|\bFileStream\b|\bDirectory\.(CreateDirectory|Delete)\b|\bSaveFileDialog\b|\.SaveAs?\s*\(")),
    ("HTTP / web service", "R", R(r"\b(DownloadString|DownloadFile|DownloadData|GetResponse|GetResponseStream|WebBrowser)\b|\.Navigate\s*\(|HttpClient\.Get")),
    ("HTTP / web service", "W", R(r"\b(UploadString|UploadFile|UploadValues|UploadData|PostAsync|PutAsync)\b")),
    ("Mail (SMTP)", "W", R(r"\b(SmtpClient|MailMessage)\b|System\.Net\.Mail")),
    ("Printer", "W", R(r"\b(PrintDocument|PrintDialog|PrintPreviewDialog|PrintPreviewControl)\b|\.Print\s*\(")),
    ("Camera / video device", "R", R(r"\b(VideoCaptureDevice|FilterInfoCollection|VideoSourcePlayer|NewFrameEventArgs)\b|AForge\.Video")),
    ("Settings / config", "R", R(r"\bConfigurationManager\b|\bAppSettings\b|My\.Settings\.\w+(?!\s*=[^=])|ConnectionStrings")),
    ("Settings / config", "W", R(r"My\.Settings\.\w+\s*=[^=]|My\.Settings\.Save")),
    ("Registry", "R", R(r"Registry\w*\.GetValue|\bOpenSubKey\b")),
    ("Registry", "W", R(r"\b(SetValue|CreateSubKey|DeleteSubKey|DeleteValue)\b")),
    ("Clipboard", "R", R(r"Clipboard\.(GetText|GetImage|GetData)")),
    ("Clipboard", "W", R(r"Clipboard\.(SetText|SetImage|SetData)")),
    ("Serial / socket", "R", R(r"\b(SerialPort|TcpClient|UdpClient|NetworkStream)\b")),
]
CTRL_IN = re.compile(r"\b([A-Za-z_]\w*)\.(Text|SelectedItem|SelectedValue|SelectedIndex|Checked|Value|Image)\b(?!\s*=[^=])")
CTRL_OUT = re.compile(r"^\s*(?:Me\.)?([A-Za-z_]\w*)\.(Text|SelectedValue|SelectedIndex|Checked|Value|Image|DataSource)\s*=[^=]", re.M)
CTRL_OUT2 = re.compile(r"\b([A-Za-z_]\w*)\.(Items\.Add|Rows\.Add|DataSource)\b")
CODE_KEYS = ("rawcode", "normalizedcode", "body", "code", "sourcetext", "initializercode")


# ============================== MODEL =================================
def members_of(t, plural, kind_word):
    v = gi(t, plural)
    if isinstance(v, list) and v:
        return v
    mem = gi(t, "Members")
    if isinstance(mem, list):
        return [m for m in mem if isinstance(m, dict) and str(gi(m, "Kind", default="")).lower() == kind_word.lower()]
    return []


def build_model(ir):
    T = []
    for i, t in enumerate(ir.get("Types") or []):
        tid = gi(t, "Id", default="T:?%d" % i)
        extra = {}
        irf = gi(t, "IrFile")
        if IR_BASE_DIR and irf:
            p = os.path.join(IR_BASE_DIR, irf)
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8-sig") as f:
                        extra = json.load(f)
                except Exception as e:
                    log("could not read per-type IR %s: %s" % (p, e))
        src = gi(t, "SourceFiles", default=[]) or []
        methods = members_of(t, "Methods", "Method") or members_of(extra, "Methods", "Method")
        fields = members_of(t, "Fields", "Field") or members_of(extra, "Fields", "Field")
        mnames = []
        for m in methods:
            mid_ = gi(m, "Id", default="") if isinstance(m, dict) else ""
            mnames.append(method_name_from_id(mid_) if mid_ else (gi(m, "Name", default="?") if isinstance(m, dict) else str(m)))
        T.append({"i": i, "raw": t, "extra": extra, "id": tid, "name": gi(t, "Name", default=type_short(tid)),
                  "full": gi(t, "FullName", default=tid[2:]), "kind": str(gi(t, "Kind", default="?")), "src": src,
                  "methods": methods, "mnames": mnames, "fields": fields,
                  "is_form": any(s.lower().endswith(".designer.vb") for s in src) or "InitializeComponent" in mnames})
    return T


def walk_code(obj, path, out):
    """collect (path, string) for code-like keys; prefer RawCode > NormalizedCode > Body > Code per parent."""
    if isinstance(obj, dict):
        found = {}
        for k, v in obj.items():
            kl = k.lower()
            if kl in CODE_KEYS and isinstance(v, str):
                found[kl] = (path + "." + k, v)
            elif isinstance(v, (dict, list)):
                walk_code(v, path + "." + k, out)
        if "initializercode" in found:
            out.append(found["initializercode"])
        for k in ("rawcode", "normalizedcode", "body", "code", "sourcetext"):
            if k in found:
                out.append(found[k])
                break
    elif isinstance(obj, list):
        for n, v in enumerate(obj):
            walk_code(v, "%s[%d]" % (path, n), out)


def find_keys(obj, path, pred, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = path + "." + k if path else k
            if pred(k.lower()) and v not in (None, "", [], {}):
                out.append((p, compact(v, 220)))
            find_keys(v, p, pred, out)
    elif isinstance(obj, list):
        for n, v in enumerate(obj):
            find_keys(v, "%s[%d]" % (path, n), pred, out)


def ev_prefix(path):
    m = re.match(r"Types\[\d+\](?:\.(?:Methods|Fields|Properties)\[\d+\])?", path)
    return m.group(0) if m else path


def tidx_of(p):
    m = re.match(r"Types\[(\d+)\]", p)
    return int(m.group(1)) if m else None


def clean_table(n):
    n = n.strip("[]").replace("[", "").replace("]", "")
    parts = [p for p in n.split(".") if p]
    if parts and parts[0].lower() == "dbo":
        parts = parts[1:]
    return ".".join(parts)


def sql_statements(code):
    """Return SQL statements assembled from the string literals of one code block."""
    lits = [l.replace('""', '"') for l in LIT_RE.findall(code)]
    out, k = [], 0
    while k < len(lits):
        if SQL_START.match(lits[k]):
            stmt = lits[k]
            while SQL_OPEN_END.search(stmt) and k + 1 < len(lits):
                k += 1
                stmt += " " + lits[k]
            out.append(oneline(stmt))
        k += 1
    return out


def parse_sql(stmt):
    """-> list of (table_or_proc, operation) with operation in C R U D X."""
    ops = []
    low = stmt.lower().lstrip()
    if low.startswith("insert"):
        for m in RX_INTO.finditer(stmt):
            ops.append((m.group(1), "C"))
    elif low.startswith("update"):
        for m in RX_UPD.finditer(stmt):
            ops.append((m.group(1), "U"))
    elif low.startswith("delete"):
        for m in RX_DEL.finditer(stmt):
            ops.append((m.group(1), "D"))
    elif low.startswith(("exec", "execute")):
        for m in RX_EXEC.finditer(stmt):
            ops.append((m.group(1), "X"))
    if low.startswith(("select", "with")) or " select " in low:
        for m in RX_FROM.finditer(stmt):
            ops.append((m.group(1), "R"))
    res = []
    for n, op in ops:
        n = clean_table(n)
        if n and n.lower() not in STOP_NAMES and not n.isdigit():
            res.append((n, op))
    return res


# ========================== DATA FLOW EXTRACTION ======================
def build_flows(ir, T):
    tn = dict((x["i"], x) for x in T)
    code = []
    for x in T:
        walk_code(x["raw"], "Types[%d]" % x["i"], code)
        if x["extra"]:
            walk_code(x["extra"], "Types[%d]" % x["i"], code)
    # stores[(kind, name)] -> {"R": {ti: set(ev)}, "W": ..., ops: Counter of CRUD per type}
    stores = defaultdict(lambda: {"R": defaultdict(set), "W": defaultdict(set), "crud": defaultdict(Counter), "sql": []})
    mflow = {}   # evidence path -> dict(reads, writes, inputs, outputs)
    type_code = defaultdict(list)
    n_sql = 0
    unparsed_db = defaultdict(set)

    def flow(ev):
        return mflow.setdefault(ev, {"reads": set(), "writes": set(), "in": set(), "out": set()})

    for p, s in code:
        ti, ev = tidx_of(p), ev_prefix(p)
        if ti is None:
            continue
        type_code[ti].append(s)
        f = flow(ev)
        got_sql = False
        for stmt in sql_statements(s):
            for name, op in parse_sql(stmt):
                got_sql = True
                n_sql += 1
                if op == "X":
                    st = stores[("Stored procedure", name)]
                    st["W"][ti].add(ev)
                    st["crud"][ti]["X"] += 1
                    f["writes"].add("proc " + name)
                else:
                    st = stores[("Table", name)]
                    st["crud"][ti][op] += 1
                    if op == "R":
                        st["R"][ti].add(ev)
                        f["reads"].add(name)
                    else:
                        st["W"][ti].add(ev)
                        f["writes"].add(name)
                if len(st["sql"]) < 3:
                    st["sql"].append(trunc(stmt, 160))
        if SP_FLAG.search(s):
            st = stores[("Stored procedure", "(name not visible: CommandType.StoredProcedure)")]
            st["W"][ti].add(ev)
            st["crud"][ti]["X"] += 1
            f["writes"].add("stored procedure (name not visible)")
        if DB_API.search(s) and not got_sql:
            unparsed_db[ti].add(ev)
        for label, d, rx in IO:
            if rx.search(s):
                st = stores[("External", label)]
                st[d][ti].add(ev)
                (f["reads"] if d == "R" else f["writes"]).add(label)
        f["in"].update(m.group(1) for m in CTRL_IN.finditer(s))
        f["out"].update(m.group(1) for m in CTRL_OUT.finditer(s))
        f["out"].update(m.group(1) for m in CTRL_OUT2.finditer(s))
    for ti, evs in unparsed_db.items():
        st = stores[("Database", "(SQL not visible in literals)")]
        for ev in evs:
            st["R"][ti].add(ev)
            st["W"][ti].add(ev)
            flow(ev)["reads"].add("database (SQL not visible)")
    # drop method flows that touch nothing
    mflow = dict((k, v) for k, v in mflow.items() if v["reads"] or v["writes"])

    # --- globals: Module fields or Shared fields ---
    globs = []
    for x in T:
        for k, f in enumerate(x["fields"]):
            if not isinstance(f, dict):
                continue
            if str(gi(f, "SourceFile", default="")).lower().endswith(".designer.vb"):
                continue
            mods = [str(m).lower() for m in (gi(f, "Modifiers", default=[]) or [])]
            if (x["kind"].lower() == "module" or "shared" in mods) and not gi(f, "IsConst"):
                nm = str(gi(f, "Name", default=""))
                if len(nm) >= MIN_GLOBAL_NAME_LEN:
                    globs.append({"type": x["i"], "name": nm, "module": x["kind"].lower() == "module", "ev": "Types[%d].Fields[%d]" % (x["i"], k)})
    cnt = Counter(g["name"].lower() for g in globs)
    guse = []
    for g in globs:
        if cnt[g["name"].lower()] > 1:
            continue
        nm = re.escape(g["name"])
        rx = re.compile(r"\b(?:%s\.)?%s\b" % (re.escape(tn[g["type"]]["name"]), nm) if g["module"] else r"\b%s\.%s\b" % (re.escape(tn[g["type"]]["name"]), nm), re.I)
        w_rx = re.compile(r"(?:\b%s\.)?\b%s\b\s*=[^=]" % (re.escape(tn[g["type"]]["name"]), nm), re.I)
        rd, wr = set(), set()
        for ti, texts in type_code.items():
            if ti == g["type"]:
                continue
            blob = "\n".join(texts)
            if rx.search(blob):
                (wr if w_rx.search(blob) else rd).add(ti)
                if w_rx.search(blob) and len(rx.findall(blob)) > len(w_rx.findall(blob)):
                    rd.add(ti)
        if rd or wr:
            guse.append((g, rd, wr))
    sp = []
    find_keys(ir.get("Types") or [], "Types", lambda k: "storedproc" in k, sp)
    return {"stores": stores, "mflow": mflow, "globs": globs, "guse": guse, "n_sql": n_sql, "sp": sp, "unparsed_db": unparsed_db}


# ============================ GRAPH DRAWING ===========================
def svg_graph(left, right, edges, lhead, rhead):
    """left/right: list of (id, label). edges: list of (lid, rid, weight, tooltip, cls) cls in r / w / rw."""
    row, pad, W, bw = 24, 36, 980, 250
    lp = dict((i, pad + k * row) for k, (i, _) in enumerate(left))
    rp = dict((i, pad + k * row) for k, (i, _) in enumerate(right))
    H = pad + max(len(left), len(right), 1) * row + 12
    x1, x2 = 10 + bw, W - 10 - bw
    o = ['<svg class="g" viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg" role="img">' % (W, H),
         '<text x="10" y="18" class="h">%s</text><text x="%d" y="18" class="h">%s</text>' % (html.escape(lhead), W - 10 - bw, html.escape(rhead))]
    for l, r, w, tip, cls in edges:
        if l in lp and r in rp:
            y1, y2 = lp[l] + 9, rp[r] + 9
            xm = (x1 + x2) / 2.0
            o.append('<path class="e %s" d="M%d,%d C%d,%d %d,%d %d,%d" stroke-width="%.1f"><title>%s</title></path>' % (
                cls, x1, y1, xm, y1, xm, y2, x2, y2, 1 + min(3.5, math.log(w + 1, 2) * 0.6), html.escape(tip)))
    for (i, lab) in left:
        o.append('<g><rect class="n" x="10" y="%d" width="%d" height="19" rx="4"/><text x="18" y="%d">%s</text></g>' % (lp[i], bw, lp[i] + 14, html.escape(trunc(lab, 38))))
    for (i, lab) in right:
        o.append('<g><rect class="n" x="%d" y="%d" width="%d" height="19" rx="4"/><text x="%d" y="%d">%s</text></g>' % (x2, rp[i], bw, x2 + 8, rp[i] + 14, html.escape(trunc(lab, 38))))
    o.append("</svg>")
    return "".join(o)


# ============================== SECTIONS ==============================
def direction(st, ti):
    r, w = ti in st["R"], ti in st["W"]
    return "rw" if (r and w) else ("r" if r else "w")


def build_sections(ir, T, F):
    tn = dict((x["i"], x) for x in T)
    MET = ir.get("Metrics") or {}
    AS = ir.get("AnalysisStatus") or {}
    stores = F["stores"]
    S = []
    warn = (AS.get("Authoritative") is False) or bool(AS.get("CompilationErrors"))
    banner = ("Authoritative=%s; compilation errors=%s. %s" % (
        AS.get("Authoritative"), AS.get("CompilationErrors"),
        "Flows are taken from the code text, which is still complete; however SQL built at run time from variables is not visible, and calls between methods are not followed (flows are per method and per type)." if warn
        else "No analysis warning recorded. Flows are per method and per type; SQL built at run time is not visible.")) if (AS or MET) else "AnalysisStatus/Metrics not present: confidence unknown."
    tables = [(k, v) for k, v in stores.items() if k[0] == "Table"]
    procs = [(k, v) for k, v in stores.items() if k[0] == "Stored procedure"]
    ext = [(k, v) for k, v in stores.items() if k[0] in ("External", "Database")]
    S.append(("h", "1. Summary"))
    S.append(("t", ["Fact", "Value"], [
        ["Types read", len(T)],
        ["SQL statements parsed from string literals", F["n_sql"]],
        ["Database tables found", len(tables)],
        ["Stored procedure references", len(procs)],
        ["Metrics.StoredProcedureCalls (from the IR)", MET.get("StoredProcedureCalls")],
        ["Other data stores / endpoints (files, HTTP, mail, printer, device, settings, ...)", len([1 for k, _ in ext if k[0] == "External"])],
        ["Methods with a data flow", len(F["mflow"])],
        ["Global / shared variables used across types", len(F["guse"])]]))
    S.append(("banner", banner, warn))

    # ---- stores inventory ----
    S.append(("h", "2. Data stores and endpoints"))
    rows = []
    for (kind, name), st in sorted(stores.items(), key=lambda kv: (kv[0][0], kv[0][1].lower())):
        evn = len(set(e for v in st["R"].values() for e in v) | set(e for v in st["W"].values() for e in v))
        rows.append([kind, name, ", ".join(tn[i]["name"] for i in sorted(st["R"]))[:100] or "-",
                     ", ".join(tn[i]["name"] for i in sorted(st["W"]))[:100] or "-", evn])
    S.append(("t", ["Kind", "Name", "Read by (types)", "Written by (types)", "Evidence (methods)"], rows) if rows else
             ("p", "No data stores were detected. If the project clearly uses a database, the SQL is probably built at run time or stored outside the literals scanned."))

    # ---- graph ----
    S.append(("h", "3. Data flow graph (types and the data stores they touch)"))
    if stores:
        tw = Counter()
        sw = Counter()
        pairs = {}
        for key, st in stores.items():
            label = "%s: %s" % (key[0], key[1]) if key[0] in ("Table", "Stored procedure") else key[1]
            for ti in set(st["R"]) | set(st["W"]):
                w = len(st["R"].get(ti, set()) | st["W"].get(ti, set()))
                pairs[(ti, label)] = (w, direction(st, ti), key)
                tw[ti] += w
                sw[label] += w
        right = [k for k, _ in sw.most_common(GRAPH_MAX_NODES)]
        left_ids = [i for i, _ in tw.most_common(GRAPH_MAX_NODES)]
        edges = []
        for (ti, label), (w, cls, key) in pairs.items():
            if ti in left_ids and label in right:
                ops = "".join(sorted(stores[key]["crud"][ti].keys()))
                edges.append((ti, label, w, "%s %s %s: %d method(s)%s" % (tn[ti]["name"], {"r": "reads", "w": "writes", "rw": "reads and writes"}[cls], label, w, (" [%s]" % ops) if ops else ""), cls))
        S.append(("p", "Blue = type reads from the store, orange = type writes to it, purple = both. Hover an arrow for details. Evidence per method is in section 5."))
        S.append(("svg", svg_graph([(i, tn[i]["name"]) for i in left_ids], [(k, k) for k in right], edges, "Type (UI / logic)", "Data store / endpoint")))
        m = ["flowchart LR"]
        used = set()
        for ti, label, w, tip, cls in sorted(edges, key=lambda z: -z[2])[:MD_MAX_EDGES]:
            a, b = mid("T%d" % ti), mid("S" + label)
            used.add((a, tn[ti]["name"]))
            used.add((b, label))
            if cls == "r":
                m.append('  %s -->|"read %d"| %s' % (b, w, a))
            elif cls == "w":
                m.append('  %s -->|"write %d"| %s' % (a, w, b))
            else:
                m.append('  %s <-->|"read/write %d"| %s' % (a, w, b))
        for a, lab in sorted(used):
            m.insert(1, '  %s["%s"]' % (a, ml(trunc(lab, 40))))
        S.append(("m", "```mermaid\n" + "\n".join(m) + "\n```"))
    else:
        S.append(("p", "No flows to draw."))

    # ---- CRUD ----
    S.append(("h", "4. Table CRUD matrix"))
    crow = []
    for (kind, name), st in sorted(tables, key=lambda kv: kv[0][1].lower()):
        by = defaultdict(set)
        for ti, c in st["crud"].items():
            for op in c:
                by[op].add(tn[ti]["name"])
        crow.append([name] + [", ".join(sorted(by[o]))[:70] or "-" for o in ("C", "R", "U", "D")] + [st["sql"][0] if st["sql"] else ""])
    S.append(("t", ["Table", "Create (INSERT)", "Read (SELECT/JOIN)", "Update", "Delete", "Sample SQL"], crow) if crow else
             ("p", "No tables could be parsed from SQL literals. Check the Open Questions."))

    # ---- per method ----
    S.append(("h", "5. Data flow per method"))
    S.append(("p", "Inputs/outputs are UI controls whose .Text / SelectedValue / Checked / DataSource is read or assigned in the method (name based). "
                   "A control name in 'Inputs' next to a write is a likely data-entry-to-store flow (Assumption: the value is what gets written)."))
    rows = []
    for ev, f in sorted(F["mflow"].items(), key=lambda kv: kv[0]):
        ti = tidx_of(ev)
        mj = re.search(r"Methods\[(\d+)\]", ev)
        nm = ""
        if mj and ti is not None and int(mj.group(1)) < len(tn[ti]["mnames"]):
            nm = tn[ti]["mnames"][int(mj.group(1))]
        rows.append(["%s.%s" % (tn[ti]["name"], nm) if nm else (tn[ti]["name"] if ti is not None else "?"), ", ".join(sorted(f["in"]))[:90] or "-",
                     ", ".join(sorted(f["reads"]))[:90] or "-", ", ".join(sorted(f["writes"]))[:90] or "-", ", ".join(sorted(f["out"]))[:90] or "-", ev])
    S.append(("t", ["Method", "Inputs (controls)", "Reads", "Writes", "Outputs (controls)", "JSON"], rows) if rows else ("p", "No methods with a detectable data flow."))

    # ---- globals ----
    S.append(("h", "6. Global / shared state carrying data between types"))
    S.append(("p", "Fields of Modules or Shared fields, matched by name in the code of other types (Assumption: name match only; 'writes' = an assignment to the name was seen)."))
    grow = []
    for g, rd, wr in sorted(F["guse"], key=lambda z: -(len(z[1]) + len(z[2]))):
        grow.append(["%s.%s" % (tn[g["type"]]["name"], g["name"]), ", ".join(sorted(tn[i]["name"] for i in wr)) or "-", ", ".join(sorted(tn[i]["name"] for i in rd)) or "-", g["ev"]])
    if grow:
        S.append(("t", ["Global", "Written by", "Read by", "JSON"], grow))
        gl = ["%s.%s" % (tn[g["type"]]["name"], g["name"]) for g, _, _ in F["guse"]]
        left_ids = sorted(set(i for g, rd, wr in F["guse"] for i in (rd | wr)))[:GRAPH_MAX_NODES]
        edges = []
        for g, rd, wr in F["guse"]:
            lab = "%s.%s" % (tn[g["type"]]["name"], g["name"])
            for i in left_ids:
                if i in rd or i in wr:
                    edges.append((i, lab, 1, "%s %s %s" % (tn[i]["name"], "writes" if i in wr else "reads", lab), "rw" if (i in rd and i in wr) else ("w" if i in wr else "r")))
        S.append(("svg", svg_graph([(i, tn[i]["name"]) for i in left_ids], [(k, k) for k in gl[:GRAPH_MAX_NODES]], edges, "Type", "Global / shared variable")))
    else:
        S.append(("p", "No global or shared variables used across types were found (or none is long enough to match by name: minimum %d characters)." % MIN_GLOBAL_NAME_LEN))

    # ---- stored procs ----
    S.append(("h", "7. Stored procedures"))
    S.append(("p", "Metrics.StoredProcedureCalls=%s. Names come from EXEC literals or from IR keys named *StoredProc*; calls through CommandType.StoredProcedure with the name in a variable are listed without a name." % MET.get("StoredProcedureCalls")))
    prow = [[k[1], ", ".join(sorted(tn[i]["name"] for i in st["W"]))[:100], ", ".join(sorted(set(e for v in st["W"].values() for e in v))[:3])] for k, st in sorted(procs, key=lambda kv: kv[0][1])]
    prow += [["(IR key) " + v, "-", p] for p, v in F["sp"]]
    S.append(("t", ["Procedure", "Called from (types)", "Evidence (JSON)"], prow) if prow else ("p", "No stored procedure references found."))

    # ---- questions ----
    S.append(("h", "8. Assumptions and open questions"))
    oq = ["Only SQL written as string literals is parsed. SQL assembled at run time, loaded from files or hidden in stored procedures is not visible. Ask: a developer of the project or the DBA.",
          "Direction (read/write) comes from SQL verbs and API names; it says what a method does to a store, not which values move. Per-column flows are not derived (Assumption).",
          "Flows are per method and per type. Chains such as form -> helper -> database are shown through the helper's own flow, not stitched together; combine with the API graph for call chains.",
          "Table names are taken as written in SQL; views, aliases and temp tables cannot be told apart from tables (Assumption)."]
    if not F["n_sql"] and F["unparsed_db"]:
        oq.append("Database API use was found but no SQL literal could be parsed: the database access is hidden behind variables or another layer. Ask: a developer of the project.")
    if not any(isinstance(m, dict) for x in T for m in x["methods"]):
        oq.append("No Methods were found under Types[]. Set IR_BASE_DIR to the folder with the per-type IR files (IrFile).")
    if warn:
        oq.append("The IR analysis is not authoritative; re-run it with the .NET reference assemblies for complete type information. Ask: the owner of the analysis tool.")
    S.append(("list", oq))
    return S


# ============================== RENDERING =============================
def to_md(S, meta):
    out = ["# Data Flow Graph - %s" % meta["project"],
           "_Generated %s from the IR JSON (schema %s, IR generated %s)._" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), meta["schema"], meta["generated"] or "time not recorded"),
           "_The HTML version (`%s`) has the coloured graphs and a filter box._" % OUTPUT_HTML]
    for s in S:
        k = s[0]
        if k == "h":
            out.append("## " + s[1])
        elif k == "p":
            out.append(s[1])
        elif k == "banner":
            out.append(("> **Confidence warning.** " if s[2] else "> **Confidence.** ") + s[1])
        elif k == "t":
            rows = s[2]
            extra = "\n\n_%d more rows are in the HTML._" % (len(rows) - MAX_TABLE_ROWS) if len(rows) > MAX_TABLE_ROWS else ""
            out.append((md_table(s[1], rows[:MAX_TABLE_ROWS]) if rows else "none") + extra)
        elif k == "m":
            out.append(s[1])
        elif k == "list":
            out.append("\n".join("- " + q for q in s[1]))
    return "\n\n".join(out) + "\n"


HTML = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title><style>
:root{--bg:#fff;--panel:#f6f7f9;--text:#1c2330;--sub:#5b6578;--line:#d5d9e2;--read:#2563eb;--write:#ea580c;--both:#7c3aed;--warn-bg:#fff4e0;--warn-tx:#7a4a00;--ok-bg:#eaf3ff;--ok-tx:#1d4570}
@media (prefers-color-scheme:dark){:root{--bg:#16181d;--panel:#1e2128;--text:#e6e8ee;--sub:#9aa3b5;--line:#343a46;--read:#60a5fa;--write:#fb923c;--both:#a78bfa;--warn-bg:#3a2e14;--warn-tx:#f2cf8a;--ok-bg:#1c2a3d;--ok-tx:#a9c8ee}}
body{margin:0;font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--text)}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 18px;z-index:3}
h1{font-size:18px;margin:0}.sub{color:var(--sub);font-size:12.5px}
main{max-width:1180px;margin:0 auto;padding:6px 18px 40px}h2{font-size:16px;margin:26px 0 6px;border-bottom:1px solid var(--line);padding-bottom:4px}
input{border:1px solid var(--line);background:var(--bg);color:var(--text);border-radius:6px;padding:5px 9px;font:inherit;min-width:240px}
table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}th,td{border-bottom:1px solid var(--line);padding:4px 7px;text-align:left;vertical-align:top}th{color:var(--sub);position:sticky;top:58px;background:var(--bg)}
.banner{padding:8px 11px;border-radius:6px;background:var(--ok-bg);color:var(--ok-tx);margin:10px 0}.banner.warn{background:var(--warn-bg);color:var(--warn-tx)}
svg.g{width:100%;height:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px;margin:8px 0}
svg.g .n{fill:var(--bg);stroke:var(--line)}svg.g text{fill:var(--text);font-size:12px}svg.g text.h{fill:var(--sub);font-weight:700}
svg.g .e{fill:none;opacity:.4}svg.g .e:hover{opacity:1}svg.g .e.r{stroke:var(--read)}svg.g .e.w{stroke:var(--write)}svg.g .e.rw{stroke:var(--both)}
.key{font-size:12.5px;color:var(--sub)}.key b.r{color:var(--read)}.key b.w{color:var(--write)}.key b.rw{color:var(--both)}.hide{display:none}
</style></head><body><header><h1>__TITLE__</h1><div class="sub">__SUB__</div>
<div style="margin-top:6px"><input id="q" type="search" placeholder="Filter all tables..." aria-label="Filter"></div></header><main>__BODY__</main>
<script>document.getElementById('q').oninput=function(){var q=this.value.toLowerCase();document.querySelectorAll('table tr').forEach(function(r){if(r.querySelector('th'))return;r.classList.toggle('hide',q&&r.textContent.toLowerCase().indexOf(q)<0);});};</script></body></html>"""


def to_html(S, meta):
    e = html.escape
    b = []
    for s in S:
        k = s[0]
        if k == "h":
            b.append("<h2>%s</h2>" % e(s[1]))
        elif k == "p":
            b.append("<p>%s</p>" % e(s[1]))
        elif k == "banner":
            b.append('<div class="banner%s">%s</div>' % (" warn" if s[2] else "", e(s[1])))
        elif k == "t":
            if not s[2]:
                b.append("<p>none</p>")
            else:
                b.append("<table><tr>%s</tr>%s</table>" % ("".join("<th>%s</th>" % e(h) for h in s[1]),
                                                          "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % e(str(c)) for c in r) for r in s[2])))
        elif k == "svg":
            b.append('<div class="key"><b class="r">blue</b> = read, <b class="w">orange</b> = write, <b class="rw">purple</b> = both</div>' + s[1])
        elif k == "list":
            b.append("<ul>%s</ul>" % "".join("<li>%s</li>" % e(q) for q in s[1]))
    title = "Data Flow Graph - %s" % meta["project"]
    sub = "Schema %s - IR generated %s - Markdown version with Mermaid: %s" % (meta["schema"], meta["generated"] or "time not recorded", OUTPUT_MD)
    return HTML.replace("__TITLE__", e(title)).replace("__SUB__", e(sub)).replace("__BODY__", "\n".join(b))


def main():
    if "<PATH" in IR_JSON_PATH or "<PATH" in OUTPUT_DIR:
        sys.exit("Set IR_JSON_PATH and OUTPUT_DIR at the top of the script.")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log("loading %s" % IR_JSON_PATH)
    with open(IR_JSON_PATH, "r", encoding="utf-8-sig") as f:
        ir = json.load(f)
    T = build_model(ir)
    log("extracting data flows from %d types" % len(T))
    F = build_flows(ir, T)
    S = build_sections(ir, T, F)
    meta = {"project": ir.get("ProjectName") or "VB.NET project", "schema": ir.get("SchemaVersion"), "generated": ir.get("GeneratedAtUtc")}
    md_path, html_path = os.path.join(OUTPUT_DIR, OUTPUT_MD), os.path.join(OUTPUT_DIR, OUTPUT_HTML)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(to_md(S, meta))
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(to_html(S, meta))
    log("sql statements=%d stores=%d method flows=%d globals=%d" % (F["n_sql"], len(F["stores"]), len(F["mflow"]), len(F["guse"])))
    log("DONE -> %s" % md_path)
    log("DONE -> %s" % html_path)


if __name__ == "__main__":
    main()
