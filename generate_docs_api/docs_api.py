# utils/docs_api.py
"""
Single API that generates all documents for a migration.

POST /api/generate-docs
    body    : {"migration_id": "96"}
    input   : BASE_OUTPUT_DIR/<migration_id>/ir/project.ir.json
    output  : BASE_OUTPUT_DIR/<migration_id>/docs/
    returns : {"success": true, "migration_id": "96", "docs_folder_path": "...", "docs": {...}}

The three generator scripts (generate_arch_graph.py, generate_pud.py,
generate_tsd.py) live in ./scripts and are used UNCHANGED: for every request we
set their IR_JSON_PATH / OUTPUT_DIR globals and call their main().
LLM configuration (LLM_BASE_URL, LLM_MODEL, LLM_API_KEY ...) stays as it is
inside each script.
"""
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

# Run the 3 generators in parallel (True) or one after another (False)
RUN_PARALLEL = True

# Script functions
SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# task name (key in response) -> (script module, [attribute holding output file name, ...])
# the first file is the main document returned under the task name; extra files are
# returned under "<task>_<ext>" keys in "extra_files"
TASKS = {
    "architecture_graph": ("generate_arch_graph", ["OUTPUT_MD", "OUTPUT_HTML"]),
    "technical_document": ("generate_tsd", ["OUTPUT_FILENAME"]),
    "project_understanding_document": ("generate_pud", ["OUTPUT_FILENAME"]),
}

# the scripts keep state in module globals, so one run at a time per script
_LOCKS = {name: threading.Lock() for name, _ in TASKS.values()}

_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def get_ir_json_path(migration_id: str) -> str:
    migration_id = str(migration_id).strip()
    ir_path = BASE_OUTPUT_DIR / migration_id / "ir" / "project.ir.json"
    return str(ir_path)


def get_docs_dir(migration_id: str) -> str:
    return str(BASE_OUTPUT_DIR / str(migration_id).strip() / "docs")


def _run_script(module_name, file_attrs, ir_json_path, docs_dir):
    """Run one generator script unchanged and return the list of files it wrote."""
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
        files = [os.path.join(docs_dir, getattr(script, a)) for a in file_attrs]
    missing = [f for f in files if not os.path.isfile(f)]
    if missing:
        raise RuntimeError("%s finished but output missing: %s" % (module_name, missing))
    return files


def generate_all_docs(migration_id: str) -> dict:
    ir_json_path = get_ir_json_path(migration_id)
    if not os.path.isfile(ir_json_path):
        raise FileNotFoundError(f"IR JSON not found at: {ir_json_path}")

    docs_dir = get_docs_dir(migration_id)
    os.makedirs(docs_dir, exist_ok=True)

    docs, extra, errors = {}, {}, {}

    def work(task_name):
        module_name, file_attrs = TASKS[task_name]
        return task_name, _run_script(module_name, file_attrs, ir_json_path, docs_dir)

    def collect(task_name, files):
        docs[task_name] = files[0]
        for f in files[1:]:
            extra["%s_%s" % (task_name, os.path.splitext(f)[1].lstrip("."))] = f

    if RUN_PARALLEL:
        with ThreadPoolExecutor(max_workers=len(TASKS)) as pool:
            futures = {pool.submit(work, t): t for t in TASKS}
            for fut, t in futures.items():
                try:
                    collect(*fut.result())
                except Exception as e:
                    traceback.print_exc()
                    errors[t] = str(e)
    else:
        for t in TASKS:
            try:
                collect(*work(t))
            except Exception as e:
                traceback.print_exc()
                errors[t] = str(e)

    # keep response order same as TASKS
    docs = {t: docs[t] for t in TASKS if t in docs}
    result = {
        "success": not errors,
        "migration_id": str(migration_id).strip(),
        "docs_folder_path": docs_dir,
        "docs": docs,
    }
    if extra:
        result["extra_files"] = extra
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
