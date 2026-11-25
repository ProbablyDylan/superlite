"""Review packet: one size-capped map the final reviewer starts from before it reads the changed code.

    superlite-cli plan packet --plan PLAN.md [--root .] [--log F] [--probes DIR] [--out F]
                              [--max-tokens 20000] [--tests F] [--base REF]

Reads probe failures from the run folder's probes/probe-results.json and the test summary from
tests/summary.json (written by run_tests.py) by default. Incidents are read straight from the
JSONL format, never through incident.py. Review Focus section lines: `- <package>: deep, numeric`.
Rows a later event resolved are listed briefly, not counted; possibly stale ones stay marked (review_triage).
Repeats of one message collapse into one item with a count, across locations too.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import base  # noqa: E402
import incident  # noqa: E402
import planfile  # noqa: E402
import review_triage as triage  # noqa: E402
import sandbox  # noqa: E402
import state  # noqa: E402
from review_contract import check_interfaces, rel_to, review_focus  # noqa: E402

CATEGORIES = ("deep", "numeric", "encoding", "protocol", "file", "state")
CHARS_PER_TOKEN = 1.9  # 20k tokens ~ 38k chars
PASS_STATUSES = {"pass", "passed", "ok"}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "tests",
             "test", "build", "dist", ".pytest_cache"}
EXCERPT_FILE_LINES = 120
EXCERPT_TOTAL_LINES = 600


def _incident(ln: str) -> dict | None:
    """One log line as an incident entry, or None when it is not one."""
    try:
        e = json.loads(ln)
    except ValueError:
        return None
    if not isinstance(e, dict) or "file" not in e:
        return None
    e["file"] = str(e.get("file", "")).replace("\\", "/")
    try:
        e["line"] = int(e.get("line") or 0)
    except (TypeError, ValueError):
        e["line"] = 0
    return e


def read_incidents(path: Path) -> list[dict]:  # O(file size) total, single pass
    if not path.is_file():
        return []
    lines = (ln.strip() for ln in incident.read_text(path).splitlines())  # under the writers' shared lock
    return [e for e in map(_incident, (ln for ln in lines if ln)) if e is not None]


def excerpt(root: Path, file: str, line: int, ctx: int = 12) -> str:  # O(B) per call (re-reads whole file every call)
    f = root / file
    if not f.is_file():
        f = root / "src" / file
    if not f.is_file():
        return "(file not found)"
    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    if line <= 0:
        lo, hi = 1, min(len(lines), 2 * ctx + 1)
    else:
        lo, hi = max(1, line - ctx), min(len(lines), line + ctx)
    rows = []
    for n in range(lo, hi + 1):
        mark = ">" if n == line else " "
        rows.append(f"{mark}{n:>4} | {lines[n - 1][:160]}")
    return "\n".join(rows) if rows else "(empty file)"


@dataclass
class Item:
    section: str  # incidents | probes | uncovered | contract
    rank: tuple
    title: str
    body: str
    file: str = ""
    line: int = 0
    more: tuple = ()  # the other files of a merged incident, for the hot-spot excerpts


def _row_lines(rows: list[dict], limit: int = 3) -> list[str]:  # O(rows)
    """One line per distinct (tags, message), with a count and who it repeats for."""
    seen: dict[tuple[str, str], list[str]] = {}
    for e in rows:
        tags = "/".join(str(e[k]) for k in ("kind", "resolution", "confidence", "agent") if e.get(k))
        msg, who = triage.message(e)
        seen.setdefault((tags, msg), []).append(who)
    out = []
    for (tags, msg), whos in list(seen.items())[:limit]:
        names = list(dict.fromkeys(w for w in whos if w))
        who = ", ".join(names[:4]) + (", ..." if len(names) > 4 else "")
        count = f"x{len(whos)}" if len(whos) > 1 else ""
        tail = ": ".join(t for t in (count, who) if t)
        out.append(f"- [{tags}] {msg[:200]}" + (f" ({tail})" if tail else ""))
    if len(seen) > limit:
        out.append(f"- (+{len(seen) - limit} more distinct entries)")
    return out


def _cluster_item(locs: list[tuple], stale: str) -> Item:
    """One item for every location that carries the same open message(s)."""
    locs.sort(key=lambda x: (x[0], x[1]))
    file, line = locs[0][0], locs[0][1]
    rows = [e for loc in locs for e in loc[2]]
    live = [e for loc in locs for e in loc[3]]
    low = any(str(e.get("confidence", "")).lower() == "low" for e in rows)
    repeated = len(rows) >= 2
    why = [w for w, on in (("repeated", repeated), ("unresolved", bool(live)), ("low confidence", low)) if on]
    why += [f"possibly stale: {stale}"] if stale else []
    tier = 0 if repeated else 1 if live else 2
    body = _row_lines(live or rows)
    if len(locs) > 1:
        more = ", ".join(f"{f}:{ln}" for f, ln, *_ in locs[1:9])
        body.append(f"- same message also at: {more}" + (f" (+{len(locs) - 9} more)" if len(locs) > 9 else ""))
    if stale:
        body.append("- Possibly stale: check it against the code before you report it.")
    where = f"{file}:{line}" + (f" (+{len(locs) - 1} more location(s))" if len(locs) > 1 else "")
    return Item("incidents", (5 if stale else 1, tier, -len(rows), file, line),
                f"{where}  ({', '.join(why)}; {len(rows)} entr{'y' if len(rows) == 1 else 'ies'})",
                "\n".join(body), file, line, tuple(dict.fromkeys(f for f, *_ in locs[1:] if f != file)))


def incident_items(entries: list[dict], notes: list[str] | None = None) -> list[Item]:  # O(K) total
    """Open incidents grouped by file:line, then locations with the same open message(s) merged into one item.
    `notes` is review_triage.classify(entries): rows it resolved count as resolved here."""
    notes = notes if notes is not None else triage.classify(entries)
    groups: dict[tuple[str, int], list[tuple[dict, str]]] = {}
    for e, n in zip(entries, notes):
        if e.get("kind") != "verified":
            groups.setdefault((e["file"], e["line"]), []).append((e, n))
    clusters: dict[tuple, list[tuple]] = {}
    for (file, line), pairs in groups.items():
        live = [(e, n) for e, n in pairs if triage.is_unresolved(e) and not n.startswith("resolved")]
        low = any(str(e.get("confidence", "")).lower() == "low" for e, _ in pairs)
        if not live and not low:
            continue
        stale_notes = {n[len("stale: "):] for _, n in live if n.startswith("stale")}
        first = sorted(stale_notes)[:1] + ([f"+{len(stale_notes) - 1} more"] if len(stale_notes) > 1 else [])
        stale = "; ".join(first) if live and all(n.startswith("stale") for _, n in live) else ""
        sig = tuple(sorted({(triage.message(e)[0], file if e.get("agent") == "run_tests" else "")
                            for e, _ in (live or pairs)}))  # a generic test message never merges across files
        clusters.setdefault((sig, stale), []).append((file, line, [e for e, _ in pairs], [e for e, _ in live]))
    return [_cluster_item(locs, stale) for (_sig, stale), locs in clusters.items()]


def resolved_lines(entries: list[dict], notes: list[str], limit: int = 12) -> list[str]:  # O(K)
    """Brief lines for the open rows a later event resolved, collapsed by message and reason."""
    seen: dict[tuple[str, str], list[str]] = {}
    for e, n in zip(entries, notes):
        if n.startswith("resolved"):
            seen.setdefault((triage.message(e)[0], n[len("resolved: "):]), []).append(f"{e['file']}:{e['line']}")
    out = []
    for (msg, why), locs in list(seen.items())[:limit]:
        where = locs[0] + (f" (+{len(locs) - 1} more)" if len(locs) > 1 else "")
        out.append(f"- {where}  {msg[:120]} - resolved: {why}")
    if len(seen) > limit:
        out.append(f"- (+{len(seen) - limit} more resolved messages)")
    return out


def read_probe_failures(probes: Path) -> list[Item]:  # O(R) total
    f = probes if probes.is_file() else probes / "probe-results.json"
    if not f.is_file():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except ValueError:
        return []
    groups: dict[tuple, int] = {}  # identical failures collapse into one item with a count
    if isinstance(data, dict) and isinstance(data.get("failures"), list):  # probe_run: one group per root cause
        for g in data["failures"]:  # a group probe_run logged as an incident is listed (and resolved) there, not twice
            if isinstance(g, dict) and not g.get("incident"):
                key = (str(g.get("category", "?")), str(g.get("kind") or g.get("exc") or "fail"),
                       str(g.get("file") or "").replace("\\", "/"), int(g.get("line") or 0),
                       str(g.get("detail") or g.get("probe") or "")[:200])
                groups[key] = groups.get(key, 0) + max(1, int(g.get("count") or 1))
        return _probe_items(groups)
    results = data.get("results", []) if isinstance(data, dict) else data  # older flat list of probe rows
    for r in results if isinstance(results, list) else []:
        if not isinstance(r, dict) or str(r.get("status", "pass")).lower() in PASS_STATUSES:
            continue
        key = (str(r.get("category", "?")), str(r.get("status")), str(r.get("file", "")).replace("\\", "/"),
               int(r.get("line") or 0), str(r.get("detail") or r.get("message") or r.get("probe") or "")[:200])
        groups[key] = groups.get(key, 0) + 1
    return _probe_items(groups)


def _probe_items(groups: dict) -> list[Item]:  # O(groups)
    items: list[Item] = []
    for (cat, status, file, line, detail), n in groups.items():
        items.append(Item("probes", (3, cat, file, line),
                          f"probe {cat} {status}" + (f": {file}:{line}" if file else " (no project frame)") + (f"  (x{n})" if n > 1 else ""),
                          f"- {detail}" if detail else "", file, line))
    return items


def focus_packages(plan: planfile.Plan) -> list[tuple[str, list[str]]]:  # O(focus lines * m) total
    out: list[tuple[str, list[str]]] = []
    for ln in review_focus(plan).splitlines():
        m = re.match(r"^\s*(?:[-*]\s*)?\**`?([\w./-]+)`?\**\s*[:—-]\s*(.+)$", ln)
        if not m:
            continue
        cats = [c for c in re.split(r"[,·/;\s]+", m.group(2).lower().replace("**", "")) if c in CATEGORIES]
        if cats:
            out.append((Path(m.group(1)).name, list(dict.fromkeys(cats))))
    return out


def package_dir(root: Path, pkg: str) -> Path | None:
    for cand in (root / "src" / pkg, root / pkg):
        if cand.is_dir():
            return cand
    return None


def _first_def_line(path: Path) -> int:  # O(B) read + parse total
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return 1
    return next((n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))), 1)


def _largest_module(root: Path, pkg: str) -> tuple[str, int]:  # O(F_pkg*stat + B) total
    """File and first def line of the biggest module in a package, for the excerpt of an unverified item."""
    d = package_dir(root, pkg)
    if not d:
        return "", 0
    cands = sorted((p for p in d.rglob("*.py") if p.name != "__init__.py"
                    and not any(part in SKIP_DIRS or part.startswith(".") for part in p.relative_to(d).parts)),
                   key=lambda p: -p.stat().st_size)
    if not cands:
        return "", 0
    return rel_to(root, cands[0]), _first_def_line(cands[0])


def uncovered_items(plan: planfile.Plan, root: Path, entries: list[dict]) -> list[Item]:  # O(pkgs*cats*(V + F_pkg*stat + B)) total
    verified = [e for e in entries if e.get("kind") == "verified"]
    items: list[Item] = []
    for pkg, cats in focus_packages(plan):
        files = [e for e in verified if pkg in re.split(r"[/.]", e["file"].replace(".py", ""))
                 or e["file"].replace(".py", "") == pkg]
        for cat in cats:
            hit = any(str(e.get("category", "")).lower() == cat
                      or re.search(rf"\b{cat}\b", str(e.get("context", "")).lower()) for e in files)
            if hit:
                continue
            file, line = _largest_module(root, pkg)
            items.append(Item("uncovered", (4, pkg, cat),
                              f"package {pkg}: no `verified` entry for category `{cat}`",
                              "- Unverified: nothing marked this category verified. Read the package and check it "
                              "yourself, or list it as a finding.", file, line))
    return items


def package_of(root: Path, f: Path) -> str | None:  # O(d) stat/resolve syscalls total
    try:
        rel = f.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    parts = list(rel.parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    elif parts and not (root / parts[0] / "__init__.py").is_file():
        return None
    return parts[0] if len(parts) > 1 else None


def source_files(root: Path) -> list[Path]:  # O(tree size after pruning) total
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        out.extend(Path(dirpath) / f for f in filenames if f.endswith(".py"))
    return out


def _import_aliases(tree: ast.AST) -> dict[str, str]:  # O(nodes) total
    alias: dict[str, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for al in n.names:
                alias[(al.asname or al.name).split(".")[0]] = al.name if al.asname else al.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            for al in n.names:
                alias[al.asname or al.name] = f"{n.module}.{al.name}"
    return alias


def _call_targets(tree: ast.AST, alias: dict[str, str]):  # O(nodes) total
    """Yield (line, dotted callee) for each call whose root name is an imported name."""
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        chain, cur = [], n.func
        while isinstance(cur, ast.Attribute):
            chain.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name) and cur.id in alias:
            yield n.lineno, ".".join([alias[cur.id]] + chain[::-1])


def cross_package_calls(root: Path) -> list[str]:  # O(S*B) total, reads+parses every source file
    files = source_files(root)
    pkgs = {p for p in (package_of(root, f) for f in files) if p}
    rows: set[tuple[str, int, str, str, str]] = set()
    for f in files:
        own = package_of(root, f)
        if not own:
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for lineno, full in _call_targets(tree, _import_aliases(tree)):
            target = full.split(".")[0]
            if target in pkgs and target != own:
                rows.add((rel_to(root, f), lineno, own, target, full))
    return [f"- {f}:{ln}  {a} -> {b}: {full}()" for f, ln, a, b, full in sorted(rows)]


def _base_ref(root: Path, ref: str | None) -> str:
    """The ref to diff against: `ref`, else the root commit, else HEAD."""
    if ref is not None:
        return ref
    roots = base.git("rev-list", "--max-parents=0", "HEAD", cwd=root, check=False).stdout.split()
    return roots[0] if roots else "HEAD"


def git_diff_stat(root: Path, ref: str | None) -> str:  # O(X) + up to 2 git subprocess spawns total
    if not (root / ".git").exists():
        return "(not a git repository)"
    try:
        r = base.git("diff", "--stat", _base_ref(root, ref), cwd=root, check=False)
    except base.GitError as e:
        print(f"packet: diff stat unavailable: {e}", file=sys.stderr)
        return "(diff stat unavailable)"
    if r.returncode != 0:
        return "(diff stat unavailable)"
    return r.stdout.strip() or "(no changes)"


def source_excerpts(root: Path, ref: str | None, files: list[str],  # O(X) + one git spawn per file shown
                    per_file: int = EXCERPT_FILE_LINES, total: int = EXCERPT_TOTAL_LINES) -> str:
    """Changed hunks (`git diff <ref> -- file`) of the hot-spot files, bounded per file and in total."""
    if not (root / ".git").exists():
        return "- (not a git repository)"
    ref = _base_ref(root, ref)
    out: list[str] = []
    left = total
    for n, f in enumerate(files):
        if left <= 0:
            out.append(f"- (excerpts stopped at the {total}-line total cap; {len(files) - n} more file(s) omitted)")
            break
        try:
            r = base.git("diff", "-U3", ref, "--", f, cwd=root, check=False)
        except base.GitError as e:
            print(f"packet: no diff for {f}: {e}", file=sys.stderr)
            continue
        lines = r.stdout.splitlines() if r.returncode == 0 else []
        if not lines:
            continue
        take = lines[:min(per_file, left)]
        left -= len(take)
        out.append(f"### {f}\n```diff\n" + "\n".join(take) + "\n```")
        if len(take) < len(lines):
            why = "per-file" if len(take) == per_file else "total"
            out.append(f"(truncated: {len(take)} of {len(lines)} diff lines shown, {why} cap)")
    return "\n".join(out) or "- none (no changed hunks in the hot-spot files)"


def _summary_in(sl: Path) -> Path:  # O(1) tests/summary.json, else an old archive's root test-summary.json
    new, old = sl / "tests" / "summary.json", sl / "test-summary.json"
    return old if not new.exists() and not (new.parent / "latest").exists() and old.exists() else new


def load_summary(path: Path) -> tuple[str, dict | None]:  # O(file size) total
    """(state, data): state is running, missing, unreadable or ok; data only when ok."""
    try:  # the pointer names the newest background run; its summary may not be written yet
        ptr = json.loads((path.parent / "latest" if path.name == "summary.json" else path.with_name("test-summary.latest")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ptr = None
    if isinstance(ptr, dict) and isinstance(ptr.get("summary"), str) and not Path(ptr["summary"]).exists():
        return "running", None
    if not path.is_file():
        return "missing", None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return "unreadable", None
    return ("ok", d) if isinstance(d, dict) else ("unreadable", None)


def read_test_summary(path: Path) -> tuple[str, int]:  # O(file size) total
    st, d = load_summary(path)
    if d is None:
        return {"running": "- tests: running (newest run not finished; results reach the fixers)",
                "missing": "- no test summary recorded"}.get(st, "- test summary unreadable"), 0
    failed = d.get("failures") or d.get("failed_tests") or []
    skipped = d.get("skipped_tests") or []
    nfail = int(d["failed"]) if isinstance(d.get("failed"), int) else len(failed)
    rows = [f"- passed {d.get('passed', '?')} · failed {nfail} · skipped {d.get('skipped', len(skipped))}"]
    if d.get("mode") == "affected" and isinstance(d.get("selected"), list):
        rows.append(f"- affected: {len(d['selected'])} test files selected")
    rows += [f"- FAILING: {t}" for t in failed[:30]]
    rows += [f"- SKIPPED: {t}" for t in skipped[:30]]
    return "\n".join(rows), nfail


def render_item(root: Path, it: Item, shown: set[tuple[str, int]], idx: str) -> str:  # O(B) per item (excerpt re-reads file)
    out = [f"### {idx} {it.title}"]
    if it.body:
        out.append(it.body)
    if it.file:
        key = (it.file, it.line)
        if key in shown:
            out.append("(excerpt shown with the item for the same location)")
        else:
            shown.add(key)
            out.append("```\n" + excerpt(root, it.file, it.line) + "\n```")
    return "\n".join(out) + "\n"


def cap_text(s: str, limit: int, note: str = "(truncated)") -> str:  # O(len(s)) total
    if len(s) <= limit:
        return s
    return s[: max(0, limit - len(note) - 1)].rstrip() + "\n" + note


def _collect(plan: planfile.Plan, root: Path, log: Path, probes: Path,  # O(S*B + I*B + I log I) + 1 python subprocess
             facts: triage.Facts | None = None):
    """All ranked items, the contract mismatches that produced some, how many signatures were checked, and the
    brief lines for incidents a later event resolved."""
    entries = read_incidents(log)
    notes = triage.classify(entries, facts)
    items = incident_items(entries, notes) + read_probe_failures(probes) + uncovered_items(plan, root, entries)
    mismatches, n_checked = check_interfaces(plan, root, None)
    for m in mismatches:
        items.append(Item("contract", (2, m.file, m.line, m.msg), f"{m.file}:{m.line}  part1 {m.task}",
                          f"- {m.msg}", m.file, m.line))
    items.sort(key=lambda i: i.rank)
    return items, mismatches, n_checked, resolved_lines(entries, notes)


def _head(plan: planfile.Plan, fast: bool, n_open: int, counts: dict, nfail: int, n_stale: int,
          changed: list[str], limit: int = 6000) -> list[str]:
    verdict = ("Nothing is open in the logs: no open incidents, probe failures, unverified categories, contract "
               "mismatches or failing tests. Read the changed files; if they hold up, reply `Verdict: APPROVED`."
               if fast else
               f"Open items: {n_open} (incidents {counts['incidents']}, {n_stale} of them possibly stale; probe "
               f"failures {counts['probes']}, unverified categories {counts['uncovered']}, contract mismatches "
               f"{counts['contract']}, failing tests {nfail}). Excerpts are +-12 lines.")
    shown = changed[:150]
    files = [f"- {f}" for f in shown] or ["- none found (no checkpoints and no diff against the base)"]
    files += [f"- (+{len(changed) - len(shown)} more)"] if len(changed) > len(shown) else []
    files = cap_text("\n".join(files), max(500, limit), f"- (list cut at the size cap; {len(changed)} files)")
    return [
        f"# Review packet: {'FAST PATH' if fast else 'REVIEW REQUIRED'}",
        f"plan: {plan.path.name}",
        "This packet is the starting map, not the boundary: read the changed files below and every file an item "
        "names, check each claim against the code, and hunt for what the logs do not show. Findings as "
        "`file:line - problem - fix - tier N`.",
        verdict,
        "",
        "## Changed files (read these)",
        files,
        "",
    ]


def _choose(root: Path, items: list[Item], budget: int):  # O(I*B) total
    """Render items in rank order until the budget is spent; later items are only counted."""
    chosen: list[tuple[Item, str]] = []
    used, shown, stopped = 0, set(), False
    omitted = {"incidents": 0, "probes": 0, "uncovered": 0, "contract": 0}
    for n, it in enumerate(items, 1):
        text = render_item(root, it, shown, f"[{n}]")
        stopped = stopped or used + len(text) > budget
        if stopped:
            omitted[it.section] += 1
            continue
        chosen.append((it, text))
        used += len(text)
    return chosen, omitted


def sandbox_section(root: Path, sl: Path) -> list[str]:  # O(sandbox files)
    """`## Sandbox files` with one line per file of the open prototype; empty when none is open."""
    try:
        rel = sandbox.open_sandbox(sl)
        names = sandbox.files(root, root / rel) if rel else []
    except OSError:
        return []
    if not rel:
        return []
    shown = names[:200]
    return ["## Sandbox files", f"Open prototype sandbox: {rel}", *[f"- {n}" for n in shown],
            *([f"- (+ {len(names) - len(shown)} more)"] if len(names) > len(shown) else []), ""]


def cmd_packet(a: argparse.Namespace) -> int:  # O(S*B + I*B + I log I) + git/python subprocesses total
    plan = planfile.read(a.plan)
    root = Path(a.root).resolve()
    sl = state.run_dir(root)
    log = Path(a.log) if a.log else sl / "incidents.jsonl"
    probes = Path(a.probes) if a.probes else sl / "probes"
    out = Path(a.out) if a.out else sl / "review-packet.md"
    tests = Path(a.tests) if a.tests else _summary_in(sl)
    cap = int(a.max_tokens * CHARS_PER_TOKEN)

    summary = load_summary(tests)[1]
    items, mismatches, n_checked, resolved = _collect(plan, root, log, probes, triage.load_facts(root, sl, summary))
    counts = {s: sum(1 for i in items if i.section == s) for s in ("incidents", "probes", "uncovered", "contract")}
    n_stale = sum(1 for i in items if i.section == "incidents" and i.rank[0] == 5)
    tests_text, nfail = read_test_summary(tests)
    n_open = sum(counts.values()) + (1 if nfail else 0)
    fast = n_open == 0

    seams = cross_package_calls(root)
    seam_text = cap_text("\n".join(seams) if seams else "- none", 5000,
                         f"- (+ more calls omitted; {len(seams)} total)")
    focus = cap_text(review_focus(plan) or "(plan has no Review Focus section)", 3000)
    diff = cap_text(git_diff_stat(root, a.base), 2000)
    hot: list[str] = []  # hot-spot files: those the ranked items point at, in rank order
    for f in (f for it in items for f in (it.file, *it.more) if f):
        rel = rel_to(root, root / f)
        if rel not in hot:
            hot.append(rel)
    excerpts = cap_text(source_excerpts(root, a.base, hot), max(2000, cap // 3))

    git_ok = (root / ".git").exists()
    head = _head(plan, fast, n_open, counts, nfail, n_stale,
                 triage.changed_files(root, sl, _base_ref(root, a.base) if git_ok else None), cap // 8)
    resolved_text = (["Resolved by a later event (not counted; spot-check any you doubt):", *resolved, ""]
                     if resolved else [])
    tail = sandbox_section(root, sl) + ["## 4. Test summary", tests_text, "",
            "## 5. Cross-package seams (calls from one package into another)", seam_text, "",
            "## 6. Diff stat", "```\n" + diff + "\n```", "",
            "## 7. Review Focus", focus, "",
            "## 8. Source excerpts (changed hunks of the hot-spot files)", excerpts, ""]
    sec_titles = {
        "incidents": "## 1. Open incidents (repeated file:line first, then unresolved, then low confidence, "
                     "possibly stale last)",
        "probes": "## 2. Probe results: failures still open and unverified categories",
        "contract": f"## 3. Contract check: real signatures vs the plan's Produces ({n_checked} checked, "
                    f"{len(mismatches)} mismatch(es))",
    }
    fixed_len = len("\n".join(head + tail + resolved_text)) + 400 + sum(len(t) + 40 for t in sec_titles.values())
    chosen, omitted = _choose(root, items, cap - fixed_len)
    parts = list(head)
    for sec in ("incidents", "probes", "contract"):
        parts.append(sec_titles[sec])
        sec_items = [t for it, t in chosen if it.section == sec or (sec == "probes" and it.section == "uncovered")]
        parts.extend(sec_items or ["- none\n"])
        om = omitted[sec] + (omitted["uncovered"] if sec == "probes" else 0)
        if om:
            parts.append(f"- ({om} lower-ranked item(s) omitted to stay within the size cap)\n")
        parts.extend(resolved_text if sec == "incidents" else [])
    parts.extend(tail)
    text = cap_text("\n".join(parts), cap, "(packet truncated at the size cap)")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {out} ({len(text)} chars, ~{int(len(text) / CHARS_PER_TOKEN)} tokens) - "
          f"{'FAST PATH' if fast else f'{n_open} open item(s)'}")
    return 0
