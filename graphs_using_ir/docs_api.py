# utils/docs_api.py
"""
Single API that generates all documents (6 now) for a migration as PDF.

POST /api/generate-docs
    body    : {"migration_id": "96"}
    input   : BASE_OUTPUT_DIR/<migration_id>/ir/project.ir.json
    output  : BASE_OUTPUT_DIR/<migration_id>/docs/   (markdowns AND pdfs are kept here)
    returns : {"success": true, "migration_id": "96", "docs_folder_path": "...", "pdfs": {...}}

The generator scripts (generate_arch_graph, generate_pud, generate_tsd, generate_api_graph,
generate_dependency_graph, generate_data_flow_graph) live in ./scripts and are used UNCHANGED: for every request we
set their IR_JSON_PATH / OUTPUT_DIR globals and call their main(). Each script
writes a markdown file; that markdown is then converted to a PDF
(headless Chromium, Mermaid diagrams are rendered as real diagrams).

Requirements for the PDF step (one time):
    pip install markdown-it-py playwright
    playwright install chromium          # (or set PDF_BROWSER_PATH / have Chrome or Edge installed)
    + put mermaid.min.js in utils/assets/  (or set MERMAID_JS_PATH) - needed only for diagrams
"""
import html as _html
import json
import os
import re
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from pathlib import Path

from flask import Blueprint, Flask, jsonify, request

# Base output directory (as per your path)
BASE_OUTPUT_DIR = Path("/mnt/MyDocuments/MY_DOCS/feature-himanshu/migration-service/analysis/convertor/output")

# Run the generators (+ their PDF conversion) in parallel (True) or one after another (False)
RUN_PARALLEL = True

# ----------------------------- PDF settings -----------------------------
# Mermaid library used to draw the diagrams inside the PDF. Local file is preferred
# (works offline); the CDN url is only a fallback.
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
MERMAID_JS_PATH = os.environ.get("MERMAID_JS_PATH", str(ASSETS_DIR / "mermaid.min.js"))
MERMAID_CDN_URL = os.environ.get("MERMAID_CDN_URL", "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js")
# Optional: full path of a Chrome / Chromium / Edge executable, if playwright's own chromium is not installed
PDF_BROWSER_PATH = os.environ.get("PDF_BROWSER_PATH", "")
PDF_PAGE_FORMAT = "A4"
MAX_PARALLEL_PDF = 3             # how many browsers may run at the same time while making PDFs
PDF_SECTION_PAGE_BREAK = False   # True = every "## " section starts on a new page

# Script functions
SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# task name (key in response) -> (script module, attribute of the script that holds the markdown file name)
TASKS = {
    "architecture_graph": ("generate_arch_graph", "OUTPUT_MD"),
    "technical_document": ("generate_tsd", "OUTPUT_FILENAME"),
    "project_understanding_document": ("generate_pud", "OUTPUT_FILENAME"),
    "api_graph": ("generate_api_graph", "OUTPUT_MD"),
    "dependency_graph": ("generate_dependency_graph", "OUTPUT_MD"),
    "data_flow_graph": ("generate_data_flow_graph", "OUTPUT_MD"),
}

# the scripts keep state in module globals, so one run at a time per script
_LOCKS = {name: threading.Lock() for name, _ in TASKS.values()}
_PDF_SLOTS = threading.Semaphore(MAX_PARALLEL_PDF)

_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def get_ir_json_path(migration_id: str) -> str:
    migration_id = str(migration_id).strip()
    ir_path = BASE_OUTPUT_DIR / migration_id / "ir" / "project.ir.json"
    return str(ir_path)


def get_docs_dir(migration_id: str) -> str:
    return str(BASE_OUTPUT_DIR / str(migration_id).strip() / "docs")


# ======================================================================
#                      MARKDOWN  ->  PDF
# ======================================================================
_CSS = r"""
@page { size: A4; margin: 18mm 16mm 20mm 16mm; }
@page land { size: A4 landscape; margin: 16mm 14mm 18mm 14mm; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font-family: "Segoe UI", "Helvetica Neue", Arial, "Liberation Sans", "DejaVu Sans", sans-serif;
       font-size: 10pt; line-height: 1.5; color: #1f2937; margin: 0; }
.title-block { background: linear-gradient(135deg, #0f2f57 0%, #1d4f91 100%); color: #fff; border-radius: 6px;
               padding: 22px 26px 18px 26px; margin-bottom: 14px; }
.title-block .eyebrow { font-size: 9pt; letter-spacing: .14em; text-transform: uppercase; color: #bcd3f2; margin-bottom: 4px; }
.title-block h1 { font-size: 24pt; line-height: 1.15; margin: 0 0 8px 0; font-weight: 700; color: #fff; border: 0; }
.title-block .meta p { margin: 2px 0; font-size: 8.5pt; color: #d6e4f7; }
.title-block .meta em { font-style: normal; }
.title-block .meta code { background: rgba(255,255,255,.15); color: #fff; }
.front-extra blockquote { margin: 0 0 12px 0; }
nav.toc { margin: 4px 0 6px 0; }
nav.toc .toc-title { font-size: 13pt; color: #0f2f57; margin: 0 0 6px 0; padding-bottom: 4px; border-bottom: 2px solid #1d4f91; }
nav.toc ul { list-style: none; margin: 0; padding: 0; }
nav.toc.two-col ul { column-count: 2; column-gap: 24px; }
nav.toc li { margin: 0; padding: 1px 0; break-inside: avoid; font-size: 9pt; }
nav.toc li.l2 { font-weight: 600; margin-top: 4px; }
nav.toc li.l3 { padding-left: 14px; font-size: 8.5pt; color: #4b5563; }
nav.toc a { color: inherit; text-decoration: none; }
.body-start { break-before: page; }
h2, h3, h4, h5 { break-after: avoid; page-break-after: avoid; }
h2 { font-size: 15pt; color: #0f2f57; margin: 22px 0 8px 0; padding-bottom: 4px; border-bottom: 2px solid #1d4f91; }
.sec-break h2 { break-before: page; margin-top: 0; }
h3 { font-size: 12pt; color: #1d4f91; margin: 16px 0 6px 0; }
h4 { font-size: 10.5pt; color: #1f2937; margin: 12px 0 4px 0; }
h5, h6 { font-size: 10pt; color: #374151; margin: 10px 0 4px 0; }
body > h1, .body h1 { font-size: 17pt; color: #0f2f57; margin: 22px 0 8px 0; }
p { margin: 0 0 8px 0; orphans: 3; widows: 3; }
ul, ol { margin: 0 0 8px 0; padding-left: 22px; }
li { margin: 2px 0; }
li > p { margin: 0 0 3px 0; }
a { color: #1d4f91; text-decoration: none; }
hr { border: 0; border-top: 1px solid #d1d5db; margin: 14px 0; }
strong { color: #111827; }
blockquote { margin: 8px 0 12px 0; padding: 8px 12px; background: #eff6ff; border-left: 4px solid #3b82f6;
             border-radius: 0 4px 4px 0; color: #1e3a5f; break-inside: avoid; }
blockquote p:last-child { margin-bottom: 0; }
code { font-family: Consolas, "DejaVu Sans Mono", "Liberation Mono", Menlo, monospace; font-size: 8.6pt;
       background: #f1f5f9; border: 1px solid #e2e8f0; border-radius: 3px; padding: 0 3px; overflow-wrap: anywhere; }
pre { background: #f8fafc; border: 1px solid #dbe3ee; border-left: 4px solid #94a3b8; border-radius: 4px;
      padding: 8px 10px; margin: 8px 0 12px 0; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 8.2pt; line-height: 1.4; }
pre code { background: none; border: 0; padding: 0; font-size: inherit; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 14px 0; font-size: 8.6pt; line-height: 1.35; }
thead { display: table-header-group; }
tr { break-inside: avoid; page-break-inside: avoid; }
th { background: #14365d; color: #fff; text-align: left; font-weight: 600; padding: 5px 7px; border: 1px solid #14365d; }
td { padding: 4px 7px; border: 1px solid #d5dde8; vertical-align: top; overflow-wrap: anywhere; word-break: break-word; }
tbody tr:nth-child(even) td { background: #f4f7fb; }
td code { font-size: 8pt; }
.src { color: #6b7280; font-size: 0.85em; }
.diagram-block { margin: 10px 0 16px 0; break-inside: avoid; page-break-inside: avoid; }
.landscape-group { page: land; }
.diagram { text-align: center; border: 1px solid #dbe3ee; border-radius: 6px; padding: 10px; background: #fff; }
.diagram svg { display: block; margin: 0 auto; max-width: none; }
.diagram-fail { text-align: left; }
.diagram-fail .note { font-size: 8.5pt; color: #92400e; background: #fffbeb; border: 1px solid #fcd34d; border-radius: 4px;
                      padding: 5px 8px; margin-bottom: 6px; }
.diagram-fail pre { margin: 0; font-size: 7.5pt; }
"""

_JS_RENDER = r"""
async ([diagrams, haveMermaid, pageDims]) => {
  const out = [];
  const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const fail = (host, code, msg) => {
    host.innerHTML = '<div class="diagram-fail"><div class="note">Diagram could not be drawn (' + esc(msg) +
                     '). Diagram source:</div><pre>' + esc(code) + '</pre></div>';
  };
  if (haveMermaid) {
    mermaid.initialize({ startOnLoad: false, securityLevel: 'loose', theme: 'default', maxTextSize: 2000000, maxEdges: 5000,
      fontFamily: '"Segoe UI","Helvetica Neue",Arial,"Liberation Sans","DejaVu Sans",sans-serif',
      flowchart: { useMaxWidth: false, htmlLabels: true, curve: 'basis' },
      sequence: { useMaxWidth: false }, er: { useMaxWidth: false } });
  }
  if (document.fonts && document.fonts.ready) { await document.fonts.ready; }
  for (let i = 0; i < diagrams.length; i++) {
    const block = document.querySelector('.diagram-block[data-idx="' + i + '"]');
    const host = block.querySelector('.diagram');
    if (!haveMermaid) { fail(host, diagrams[i], 'mermaid.min.js not available'); out.push({i, ok: false}); continue; }
    try {
      const res = await mermaid.render('mmd' + i, diagrams[i]);
      host.innerHTML = res.svg;
      const svg = host.querySelector('svg');
      let w = 0, h = 0;
      const vb = svg.viewBox && svg.viewBox.baseVal;
      if (vb && vb.width) { w = vb.width; h = vb.height; }
      else { const r = svg.getBoundingClientRect(); w = r.width; h = r.height; }
      // portrait vs landscape page, scale to fit
      const sp = Math.min(1, pageDims.pw / w, pageDims.ph / h);
      const sl = Math.min(1, pageDims.lw / w, pageDims.lh / h);
      let s = sp;
      if (sp < 0.75 && sl > sp * 1.15) {
        // landscape: move the heading(s) just above the diagram onto the same landscape page
        const wrap = document.createElement('div'); wrap.className = 'landscape-group';
        let first = block;
        while (first.previousElementSibling && /^H[2-6]$/.test(first.previousElementSibling.tagName)) { first = first.previousElementSibling; }
        first.parentNode.insertBefore(wrap, first);
        while (first !== block.nextSibling && first) { const nx = first.nextSibling; wrap.appendChild(first); first = nx; if (wrap.contains(block)) break; }
        block.classList.add('landscape'); s = sl;
      }
      svg.removeAttribute('width'); svg.removeAttribute('height');
      svg.setAttribute('style', 'width:' + (w * s).toFixed(1) + 'px;height:' + (h * s).toFixed(1) + 'px;max-width:none;');
      out.push({ i, ok: true, w, h, landscape: block.classList.contains('landscape') });
    } catch (e) {
      const stray = document.getElementById('dmmd' + i); if (stray) stray.remove();
      fail(host, diagrams[i], (e && e.message ? e.message : String(e)).split('\n')[0].slice(0, 160));
      out.push({ i, ok: false, error: String(e && e.message || e).slice(0, 300) });
    }
  }
  document.querySelectorAll('[id^="dmmd"]').forEach(n => n.remove());
  return out;
}
"""

_md_engine = None
_md_engine_lock = threading.Lock()


def _get_md_engine():
    """markdown-it parser with tables, heading ids and a mermaid-aware fence renderer."""
    global _md_engine
    with _md_engine_lock:
        if _md_engine is not None:
            return _md_engine
        try:
            from markdown_it import MarkdownIt
        except ImportError:
            raise RuntimeError("PDF conversion needs 'markdown-it-py' (pip install markdown-it-py)")
        md = MarkdownIt("commonmark", {"html": False, "breaks": True, "linkify": False}).enable(["table", "strikethrough"])

        def plain_text(inline_tok):
            parts = []
            for ch in (inline_tok.children or []):
                if ch.type in ("text", "code_inline"):
                    parts.append(ch.content)
                elif ch.type in ("softbreak", "hardbreak"):
                    parts.append(" ")
            return "".join(parts).strip()

        def heading_ids(state):
            env = state.env
            toc = env.setdefault("toc", [])
            for i, tok in enumerate(state.tokens):
                if tok.type == "heading_open":
                    env["hcount"] = env.get("hcount", 0) + 1
                    hid = "h-%d" % env["hcount"]
                    tok.attrSet("id", hid)
                    level = int(tok.tag[1])
                    if level in (2, 3) and i + 1 < len(state.tokens):
                        toc.append((level, hid, plain_text(state.tokens[i + 1])))

        md.core.ruler.push("heading_ids", heading_ids)

        orig_fence = md.renderer.fence

        def fence(tokens, idx, options, env):
            tok = tokens[idx]
            lang = tok.info.strip().split()[0].lower() if tok.info.strip() else ""
            if lang == "mermaid":
                diagrams = env.setdefault("diagrams", [])
                diagrams.append(tok.content)
                return '<div class="diagram-block" data-idx="%d"><div class="diagram"></div></div>\n' % (len(diagrams) - 1)
            return orig_fence(tokens, idx, options, env)

        md.add_render_rule("fence", lambda self, tokens, idx, options, env: fence(tokens, idx, options, env))
        _md_engine = md
        return md


_CODE_SPLIT = re.compile(r"(<pre\b.*?</pre>|<code\b.*?</code>)", re.S)


def _post_process_html(h: str) -> str:
    """text-only clean ups: <br> written by the LLM inside table cells, [JSON: ...] source tags."""
    segs = _CODE_SPLIT.split(h)
    for i in range(0, len(segs), 2):  # even segments = outside <pre>/<code>
        s = segs[i]
        s = re.sub(r"&lt;br\s*/?&gt;", "<br>", s, flags=re.I)
        # [JSON: ...] only in text (not inside tag attributes)
        s = re.sub(r"(>[^<]*)", lambda m: re.sub(r"\[JSON:[^\]]*\]", lambda x: '<span class="src">%s</span>' % x.group(0), m.group(1)), s)
        segs[i] = s
    return "".join(segs)


def _normalize_markdown(text: str) -> str:
    """make sloppy (LLM) markdown parse the way it was meant: blank line before tables."""
    out, in_fence, prev = [], False, ""
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and line.lstrip().startswith("|") and prev.strip() and not prev.lstrip().startswith("|"):
            out.append("")
        out.append(line)
        prev = line
    return "\n".join(out)


def _build_html(md_text: str, fallback_title: str):
    md = _get_md_engine()
    text = _normalize_markdown(md_text)
    m2 = re.search(r"(?m)^##\s", text)
    front, body = (text[:m2.start()], text[m2.start():]) if m2 else (text, "")
    # title
    title = fallback_title
    mt = re.search(r"(?m)^#\s+(.+?)\s*$", front)
    if mt:
        title = mt.group(1).strip()
        front = front[:mt.start()] + front[mt.end():]
    if " - " in title:
        eyebrow, project = title.split(" - ", 1)
    else:
        eyebrow, project = "Document", title

    env = {}
    # body first (collects toc entries); front paragraphs: first the italic meta lines, then callouts
    body_html = _post_process_html(md.render(body, env))
    meta_lines, extra_lines = [], []
    for line in front.strip().splitlines():
        (extra_lines if line.lstrip().startswith(">") else meta_lines).append(line)
    env_front = {"hcount": 1000}
    meta_html = _post_process_html(md.render("\n".join(meta_lines), env_front))
    extra_html = _post_process_html(md.render("\n".join(extra_lines), env_front))
    diagrams_all = env.get("diagrams", [])
    # front blocks have no diagrams normally; if they do, append (index continues after body diagrams)
    for extra_env in (env_front,):
        for d in extra_env.get("diagrams", []):
            diagrams_all.append(d)

    toc = env.get("toc", [])
    toc_html = ""
    if toc:
        items = "".join('<li class="l%d"><a href="#%s">%s</a></li>' % (lvl, hid, _html.escape(txt)) for lvl, hid, txt in toc)
        toc_html = '<nav class="toc%s"><h2 class="toc-title">Contents</h2><ul>%s</ul></nav>' % (
            " two-col" if len(toc) > 24 else "", items)
    break_cls = " sec-break" if PDF_SECTION_PAGE_BREAK else ""
    body_cls = "body-start" if len(toc) > 12 else ""
    doc = ('<!DOCTYPE html><html><head><meta charset="utf-8"><title>%s</title><style>%s</style></head><body>'
           '<div class="title-block"><div class="eyebrow">%s</div><h1>%s</h1><div class="meta">%s</div></div>'
           '<div class="front-extra">%s</div>%s<div class="body %s%s">%s</div></body></html>') % (
        _html.escape(title), _CSS, _html.escape(eyebrow), _html.escape(project), meta_html, extra_html, toc_html,
        body_cls, break_cls, body_html)
    return doc, diagrams_all, eyebrow, project


def _launch_browser(pw):
    """bundled chromium -> explicit executable -> installed Chrome -> installed Edge"""
    errors = []
    args = ["--no-sandbox", "--disable-dev-shm-usage"]
    if PDF_BROWSER_PATH:
        try:
            return pw.chromium.launch(executable_path=PDF_BROWSER_PATH, args=args)
        except Exception as e:
            errors.append("PDF_BROWSER_PATH: %s" % str(e).splitlines()[0])
    try:
        return pw.chromium.launch(args=args)
    except Exception as e:
        errors.append("chromium: %s" % str(e).splitlines()[0])
    for channel in ("chrome", "msedge"):
        try:
            return pw.chromium.launch(channel=channel, args=args)
        except Exception as e:
            errors.append("%s: %s" % (channel, str(e).splitlines()[0]))
    raise RuntimeError("No browser available for PDF creation (run 'playwright install chromium' or set "
                       "PDF_BROWSER_PATH). Details: " + " | ".join(errors))


def md_to_pdf(md_path: str, pdf_path: str = None) -> str:
    """Convert one markdown file to a formatted PDF (same folder, same name). Returns the pdf path."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("PDF conversion needs playwright (pip install playwright && playwright install chromium)")
    md_path = str(md_path)
    pdf_path = pdf_path or (os.path.splitext(md_path)[0] + ".pdf")
    with open(md_path, "r", encoding="utf-8-sig") as f:
        md_text = f.read()
    doc, diagrams, eyebrow, project = _build_html(md_text, os.path.splitext(os.path.basename(md_path))[0])

    footer = ('<div style="width:100%%;font-size:7.5px;color:#6b7280;padding:0 16mm;display:flex;'
              'justify-content:space-between;font-family:Arial,Helvetica,sans-serif;">'
              '<span>%s &mdash; %s</span><span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span></div>'
              ) % (_html.escape(eyebrow), _html.escape(project))
    header = '<div style="font-size:1px"></div>'

    have_mermaid_file = bool(MERMAID_JS_PATH) and os.path.isfile(MERMAID_JS_PATH)
    # usable content size in CSS px (96 dpi) for portrait / landscape pages, minus room for a heading above a diagram
    page_dims = {"pw": 660, "ph": 880, "lw": 980, "lh": 580}

    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        try:
            page = browser.new_page()
            page.set_content(doc, wait_until="load")
            have_mermaid = False
            if diagrams:
                if have_mermaid_file:
                    try:
                        page.add_script_tag(path=MERMAID_JS_PATH)
                        have_mermaid = True
                    except Exception as e:
                        print("[docs_api] could not load %s: %s" % (MERMAID_JS_PATH, e), flush=True)
                if not have_mermaid and MERMAID_CDN_URL:
                    try:
                        page.add_script_tag(url=MERMAID_CDN_URL)
                        have_mermaid = bool(page.evaluate("typeof mermaid !== 'undefined'"))
                    except Exception as e:
                        print("[docs_api] mermaid CDN not reachable: %s" % str(e).splitlines()[0], flush=True)
                if not have_mermaid:
                    print("[docs_api] WARNING: mermaid.min.js not available, diagrams will be shown as code. "
                          "Put it in %s or set MERMAID_JS_PATH" % MERMAID_JS_PATH, flush=True)
                res = page.evaluate(_JS_RENDER, [diagrams, have_mermaid, page_dims])
                bad = [r for r in res if not r.get("ok")]
                print("[docs_api] %s: %d diagram(s), %d not drawn%s" % (
                    os.path.basename(md_path), len(diagrams), len(bad),
                    ("  e.g. " + bad[0].get("error", "")) if bad and bad[0].get("error") else ""), flush=True)
            pdf_args = dict(path=pdf_path, format=PDF_PAGE_FORMAT, print_background=True, prefer_css_page_size=True,
                            display_header_footer=True, header_template=header, footer_template=footer)
            try:
                page.pdf(outline=True, tagged=True, **pdf_args)
            except TypeError:  # older playwright without outline/tagged
                page.pdf(**pdf_args)
        finally:
            browser.close()
    if not os.path.isfile(pdf_path):
        raise RuntimeError("PDF was not created for %s" % md_path)
    return pdf_path


# ======================================================================
#                      GENERATION PIPELINE
# ======================================================================
def _run_script(module_name, md_attr, ir_json_path, docs_dir):
    """Run one generator script unchanged and return the markdown file it wrote."""
    with _LOCKS[module_name]:
        script = import_module(module_name)
        # point the script at this migration
        script.IR_JSON_PATH = ir_json_path
        script.OUTPUT_DIR = docs_dir
        # clear state that the scripts accumulate between runs
        for attr in ("ASSUMPTIONS", "OPEN_QUESTIONS", "LLM_ERRORS"):
            if hasattr(script, attr):
                getattr(script, attr).clear()
        try:
            script.main()
        except SystemExit as e:  # the scripts call sys.exit("message") on config problems
            raise RuntimeError("%s exited: %s" % (module_name, e))
        md_path = os.path.join(docs_dir, getattr(script, md_attr))
    if not os.path.isfile(md_path):
        raise RuntimeError("%s finished but output missing: %s" % (module_name, md_path))
    return md_path


def generate_all_docs(migration_id: str) -> dict:
    ir_json_path = get_ir_json_path(migration_id)
    if not os.path.isfile(ir_json_path):
        raise FileNotFoundError(f"IR JSON not found at: {ir_json_path}")

    docs_dir = get_docs_dir(migration_id)
    os.makedirs(docs_dir, exist_ok=True)

    pdfs, errors = {}, {}

    def work(task_name):
        module_name, md_attr = TASKS[task_name]
        md_path = _run_script(module_name, md_attr, ir_json_path, docs_dir)   # markdown stays in docs_dir
        with _PDF_SLOTS:
            return task_name, md_to_pdf(md_path)                               # pdf is written next to it

    if RUN_PARALLEL:
        with ThreadPoolExecutor(max_workers=len(TASKS)) as pool:
            futures = {pool.submit(work, t): t for t in TASKS}
            for fut, t in futures.items():
                try:
                    name, pdf = fut.result()
                    pdfs[name] = pdf
                except Exception as e:
                    traceback.print_exc()
                    errors[t] = str(e)
    else:
        for t in TASKS:
            try:
                name, pdf = work(t)
                pdfs[name] = pdf
            except Exception as e:
                traceback.print_exc()
                errors[t] = str(e)

    # keep response order same as TASKS
    pdfs = {t: pdfs[t] for t in TASKS if t in pdfs}
    result = {
        "success": not errors,
        "migration_id": str(migration_id).strip(),
        "docs_folder_path": docs_dir,
        "pdfs": pdfs,
    }
    if errors:
        result["errors"] = errors
    return result


docs_bp = Blueprint("docs_api", __name__)


@docs_bp.route("/api/generate-docs", methods=["POST"])
def generate_docs_endpoint():
    payload = request.get_json(silent=True) or {}
    migration_id = str(payload.get("migration_id", "")).strip()
    if not migration_id:
        return jsonify({"success": False, "error": "migration_id is required"}), 400
    if not _ID_RE.match(migration_id) or migration_id in (".", ".."):
        return jsonify({"success": False, "error": "invalid migration_id"}), 400
    try:
        result = generate_all_docs(migration_id)
    except FileNotFoundError as e:
        return jsonify({"success": False, "migration_id": migration_id, "error": str(e)}), 404
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "migration_id": migration_id, "error": str(e)}), 500
    return jsonify(result), (200 if result["success"] else 500)


if __name__ == "__main__":
    app = Flask(__name__)
    app.register_blueprint(docs_bp)
    app.run(host="0.0.0.0", port=5000)
