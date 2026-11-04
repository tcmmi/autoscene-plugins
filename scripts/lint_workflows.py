#!/usr/bin/env python3
"""A lint of the catalog's workflows, for what GitHub would only find when it
runs them: the job graph, the outputs each `needs`, `steps` and `jobs`
reference reads, the inputs of a call, the grants a call gives, `${{ }}`
inside run scripts, and `bash -n` on every run script. It also holds the
rules this repo sets itself: every job that runs steps has a time limit,
nothing runs with always(), every action is pinned to a commit (`@<40 hex>
# vX.Y.Z`, as a tag can be moved), and a secret reaches only the env of one
step, as `${{ secrets.NAME }}` alone, in a job that runs in an environment
(whose rules decide whether the job gets it).

It needs PyYAML (python3-yaml); test_plan.py runs it. Usage:
`python3 -I scripts/lint_workflows.py [<repo>]`, which prints each problem.
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The outputs each plan.py command sets.
PLAN_OUTPUTS = {
    "changes": {"versions", "any", "index", "epoch"},
    "settings": {"uv", "asp_rev", "days"},
    "show": {"slug", "repo", "commit", "path", "kind", "uv", "matrix"},
    "source": {"scripts"},
    "epoch": {"certificate"},
}
EXPR = re.compile(r"\$\{\{(.*?)\}\}", re.S)
# Any use of the secrets context: secrets.X, secrets['X'], toJSON(secrets).
SECRETS = re.compile(r"\bsecrets\b")
ONE_SECRET = re.compile(r"\s*secrets\.[A-Za-z_][A-Za-z0-9_]*\s*")
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(.*?)\s*$")
PINNED = re.compile(r"[^\s@]+@[0-9a-f]{40} # v[0-9]+\.[0-9]+\.[0-9]+")
ANY = object()  # a step that names its outputs at run time


def walk(value, path=""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from walk(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from walk(v, f"{path}[{i}]")
    elif isinstance(value, str):
        yield path, value


def expressions(path, text):
    """The expressions in `text`; an `if:` is one even without ${{ }}."""
    found = EXPR.findall(text)
    if path.endswith(".if") and "${{" not in text:
        found.append(text)
    return found


def step_outputs(step):
    run = step.get("run", "")
    outs = set()
    m = re.search(r"plan\.py (?:--root \S+ )?(changes|settings|show|source|epoch)\b", run)
    if m:
        outs |= PLAN_OUTPUTS[m.group(1)]
    outs |= set(re.findall(r'echo "([\w-]+)=.*>> "\$GITHUB_OUTPUT"', run))
    if re.search(r'echo "\$\w+=.*>> "\$GITHUB_OUTPUT"', run):
        return ANY
    return outs


def triggers(doc):
    on = doc.get(True, doc.get("on"))
    return on if isinstance(on, dict) else {}


def lint(root=ROOT):
    """Every problem of the workflows in `root`, as lines."""
    import yaml  # here, so test_plan.py can say when it is missing

    problems = []
    for wf in sorted((Path(root) / ".github/workflows").glob("*.yml")):
        source = wf.read_text(encoding="utf-8")
        doc = yaml.safe_load(source)
        for number, line in enumerate(source.splitlines(), 1):
            m = USES.match(line)
            if m and not m.group(1).startswith("./") and not PINNED.fullmatch(m.group(1)):
                problems.append(f"{wf.name}:{number}: {m.group(1)} isn't pinned to a commit (@<40 hex> # vX.Y.Z)")
        for path, text in walk({k: v for k, v in doc.items() if k != "jobs"}, "workflow"):
            if any(SECRETS.search(expr) for expr in expressions(path, text)):
                problems.append(f"{wf.name} {path}: a secret outside a step's env")
        call = triggers(doc).get("workflow_call") or {}
        inputs = set(call.get("inputs") or {})
        jobs = doc["jobs"]
        for name, out in (call.get("outputs") or {}).items():
            for job, value in re.findall(r"jobs\.([\w-]+)\.outputs\.([\w-]+)", out.get("value", "")):
                if value not in (jobs.get(job, {}).get("outputs") or {}):
                    problems.append(f"{wf.name}: output {name} reads jobs.{job}.outputs.{value}, which isn't there")
        for jid, job in jobs.items():
            where = f"{wf.name}:{jid}"
            needs = job.get("needs", [])
            needs = [needs] if isinstance(needs, str) else needs
            for n in needs:
                if n not in jobs:
                    problems.append(f"{where}: needs unknown job {n}")
            if "steps" in job and "timeout-minutes" not in job:
                problems.append(f"{where}: no timeout-minutes")
            if "always()" in str(job.get("if", "")):
                problems.append(f"{where}: always() also runs a cancelled run; use !cancelled()")
            if "uses" in job and job["uses"].startswith("./"):
                problems += call_problems(root, where, job, doc)
            if "secrets" in job:
                problems.append(f"{where}: passes secrets to a called workflow")
            for path, text in walk(job, "job"):
                found = [expr for expr in expressions(path, text) if SECRETS.search(expr)]
                if found:
                    if not re.fullmatch(r"job\.steps\[\d+\]\.env\.[\w-]+", path):
                        problems.append(f"{where} {path}: a secret outside a step's env")
                    elif not EXPR.fullmatch(text.strip()) or not ONE_SECRET.fullmatch(found[0]):
                        problems.append(f"{where} {path}: a secret other than ${{{{ secrets.NAME }}}} alone")
                    elif "environment" not in job:
                        problems.append(f"{where} {path}: a secret in a job without an environment")
            seen = {}
            for i, step in enumerate(job.get("steps", [])):
                run = step.get("run")
                if run is not None:
                    if "${{" in run:
                        problems.append(f"{where} step {i}: ${{{{ }}}} inside run")
                    done = subprocess.run(["bash", "-n"], input=run.encode(), capture_output=True, check=False)
                    if done.returncode:
                        problems.append(f"{where} step {i}: bash -n: {done.stderr.decode().strip()}")
                for path, text in walk(step, f"steps[{i}]"):
                    for expr in expressions(path, text):
                        for sid, out in re.findall(r"steps\.([\w-]+)\.outputs\.([\w-]+)", expr):
                            if sid not in seen:
                                problems.append(f"{where} {path}: step {sid} isn't earlier in the job")
                            elif seen[sid] is not ANY and out not in seen[sid]:
                                problems.append(f"{where} {path}: step {sid} sets no output {out}")
                if "id" in step:
                    seen[step["id"]] = step_outputs(step)
            for path, text in walk(job.get("outputs") or {}, "outputs"):
                for expr in expressions(path, text):
                    for sid, out in re.findall(r"steps\.([\w-]+)\.outputs\.([\w-]+)", expr):
                        if sid not in seen or (seen[sid] is not ANY and out not in seen[sid]):
                            problems.append(f"{where} {path}: steps.{sid}.outputs.{out} isn't set")
            for path, text in walk(job, "job"):
                for expr in expressions(path, text):
                    for n, kind, name in re.findall(r"needs\.([\w-]+)\.(outputs|result)\.?([\w-]*)", expr):
                        if n not in needs:
                            problems.append(f"{where} {path}: needs.{n}, but the job doesn't need it")
                        elif kind == "outputs" and name and name not in job_outputs(root, jobs[n]):
                            problems.append(f"{where} {path}: {n} has no output {name}")
                    for name in re.findall(r"\binputs\.([\w-]+)", expr):
                        if name not in inputs:
                            problems.append(f"{where} {path}: unknown input {name}")
                    if "matrix." in expr and "strategy" not in job:
                        problems.append(f"{where} {path}: matrix without strategy")
    return problems


def job_outputs(root, job):
    if "uses" in job and job["uses"].startswith("./"):
        import yaml

        called = yaml.safe_load((Path(root) / job["uses"][2:]).read_text(encoding="utf-8"))
        return set((triggers(called).get("workflow_call") or {}).get("outputs") or {})
    return set(job.get("outputs") or {})


def call_problems(root, where, job, doc):
    """A call of a reusable workflow: its inputs, and the grants it gives
    against what each job it holds asks for."""
    import yaml

    problems = []
    called = Path(root) / job["uses"][2:]
    if not called.is_file():
        return [f"{where}: calls {job['uses']}, which isn't there"]
    cdoc = yaml.safe_load(called.read_text(encoding="utf-8"))
    cin = (triggers(cdoc).get("workflow_call") or {}).get("inputs") or {}
    given = job.get("with") or {}
    for k in given:
        if k not in cin:
            problems.append(f"{where}: passes unknown input {k}")
    for k, spec in cin.items():
        if spec.get("required") and k not in given:
            problems.append(f"{where}: misses required input {k}")
    granted = job.get("permissions", doc.get("permissions", {}))
    for cj, cjob in cdoc["jobs"].items():
        for perm, level in (cjob.get("permissions") or {}).items():
            if level == "write" and granted.get(perm) != "write":
                problems.append(f"{where}: {cj} asks for {perm}: write, but the call grants {granted.get(perm)}")
    return problems


if __name__ == "__main__":
    found = lint(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT)
    print("\n".join(found) or "No problems.")
    sys.exit(1 if found else 0)
