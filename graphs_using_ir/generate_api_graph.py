#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_api_graph.py  -  VB.NET IR JSON  ->  api_graph.md  +  api_graph.html

Single file, Python 3.8 compatible, standard library only. No LLM, no Graphviz, no internet needed
(same style as generate_dependency_graph.py: two path variables at the top, deterministic output).

WHAT IT ANSWERS
  1. Internal API   : which public methods does each type offer, who calls them, who is never called
  2. External APIs  : what the project consumes: databases / stored procedures, HTTP and web services,
                      mail, sockets / serial, COM / OS / process, file system, printing, devices, 3rd-party libraries
  3. Exposed APIs   : does the project itself expose anything over HTTP or another protocol
                      (ASMX / WCF / Web API, HttpListener, TcpListener, named pipes, remoting, COM server, Windows service)
  4. Endpoints      : URLs found in code (query strings are cut off)

HOW IT IS BUILT
  Public API  = Types[].Methods[] whose Visibility/Modifiers say Public (constructors, designer code, Dispose and
                UI event handlers are listed separately, not as API).
  Internal calls = CallGraph.Edges that point to a known method (exact). Unresolved calls whose first name part is a
                known type (e.g. "Database.connect") are matched by name and marked "inferred" (an Assumption).
  External use = regex over code strings + unresolved call targets + unresolved type references. Every hit keeps its
                JSON path as evidence.
  The confidence banner comes from AnalysisStatus/Metrics: with many unresolved edges, "no caller found" can simply
  mean "not resolved".

INPUT  : the IR JSON only. Set the two paths below.
OUTPUT : api_graph.md (Mermaid + tables) and api_graph.html (one self-contained page with SVG graphs, search box).
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
OUTPUT_MD = "api_graph.md"
OUTPUT_HTML = "api_graph.html"

PUBLIC_VISIBILITIES = ("public",)   # add "friend" to also treat Friend (internal) members as API
INFER_UNRESOLVED_CALLS = True       # match unresolved call targets such as "Database.connect" to known types by name
MAX_TABLE_ROWS = 150                # rows per table in the .md (the .html shows all)
MD_MAX_EDGES = 50                   # Mermaid diagram limit
GRAPH_MAX_NODES = 30                # nodes per side in the SVG graphs

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


def method_type(mid_):
    if not isinstance(mid_, str) or not mid_.startswith("M:"):
        return None
    head = mid_[2:].split("(")[0]
    return ("T:" + head.rsplit(".", 1)[0]) if "." in head else None


def method_sig(mid_):
    head = mid_[2:] if mid_.startswith("M:") else mid_
    name = head.split("(")[0].split(".")[-1]
    args = head[head.index("("):] if "(" in head else ""
    return name + args


def vendor_of(name):
    name = str(name).strip()
    if name.startswith("System.Windows.Forms"):
        return "WinForms"
    if name.startswith("System."):
        return ".".join(name.split(".")[:2])
    if "." in name:
        return re.split(r"[.<(]", name)[0]
    return "(unqualified)"


def vendor_from_dep(name):
    base = re.sub(r"[._]v?\d+(?:[._]\d+)*(?:\.\w+)?$", "", name)
    return re.split(r"[._]", base)[0]


def version_from_dep(name):
    mv = re.search(r"Version=(\d+(?:\.\d+)+)", name)
    if mv:
        return mv.group(1)
    m = re.search(r"[._]v?(\d+(?:[._]\d+)+)", name)
    return m.group(1).replace("_", ".") if m else ""


# ============================ PATTERNS ================================
def R(rx):
    return re.compile(rx, re.I)


CLASSES = [  # (category, API label, regex)
    ("Database", "ADODB (classic ADO)", R(r"\bADODB\.")),
    ("Database", "SQL Server (ADO.NET)", R(r"\b(SqlConnection|SqlCommand|SqlDataAdapter|SqlDataReader)\b")),
    ("Database", "OleDb", R(r"\bOleDb(Connection|Command|DataAdapter|DataReader)\b")),
    ("Database", "ODBC", R(r"\bOdbc(Connection|Command|DataAdapter)\b")),
    ("Database", "Oracle", R(r"\bOracle(Connection|Command)\b")),
    ("Database", "MySQL", R(r"\bMySql(Connection|Command)\b")),
    ("Database", "Execute* calls", R(r"\b(ExecuteReader|ExecuteNonQuery|ExecuteScalar)\b")),
    ("Database", "Stored procedure call", R(r"CommandType\.StoredProcedure|\bexec(?:ute)?\s+[A-Za-z_]\w*")),
    ("Web / HTTP client", "HTTP request classes", R(r"\b(HttpWebRequest|HttpWebResponse|WebRequest|WebClient|HttpClient|DownloadString|DownloadFile|UploadString|UploadFile)\b")),
    ("Web / HTTP client", "SOAP / WCF client", R(r"\b(ServiceReference|WebReference|SoapHttpClientProtocol|ChannelFactory|ClientBase)\b")),
    ("Web / HTTP client", "Embedded browser / Navigate", R(r"\bWebBrowser\b|\.Navigate\s*\(")),
    ("Mail", "SMTP / mail", R(r"\b(SmtpClient|MailMessage|System\.Net\.Mail)\b|Outlook\.Application")),
    ("Network / serial / IPC", "FTP", R(r"\bFtpWebRequest\b")),
    ("Network / serial / IPC", "Sockets / serial / pipes", R(r"\b(TcpClient|UdpClient|NetworkStream|SerialPort|NamedPipeClientStream)\b|\bSocket\b")),
    ("OS / COM / process", "Late-bound COM (CreateObject)", R(r"\bCreateObject\s*\(")),
    ("OS / COM / process", "Process / Shell", R(r"\bProcess\.Start\b|\bShell\s*\(")),
    ("OS / COM / process", "P/Invoke (DllImport / Declare)", R(r"\bDllImport\b|\bDeclare\s+(Function|Sub)\b")),
    ("OS / COM / process", "Registry", R(r"\bRegistry\b|\bRegistryKey\b")),
    ("OS / COM / process", "Office interop", R(r"Microsoft\.Office\.Interop|\bExcel\.Application\b|\bWord\.Application\b")),
    ("File system", "File / directory / streams", R(r"\b(File|Directory|Path)\.(Read|Write|Append|Exists|Delete|Copy|Move|Create|Get)\w*|\b(StreamReader|StreamWriter|FileStream)\b|My\.Computer\.FileSystem")),
    ("Printing", "Print classes", R(r"\b(PrintDocument|PrintDialog|PrintPreviewDialog|PrintPreviewControl)\b")),
    ("Devices / media", "Camera / video (AForge)", R(r"\b(VideoCaptureDevice|FilterInfoCollection|VideoSourcePlayer)\b|AForge\.Video")),
]

EXPOSE = [  # things the project itself could EXPOSE
    ("Exposed", "ASMX web service", R(r"\bWebMethod\b|System\.Web\.Services|\bWebService\b")),
    ("Exposed", "WCF service", R(r"\b(ServiceContract|OperationContract|ServiceHost|WebServiceHost|WebGet|WebInvoke)\b")),
    ("Exposed", "ASP.NET Web API / MVC", R(r"\b(ApiController|ControllerBase|HttpGet|HttpPost|HttpPut|HttpDelete|RoutePrefix)\b")),
    ("Exposed", "HTTP handler / module", R(r"\b(IHttpHandler|IHttpModule)\b")),
    ("Exposed", "HTTP listener", R(r"\bHttpListener\b")),
    ("Exposed", "TCP server / named-pipe server", R(r"\b(TcpListener|NamedPipeServerStream)\b")),
    ("Exposed", ".NET Remoting / COM server", R(r"\b(RemotingConfiguration|MarshalByRefObject|ComVisible|ClassInterface)\b")),
    ("Exposed", "Windows service", R(r"\b(ServiceBase|ServiceInstaller)\b")),
]
URL_RE = re.compile(r"https?://[^\s\"'<>)]+", re.I)
CODE_KEYS = ("rawcode", "normalizedcode", "body", "code", "sourcetext", "initializercode")
GENERATED = ("initializecomponent", "dispose", ".ctor", "#ctor", "new")
HANDLER_RE = re.compile(r"_[A-Za-z]+$")


# ============================ MODEL ===================================
def members_of(t, plural, kind_word):
    v = gi(t, plural)
    if isinstance(v, list) and v:
        return v
    mem = gi(t, "Members")
    if isinstance(mem, list):
        return [m for m in mem if isinstance(m, dict) and str(gi(m, "Kind", default="")).lower() == kind_word.lower()]
    return []


def visibility(m):
    if not isinstance(m, dict):
        return None
    v = gi(m, "Visibility")
    if isinstance(v, str) and v:
        return v.lower()
    for x in gi(m, "Modifiers", default=[]) or []:
        if str(x).lower() in ("public", "private", "friend", "protected", "protected friend"):
            return str(x).lower()
    return None


def build_model(ir):
    types = ir.get("Types") or []
    T = []
    for i, t in enumerate(types):
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
        methods = members_of(t, "Methods", "Method") or members_of(extra, "Methods", "Method")
        src = gi(t, "SourceFiles", default=[]) or []
        base = gi(t, "BaseClass")
        base_txt = gi(base, "Text", default="") if isinstance(base, dict) else (base or "")
        mrecs = []
        for j, m in enumerate(methods):
            mid_ = gi(m, "Id", default="") if isinstance(m, dict) else ""
            name = method_name_from_id(mid_) if mid_ else (gi(m, "Name", default="?") if isinstance(m, dict) else str(m))
            args = mid_[mid_.index("("):] if "(" in mid_ else ""
            mrecs.append({"j": j, "id": mid_, "name": name, "sig": name + args, "vis": visibility(m), "raw": m,
                          "handler": ("EventArgs" in mid_) or (bool(HANDLER_RE.search(name)) and "EventArgs" in args)})
        is_form = any(s.lower().endswith(".designer.vb") for s in src) or any(r["name"] == "InitializeComponent" for r in mrecs)
        T.append({"i": i, "raw": t, "extra": extra, "id": tid, "name": gi(t, "Name", default=type_short(tid)),
                  "full": gi(t, "FullName", default=tid[2:]), "kind": gi(t, "Kind", default="?"), "src": src,
                  "base": base_txt, "ifaces": [gi(x, "Text", default=str(x)) if isinstance(x, dict) else str(x) for x in (gi(t, "Interfaces", default=[]) or [])],
                  "attrs": [str(a) for a in (gi(t, "Attributes", default=[]) or [])], "methods": mrecs, "is_form": is_form,
                  "generated": ".My." in tid})
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


# ========================== API EXTRACTION ============================
def build_api(ir, T):
    idx_of = dict((x["id"], x["i"]) for x in T)
    mpath = {}          # method id -> evidence path
    mrec = {}           # method id -> (type index, record)
    for x in T:
        for r in x["methods"]:
            if r["id"]:
                mpath[r["id"]] = "Types[%d].Methods[%d]" % (x["i"], r["j"])
                mrec[r["id"]] = (x["i"], r)
    name_to_type = defaultdict(list)
    for x in T:
        name_to_type[x["name"].lower()].append(x["i"])
    mnames = dict((x["i"], dict((r["name"].lower(), r["id"]) for r in x["methods"] if r["id"])) for x in T)

    # --- external use from code strings, declarations and unresolved call targets ---
    use = defaultdict(lambda: defaultdict(set))     # (cat, label) -> type idx -> evidence set
    exp = defaultdict(lambda: defaultdict(set))
    urls = defaultdict(set)

    def add(table, cat, label, ti, ev):
        table[(cat, label)][ti].add(ev)

    code = []
    for x in T:
        walk_code(x["raw"], "Types[%d]" % x["i"], code)
        if x["extra"]:
            walk_code(x["extra"], "Types[%d]" % x["i"], code)
    for p, s in code:
        ti, ev = tidx_of(p), ev_prefix(p)
        if ti is None:
            continue
        for cat, label, rx in CLASSES:
            if rx.search(s):
                add(use, cat, label, ti, ev)
        for cat, label, rx in EXPOSE:
            if rx.search(s):
                add(exp, cat, label, ti, ev)
        for u in URL_RE.findall(s):
            urls[re.sub(r"[?#].*$", "", u).rstrip(".,;")].add((ti, ev))
    for x in T:  # declarations: attributes, base class, interfaces, method attributes
        decl = " ".join(x["attrs"] + [x["base"]] + x["ifaces"])
        for cat, label, rx in EXPOSE:
            if rx.search(decl):
                add(exp, cat, label, x["i"], "Types[%d]" % x["i"])
            for r in x["methods"]:
                at = gi(r["raw"], "Attributes", default=[]) if isinstance(r["raw"], dict) else []
                if rx.search(" ".join(str(a) for a in (at or []))):
                    add(exp, cat, label, x["i"], "Types[%d].Methods[%d]" % (x["i"], r["j"]))

    # --- call graph ---
    cg = ir.get("CallGraph") or {}
    tt_edges = defaultdict(lambda: {"exact": 0, "inferred": 0, "methods": Counter()})
    callers_of = defaultdict(lambda: defaultdict(lambda: [0, 0]))   # callee method id -> caller type -> [exact, inferred]
    callees_of = Counter()
    unres_roots = Counter()
    unres_total = 0
    for n, e in enumerate(cg.get("Edges") or []):
        frm, to = gi(e, "From"), gi(e, "To")
        ft = idx_of.get(method_type(frm)) if isinstance(frm, str) else None
        if ft is None:
            continue
        fpath = mpath.get(frm, "CallGraph.Edges[%d]" % n)
        if to in mrec:
            tt = mrec[to][0]
            if tt != ft:
                tt_edges[(ft, tt)]["exact"] += 1
                tt_edges[(ft, tt)]["methods"][method_name_from_id(to)] += 1
                callers_of[to][ft][0] += 1
            callees_of[frm] += 1
            continue
        if not isinstance(to, str) or ":" not in to:
            continue
        unres_total += 1
        disp = str(gi(e, "TargetDisplay", default=to.split(":", 1)[1]))
        for cat, label, rx in CLASSES:
            if rx.search(disp):
                add(use, cat, label, ft, fpath)
        for cat, label, rx in EXPOSE:
            if rx.search(disp):
                add(exp, cat, label, ft, fpath)
        parts = [p.strip() for p in re.sub(r"\(.*$", "", disp).split(".") if p.strip()]
        if not parts:
            continue
        cand = name_to_type.get(parts[0].lower(), []) if INFER_UNRESOLVED_CALLS else []
        if len(cand) == 1 and cand[0] != ft:
            tt = cand[0]
            tt_edges[(ft, tt)]["inferred"] += 1
            target_m = mnames[tt].get(parts[1].lower()) if len(parts) > 1 else None
            tt_edges[(ft, tt)]["methods"][parts[1] if len(parts) > 1 else "(type)"] += 1
            if target_m:
                callers_of[target_m][ft][1] += 1
            callees_of[frm] += 1
        else:
            unres_roots[vendor_of(".".join(parts[:3])) if len(parts) > 1 else parts[0]] += 1
    return {"use": use, "exp": exp, "urls": urls, "tt": tt_edges, "callers_of": callers_of, "callees_of": callees_of,
            "unres_roots": unres_roots, "unres_total": unres_total, "mpath": mpath}


def third_party(ir, T):
    idx_of = dict((x["id"], x["i"]) for x in T)
    tg = ir.get("TypeDependencyGraph") or {}
    per = defaultdict(Counter)
    for e in tg.get("Edges") or []:
        frm, to = gi(e, "From"), gi(e, "To")
        if frm in idx_of and isinstance(to, str) and to.startswith("unresolved-type:"):
            v = vendor_of(gi(e, "TargetDisplay", default=to[len("unresolved-type:"):]))
            if v not in ("WinForms", "(unqualified)") and not v.startswith("System"):
                per[v][idx_of[frm]] += 1
    deps = defaultdict(list)
    for d in ir.get("ExternalDependencies") or []:
        deps[vendor_from_dep(str(d))].append(str(d))
    return per, deps


# ============================ GRAPH DRAWING ===========================
def svg_graph(left, right, edges, lhead, rhead):
    """left/right: list of (id, label). edges: list of (lid, rid, weight, tooltip)."""
    row, pad, W, bw = 24, 36, 980, 250
    lp = dict((i, pad + k * row) for k, (i, _) in enumerate(left))
    rp = dict((i, pad + k * row) for k, (i, _) in enumerate(right))
    H = pad + max(len(left), len(right), 1) * row + 12
    x1, x2 = 10 + bw, W - 10 - bw
    o = ['<svg class="g" viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg" role="img">' % (W, H),
         '<text x="10" y="18" class="h">%s</text><text x="%d" y="18" class="h">%s</text>' % (html.escape(lhead), W - 10 - bw, html.escape(rhead))]
    for l, r, w, tip in edges:
        if l in lp and r in rp:
            y1, y2 = lp[l] + 9, rp[r] + 9
            xm = (x1 + x2) / 2.0
            o.append('<path class="e" d="M%d,%d C%d,%d %d,%d %d,%d" stroke-width="%.1f"><title>%s</title></path>' % (
                x1, y1, xm, y1, xm, y2, x2, y2, 1 + min(3.5, math.log(w + 1, 2) * 0.6), html.escape(tip)))
    for (i, lab) in left:
        o.append('<g><rect class="n" x="10" y="%d" width="%d" height="19" rx="4"/><text x="18" y="%d">%s</text></g>' % (lp[i], bw, lp[i] + 14, html.escape(trunc(lab, 38))))
    for (i, lab) in right:
        o.append('<g><rect class="n" x="%d" y="%d" width="%d" height="19" rx="4"/><text x="%d" y="%d">%s</text></g>' % (x2, rp[i], bw, x2 + 8, rp[i] + 14, html.escape(trunc(lab, 38))))
    o.append("</svg>")
    return "".join(o)


def mermaid_edges(edges, label_of, title_edges):
    lines = ["flowchart LR"]
    used = set()
    for a, b, lab in edges:
        used.update([a, b])
        lines.append('  %s -->|"%s"| %s' % (mid(a), ml(lab), mid(b)))
    for u in sorted(used, key=str):
        lines.insert(1, '  %s["%s"]' % (mid(u), ml(trunc(label_of(u), 34))))
    return "```mermaid\n" + "\n".join(lines) + "\n```" if edges else "No edges found. [JSON: %s]" % title_edges


# ============================== SECTIONS ==============================
def build_sections(ir, T, A):
    tn = dict((x["i"], x) for x in T)
    MET = ir.get("Metrics") or {}
    AS = ir.get("AnalysisStatus") or {}
    S = []   # ("h", text) ("p", text) ("t", headers, rows) ("m", mermaid) ("svg", svg)

    # ---- API surface ----
    vis_known = sum(1 for x in T for r in x["methods"] if r["vis"])
    n_methods = sum(len(x["methods"]) for x in T)
    api, handlers = {}, Counter()
    for x in T:
        rows = []
        for r in x["methods"]:
            low = r["name"].lower()
            if r["handler"]:
                handlers[x["i"]] += 1
                continue
            if low in GENERATED or x["generated"]:
                continue
            ok = (r["vis"] in PUBLIC_VISIBILITIES) if vis_known else True
            if ok:
                rows.append(r)
        api[x["i"]] = rows
    n_api = sum(len(v) for v in api.values())
    tt = A["tt"]
    exact = sum(v["exact"] for v in tt.values())
    inferred = sum(v["inferred"] for v in tt.values())
    expo = [(k, sum(len(e) for e in v.values())) for k, v in A["exp"].items()]
    web_met = (MET.get("WebServices"), MET.get("WebOperations"))
    unresolved, internal = MET.get("UnresolvedEdges"), MET.get("InternalEdges")
    warn = (AS.get("Authoritative") is False) or bool(AS.get("CompilationErrors"))
    banner = ("Authoritative=%s; compilation errors=%s; internal edges=%s vs unresolved edges=%s. %s" % (
        AS.get("Authoritative"), AS.get("CompilationErrors"), internal, unresolved,
        "Callers are probably UNDER-counted and external use may be incomplete: 'no caller found' can mean 'not resolved'." if warn else "No analysis warning recorded.")
        ) if (AS or MET) else "AnalysisStatus/Metrics not present in the JSON: confidence unknown."

    ext_rows_all = sorted(A["use"].items(), key=lambda kv: (kv[0][0], -sum(len(e) for e in kv[1].values())))
    tp, deps = A["third_party"]
    S.append(("h", "1. Summary"))
    S.append(("t", ["Fact", "Value"], [
        ["Types / methods read", "%d / %d" % (len(T), n_methods)],
        ["Public API methods (excluding constructors, designer code, UI event handlers)", n_api if vis_known else "%d (visibility not in JSON: all non-handler methods counted)" % n_api],
        ["UI event handlers (entry points triggered by the UI)", sum(handlers.values())],
        ["Internal API calls between types: exact / inferred by name", "%d / %d" % (exact, inferred)],
        ["External API kinds consumed", len(ext_rows_all)],
        ["Exposed interface kinds (HTTP / other protocols)", len(expo)],
        ["Metrics.WebServices / WebOperations", "%s / %s" % web_met],
        ["Distinct URLs found in code", len(A["urls"])],
        ["Third-party library vendors referenced", len(set(tp) | set(deps))]]))
    S.append(("banner", banner, warn))

    # ---- exposed ----
    S.append(("h", "2. APIs exposed by the project (HTTP and other protocols)"))
    if expo:
        rows = []
        for (cat, label), v in sorted(A["exp"].items()):
            evs = sorted(set(e for s in v.values() for e in s))
            rows.append([label, ", ".join(tn[i]["name"] for i in sorted(v))[:120], len(evs), ", ".join(evs[:3])])
        S.append(("t", ["Mechanism", "Types", "Evidence count", "Sample evidence (JSON)"], rows))
    else:
        S.append(("p", "No exposed HTTP or other protocol interface was found. Checked: ASMX, WCF, Web API/MVC, HTTP handlers, HttpListener, "
                       "TcpListener / named pipes, remoting / COM server, Windows service (attributes, base classes, code, call targets). Metrics.WebServices=%s, "
                       "Metrics.WebOperations=%s. Assumption: this is a desktop (WinForms) application that only consumes services." % web_met))

    # ---- internal graph ----
    S.append(("h", "3. Internal API graph (who uses whose public methods)"))
    pairs = sorted(tt.items(), key=lambda kv: -(kv[1]["exact"] + kv[1]["inferred"]))
    if pairs:
        callee_w = Counter()
        for (a, b), v in tt.items():
            callee_w[b] += v["exact"] + v["inferred"]
        right_ids = [i for i, _ in callee_w.most_common(GRAPH_MAX_NODES)]
        e_use = [(a, b, v) for (a, b), v in pairs if b in right_ids]
        caller_w = Counter()
        for a, b, v in e_use:
            caller_w[a] += v["exact"] + v["inferred"]
        left_ids = [i for i, _ in caller_w.most_common(GRAPH_MAX_NODES)]
        edges = [(a, b, v["exact"] + v["inferred"], "%s -> %s: %s (exact %d, inferred %d)" % (
            tn[a]["name"], tn[b]["name"], ", ".join(m for m, _ in v["methods"].most_common(6)), v["exact"], v["inferred"])) for a, b, v in e_use if a in left_ids]
        S.append(("p", "Left = caller type, right = type whose API is used. Hover an arrow for the methods. Inferred links are name matches on unresolved calls (Assumption)."))
        S.append(("svg", svg_graph([(i, tn[i]["name"]) for i in left_ids], [(i, tn[i]["name"]) for i in right_ids], edges, "Caller type", "Type whose API is called")))
        medges = [(a, b, "%s%s" % (", ".join(m for m, _ in v["methods"].most_common(2)), "" if not v["inferred"] else " *")) for (a, b), v in pairs[:MD_MAX_EDGES]]
        S.append(("m", mermaid_edges(medges, lambda i: tn[i]["name"], "CallGraph.Edges") + "\n\n`*` = at least one link inferred by name."))
        S.append(("t", ["Caller", "Callee", "Exact", "Inferred", "Methods called"],
                  [[tn[a]["name"], tn[b]["name"], v["exact"], v["inferred"], ", ".join("%s x%d" % kv for kv in v["methods"].most_common(6))] for (a, b), v in pairs]))
    else:
        S.append(("p", "No calls between known types were found in CallGraph.Edges (internal edges in Metrics: %s)." % internal))

    # ---- API surface table ----
    S.append(("h", "4. Public API surface per type"))
    S.append(("p", "Calls are those the IR could resolve. 'none found' = no caller in the resolved call graph. UI handlers are not counted as API."))
    rows, detail = [], []
    for x in T:
        if not api[x["i"]] and not handlers[x["i"]]:
            continue
        users = Counter()
        for r in api[x["i"]]:
            for ct in A["callers_of"].get(r["id"], {}):
                users[ct] += 1
        rows.append([x["name"], len(api[x["i"]]), handlers[x["i"]], ", ".join(tn[c]["name"] for c, _ in users.most_common(8)) or "none found", "Types[%d]" % x["i"]])
        for r in api[x["i"]]:
            c = A["callers_of"].get(r["id"], {})
            who = ", ".join("%s%s" % (tn[ct]["name"], "" if ex else " (inferred)") for ct, (ex, inf) in sorted(c.items()) for ex in [ex > 0])
            detail.append([x["name"], trunc(r["sig"], 90), r["vis"] or "?", who or "none found", A["callees_of"].get(r["id"], 0), "Types[%d].Methods[%d]" % (x["i"], r["j"])])
    S.append(("t", ["Type", "Public API methods", "UI handlers", "Used by (types)", "JSON"], rows))
    S.append(("h3", "Public methods"))
    S.append(("t", ["Type", "Method", "Visibility", "Called by", "Internal calls out", "JSON"], detail))

    # ---- external ----
    S.append(("h", "5. External APIs consumed"))
    if ext_rows_all:
        rows = []
        for (cat, label), v in ext_rows_all:
            evs = sorted(set(e for s in v.values() for e in s))
            rows.append([cat, label, len(v), len(evs), ", ".join(tn[i]["name"] for i in sorted(v))[:110], ", ".join(evs[:3])])
        S.append(("t", ["Category", "API", "Types", "Evidence (methods/fields)", "Used by", "Sample evidence (JSON)"], rows))
        weights = defaultdict(Counter)
        for (cat, label), v in A["use"].items():
            for ti, evs in v.items():
                weights[ti][cat + ": " + label] += len(evs)
        nodes_r = Counter()
        for ti, c in weights.items():
            for k, w in c.items():
                nodes_r[k] += w
        right = [k for k, _ in nodes_r.most_common(GRAPH_MAX_NODES)]
        left_ids = [i for i, _ in sorted(((i, sum(c.values())) for i, c in weights.items()), key=lambda z: -z[1])[:GRAPH_MAX_NODES]]
        edges = [(i, k, w, "%s -> %s: %d method(s)/field(s)" % (tn[i]["name"], k, w)) for i in left_ids for k, w in weights[i].items() if k in right]
        S.append(("svg", svg_graph([(i, tn[i]["name"]) for i in left_ids], [(k, k) for k in right], edges, "Type", "External API")))
        medges = sorted(edges, key=lambda z: -z[2])[:MD_MAX_EDGES]
        S.append(("m", mermaid_edges([(a, b, str(w)) for a, b, w, _ in medges], lambda i: tn[i]["name"] if isinstance(i, int) else i, "Types[].* code, CallGraph.Edges")))
    else:
        S.append(("p", "No external API usage was detected by the patterns in this script. Unresolved call roots (top 15): %s" % (", ".join("%s x%d" % kv for kv in A["unres_roots"].most_common(15)) or "none")))

    # ---- stored procs ----
    S.append(("h", "6. Stored procedures and SQL keys recorded in the IR"))
    sp = []
    find_keys(ir.get("Types") or [], "Types", lambda k: "storedproc" in k or k in ("sql", "sqlcalls", "sqlstatements", "commandtext"), sp)
    S.append(("p", "Metrics.StoredProcedureCalls=%s. 'Stored procedure call' pattern hits are in section 5." % MET.get("StoredProcedureCalls")))
    S.append(("t", ["JSON path", "Value"], [[p, v] for p, v in sp]) if sp else ("p", "No stored-procedure/SQL keys were found inside Types[] in the JSON structure."))

    # ---- urls ----
    S.append(("h", "7. Endpoints (URLs) found in code"))
    if A["urls"]:
        S.append(("t", ["URL (query string removed)", "Types", "Sample evidence (JSON)"],
                  [[u, ", ".join(sorted(set(tn[t]["name"] for t, _ in v))), ", ".join(sorted(e for _, e in v)[:3])] for u, v in sorted(A["urls"].items())]))
    else:
        S.append(("p", "No http(s) URLs were found in code strings."))

    # ---- third party ----
    S.append(("h", "8. Third-party libraries used"))
    allv = sorted(set(tp) | set(deps))
    rows = []
    for v in allv:
        vers = sorted(set(version_from_dep(d) for d in deps.get(v, []) if version_from_dep(d)))
        rows.append([v, len(deps.get(v, [])), ", ".join(vers) or "-", sum(tp[v].values()) if v in tp else 0,
                     ", ".join(tn[i]["name"] for i, _ in tp[v].most_common(8)) if v in tp else "-"])
    S.append(("t", ["Library / vendor", "Assemblies", "Version(s)", "Unresolved type refs", "Main users"], rows) if rows else ("p", "none"))
    S.append(("p", "Source: ExternalDependencies, and TypeDependencyGraph edges to unresolved types (System.* and WinForms excluded). [JSON: ExternalDependencies, TypeDependencyGraph.Edges]"))

    # ---- open questions ----
    S.append(("h", "9. Assumptions and open questions"))
    oq = ["Patterns are name-based (regex): custom wrappers around these APIs would not be detected. Ask: a developer of the project.",
          "Inferred calls are matched by type/method name only; overloads and same-named types are not distinguished (Assumption).",
          "Whether the application is also called by other systems cannot be seen in the IR. Ask: the application owner."]
    if not vis_known:
        oq.append("Types[].Methods[] carry no Visibility/Modifiers in this JSON, so every non-handler method was counted as API. Provide per-method IR via IR_BASE_DIR or check the schema.")
    if not n_methods:
        oq.append("No Methods were found under Types[]. Set IR_BASE_DIR to the folder with the per-type IR files (IrFile).")
    if not any(len(v) for v in A["use"].values()):
        oq.append("No code strings were found (RawCode/NormalizedCode), so external APIs could only be taken from call targets.")
    if warn:
        oq.append("The IR analysis is not authoritative (see the confidence note). Re-run it with the .NET reference assemblies and the project DLLs to get complete call and API data. Ask: the owner of the analysis tool.")
    S.append(("list", oq))
    return S, banner, warn


# ============================== RENDERING =============================
def to_md(S, meta):
    out = ["# API Graph - %s" % meta["project"],
           "_Generated %s from the IR JSON (schema %s, IR generated %s)._" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), meta["schema"], meta["generated"] or "time not recorded"),
           "_The HTML version (`%s`) has the clickable graphs and a search box._" % OUTPUT_HTML]
    for s in S:
        k = s[0]
        if k == "h":
            out.append("## " + s[1])
        elif k == "h3":
            out.append("### " + s[1])
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
:root{--bg:#fff;--panel:#f6f7f9;--text:#1c2330;--sub:#5b6578;--line:#d5d9e2;--accent:#2563eb;--warn-bg:#fff4e0;--warn-tx:#7a4a00;--ok-bg:#eaf3ff;--ok-tx:#1d4570}
@media (prefers-color-scheme:dark){:root{--bg:#16181d;--panel:#1e2128;--text:#e6e8ee;--sub:#9aa3b5;--line:#343a46;--accent:#60a5fa;--warn-bg:#3a2e14;--warn-tx:#f2cf8a;--ok-bg:#1c2a3d;--ok-tx:#a9c8ee}}
body{margin:0;font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--text)}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 18px;z-index:3}
h1{font-size:18px;margin:0}.sub{color:var(--sub);font-size:12.5px}
main{max-width:1180px;margin:0 auto;padding:6px 18px 40px}h2{font-size:16px;margin:26px 0 6px;border-bottom:1px solid var(--line);padding-bottom:4px}h3{font-size:14px}
input{border:1px solid var(--line);background:var(--bg);color:var(--text);border-radius:6px;padding:5px 9px;font:inherit;min-width:240px}
table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}th,td{border-bottom:1px solid var(--line);padding:4px 7px;text-align:left;vertical-align:top}th{color:var(--sub);position:sticky;top:58px;background:var(--bg)}
.banner{padding:8px 11px;border-radius:6px;background:var(--ok-bg);color:var(--ok-tx);margin:10px 0}.banner.warn{background:var(--warn-bg);color:var(--warn-tx)}
svg.g{width:100%;height:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px;margin:8px 0}
svg.g .n{fill:var(--bg);stroke:var(--line)}svg.g text{fill:var(--text);font-size:12px}svg.g text.h{fill:var(--sub);font-weight:700}
svg.g .e{fill:none;stroke:var(--accent);opacity:.35}svg.g .e:hover{opacity:1}
.src{color:var(--sub);font:12px ui-monospace,Consolas,monospace}.hide{display:none}
</style></head><body><header><h1>__TITLE__</h1><div class="sub">__SUB__</div>
<div style="margin-top:6px"><input id="q" type="search" placeholder="Filter all tables..." aria-label="Filter"></div></header><main>__BODY__</main>
<script>document.getElementById('q').oninput=function(){var q=this.value.toLowerCase();document.querySelectorAll('table tr').forEach(function(r,i){if(r.querySelector('th'))return;r.classList.toggle('hide',q&&r.textContent.toLowerCase().indexOf(q)<0);});};</script></body></html>"""


def to_html(S, meta):
    e = html.escape
    b = []
    for s in S:
        k = s[0]
        if k == "h":
            b.append("<h2>%s</h2>" % e(s[1]))
        elif k == "h3":
            b.append("<h3>%s</h3>" % e(s[1]))
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
            b.append(s[1])
        elif k == "list":
            b.append("<ul>%s</ul>" % "".join("<li>%s</li>" % e(q) for q in s[1]))
    title = "API Graph - %s" % meta["project"]
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
    log("extracting API facts for %d types" % len(T))
    A = build_api(ir, T)
    A["third_party"] = third_party(ir, T)
    S, banner, warn = build_sections(ir, T, A)
    meta = {"project": ir.get("ProjectName") or "VB.NET project", "schema": ir.get("SchemaVersion"), "generated": ir.get("GeneratedAtUtc")}
    md_path, html_path = os.path.join(OUTPUT_DIR, OUTPUT_MD), os.path.join(OUTPUT_DIR, OUTPUT_HTML)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(to_md(S, meta))
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(to_html(S, meta))
    log("exact/inferred internal calls=%d/%d, external API kinds=%d, exposed kinds=%d, urls=%d" % (
        sum(v["exact"] for v in A["tt"].values()), sum(v["inferred"] for v in A["tt"].values()), len(A["use"]), len(A["exp"]), len(A["urls"])))
    log("DONE -> %s" % md_path)
    log("DONE -> %s" % html_path)


if __name__ == "__main__":
    main()
