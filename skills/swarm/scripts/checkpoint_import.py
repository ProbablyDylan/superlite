"""Import check for checkpointed Python modules, run on the project's interpreter. Stdlib only.

check(files, dir_name, root) -> (errors, warnings), each [(file, line, msg)]. The driver runs under
_pyrun.project_python(root) ($SUPERLITE_PROJECT_PYTHON, the project's venv, `uv run python`), else this interpreter;
an interpreter that cannot start or crashes falls back to this one with a warning. A ModuleNotFoundError for a
module that is not part of the project (not under the root, src/, a sandbox or the file's folder) is a warning:
the project's dependencies may simply be missing here.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _pyrun  # noqa: E402

IMPORT_DRIVER = r"""
import importlib, importlib.util, json, os, sys, traceback
from pathlib import Path
sys.dont_write_bytecode = True
root = os.path.abspath(sys.argv[1])
dir_name = sys.argv[2]
files = sys.argv[3:]
sys.path.insert(0, root)
if os.path.isdir(os.path.join(root, "src")):
    sys.path.insert(1, os.path.join(root, "src"))
sandbox_prefix = Path(os.path.abspath(os.path.join(root, dir_name, "sandboxes"))).as_posix()
sandbox_roots = []
for rel in files:
    path = Path(os.path.abspath(os.path.join(root, rel)))
    if path.as_posix().startswith(sandbox_prefix + "/"):
        parts = os.path.relpath(path, sandbox_prefix).replace("\\", "/").split("/")
        if len(parts) >= 3:
            folder = os.path.join(sandbox_prefix, *parts[:2])
            source = os.path.join(folder, "src")
            source = source if os.path.isdir(source) else folder
            if source not in sandbox_roots:
                sandbox_roots.append(source)
sys.path[:0] = sandbox_roots
failures = []
for rel in files:
    path = Path(os.path.join(root, rel))
    module_root = root
    if path.as_posix().startswith(sandbox_prefix + "/"):
        parts_from_root = os.path.relpath(path, sandbox_prefix).replace("\\", "/").split("/")
        if len(parts_from_root) >= 3:
            folder = os.path.join(sandbox_prefix, *parts_from_root[:2])
            module_root = os.path.join(folder, "src")
            if not os.path.isdir(module_root):
                module_root = folder
    module_rel = os.path.relpath(path, module_root).replace("\\", "/")
    parts = module_rel[:-3].split("/")
    if parts[0] == "src" and os.path.isdir(os.path.join(root, "src")):
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if parts and parts[-1] == "__main__":
        continue  # importing it runs the program; the syntax check covers it
    try:
        if parts and all(p.isidentifier() for p in parts):
            importlib.import_module(".".join(parts))
        else:
            name = "_slcheck_" + "_".join(p.replace("-", "_") for p in parts)
            sys.path.append(os.path.dirname(path))
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
    except BaseException as e:
        line = 0
        for fr in traceback.extract_tb(e.__traceback__):
            if os.path.abspath(fr.filename) == os.path.abspath(path):
                line = fr.lineno
        third = False
        if isinstance(e, ModuleNotFoundError) and e.name:
            top = e.name.split(".")[0]
            dirs = [root, os.path.join(root, "src"), os.path.dirname(os.path.abspath(path))] + sandbox_roots
            third = not any(os.path.exists(os.path.join(d, top + ".py")) or os.path.isdir(os.path.join(d, top)) for d in dirs)
        failures.append({"file": rel, "line": line, "error": f"{type(e).__name__}: {e}"[:150], "third_party": third})
print(json.dumps(failures))
"""


def _run(py, files, dir_name, root):  # O(m_py * import cost) + 1 process spawn
    """The driver's failure list, or (None, why) when the interpreter cannot start, times out or prints no result."""
    try:
        r = subprocess.run([*py, "-I", "-c", IMPORT_DRIVER, root, dir_name, *files], capture_output=True, text=True,
                           encoding="utf-8", timeout=120, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)[-150:]
    try:
        return json.loads(r.stdout.strip().splitlines()[-1]), ""
    except (IndexError, ValueError):
        return None, r.stderr.strip()[-150:]


def check(files, dir_name, root="."):  # O(m_py * import cost) + 1-2 process spawns
    if not files:
        return [], []
    py = _pyrun.project_python(root) or [sys.executable]
    out, why = _run(py, files, dir_name, root)
    notes = []
    if out is None and py != [sys.executable]:
        notes.append((files[0], 0, f"import check under {' '.join(py)} failed ({why}); checked with {sys.executable}"))
        py = [sys.executable]
        out, why = _run(py, files, dir_name, root)
    if out is None:
        return [(files[0], 0, f"import check crashed: {why}")], notes
    errors = [(f["file"], f["line"], f["error"]) for f in out if not f.get("third_party")]
    missing = [(f["file"], f["line"], f"{f['error']} (not part of the project; not installed for {' '.join(py)})")
               for f in out if f.get("third_party")]
    return errors, notes + missing
