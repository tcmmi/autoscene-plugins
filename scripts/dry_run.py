#!/usr/bin/env python3
"""A dry run of publish.yml or resign.yml on this computer, for test_plan.py.

Each job's `if`, env and steps run as GitHub runs them: the expressions are
evaluated, each run script goes through `bash --noprofile --norc -eo
pipefail`, outputs pass through GITHUB_OUTPUT, and a failed step skips the
rest of its job. What only GitHub has is played here:

- a bare repo is GitHub's copy of the catalog: checkout clones it, and git
  pushes to git@github.com:<repo>.git land in it;
- the run's artifacts are folders, and the Pages actions keep what they
  deploy;
- gh (releases, attestations, `run download`), curl (a release's package),
  asp (the index commands, with a key compiled in) and, when it isn't
  installed, jq are stand-ins: `python3 -I dry_run.py <tool> …`;
- the asp jobs and the build-plugin.yml calls aren't run: an asp job hands
  over the asp stand-in, and each build puts its package among the run's
  artifacts and attests it, unless the test says it fails;
- the clock: asp's stand-in reads the time from the world (World.wait moves
  it on), so a test can let an index run out.

It needs a POSIX system with bash, git, GNU coreutils and findutils, and
PyYAML. Standard library otherwise.
"""

import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.parse
import zipfile
from pathlib import Path

REPO = "tcmmi/autoscene-plugins"
ZERO = "0" * 40
# The key asp's stand-in has compiled in, and its password.
KEY = "dry-run-key"
PASSWORD = "dry-run-password"
SIGNER = f"{REPO}/.github/workflows/build-plugin.yml"
# The world's clock starts on a Monday, 2026-11-02 04:17 UTC, in Unix seconds.
START = 1_793_593_020
DAY = 86_400
# The longest an index may live (catalog.rs MAX_LIFETIME).
MAX_LIFETIME = 31 * DAY
TOOLS = ("gh", "curl", "asp", "jq")


# Expressions -------------------------------------------------------------------

TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>[0-9]+)|(?P<op>==|!=|&&|\|\||!|\(|\)|,)"
    r"|(?P<name>[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*))"
)
STATUS = re.compile(r"\b(?:success|failure|cancelled|always)\(")
EXPR = re.compile(r"\$\{\{(.*?)\}\}", re.S)


def tokens(text):
    out, at = [], 0
    while at < len(text):
        if text[at:].strip() == "":
            break
        m = TOKEN.match(text, at)
        if not m:
            raise ValueError(f"can't read the expression {text!r} at {at}")
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        at = m.end()
    return out


def truthy(value):
    return value not in (None, "", False, 0)


def text_of(value):
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


class Evaluator:
    """GitHub's expression syntax, as far as the catalog's workflows use it."""

    def __init__(self, context):
        self.context = context

    def __call__(self, text):
        self.toks, self.at = tokens(text), 0
        value = self.either()
        if self.at != len(self.toks):
            raise ValueError(f"left over in {text!r}: {self.toks[self.at:]}")
        return value

    def peek(self):
        return self.toks[self.at] if self.at < len(self.toks) else (None, None)

    def take(self, op=None):
        tok = self.peek()
        if op is not None and tok != ("op", op):
            raise ValueError(f"expected {op}, got {tok}")
        self.at += 1
        return tok

    def either(self):
        value = self.both()
        while self.peek() == ("op", "||"):
            self.take()
            other = self.both()
            value = value if truthy(value) else other
        return value

    def both(self):
        value = self.unary()
        while self.peek() == ("op", "&&"):
            self.take()
            other = self.unary()
            value = other if truthy(value) else value
        return value

    def unary(self):
        if self.peek() == ("op", "!"):
            self.take()
            return not truthy(self.unary())
        value = self.primary()
        if self.peek() in (("op", "=="), ("op", "!=")):
            op = self.take()[1]
            other = self.primary()
            same = text_of(value).lower() == text_of(other).lower()
            return same if op == "==" else not same
        return value

    def primary(self):
        kind, value = self.take()
        if kind == "str":
            return value[1:-1].replace("''", "'")
        if kind == "num":
            return int(value)
        if (kind, value) == ("op", "("):
            inner = self.either()
            self.take(")")
            return inner
        if kind != "name":
            raise ValueError(f"unexpected {value!r}")
        if value in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[value]
        if self.peek() == ("op", "("):
            self.take()
            args = []
            while self.peek() != ("op", ")"):
                args.append(self.either())
                if self.peek() == ("op", ","):
                    self.take()
            self.take(")")
            return self.function(value, args)
        found = self.context
        for part in value.split("."):
            found = found.get(part) if isinstance(found, dict) else None
        return found

    def function(self, name, args):
        if name == "fromJSON":
            return json.loads(args[0])
        if name == "toJSON":
            return json.dumps(args[0])
        if name in ("success", "failure", "cancelled", "always"):
            return self.context["_status"][name]
        raise ValueError(f"no function {name}")


def render(value, context):
    """A value with each ${{ }} replaced by its text."""
    if isinstance(value, dict):
        return {k: render(v, context) for k, v in value.items()}
    if not isinstance(value, str):
        return text_of(value)
    return EXPR.sub(lambda m: text_of(Evaluator(context)(m.group(1))), value)


def condition(text, context):
    """An `if:`; without a status function it holds only after success()."""
    if text is None:
        return context["_status"]["success"]
    if isinstance(text, bool):
        return text and context["_status"]["success"]
    text = text.strip()
    m = EXPR.fullmatch(text)
    if m:
        text = m.group(1)
    value = truthy(Evaluator(context)(text))
    if not STATUS.search(text):
        value = value and context["_status"]["success"]
    return value


# The world: GitHub's side of a run --------------------------------------------------


def git(*args, cwd=None, check=True):
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if check and done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout.strip()


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class World:
    """The catalog on GitHub: a bare repo, the releases and attestations gh
    knows, and what Pages deployed. `package(id, version)` gives the bytes
    a build of that version packs."""

    def __init__(self, root, package, workflows):
        self.root = Path(root)
        self.package = package
        self.bare = self.root / "github.git"
        self.work = self.root / "work"
        self.tools = self.root / "tools"
        self.deployed = []
        self.runs = 0
        git("init", "-q", "--bare", "-b", "main", str(self.bare))
        git("init", "-q", "-b", "main", str(self.work))
        for name, value in (("user.name", "Owner"), ("user.email", "owner@example.invalid")):
            git("config", name, value, cwd=self.work)
        git("remote", "add", "origin", str(self.bare), cwd=self.work)
        self.workflows = Path(workflows)
        self.tools.mkdir()
        here = Path(__file__).resolve()
        for tool in TOOLS:
            if tool == "jq" and shutil.which("jq"):
                continue
            wrapper = self.tools / tool
            wrapper.write_text(
                f"#!/bin/sh\nexec '{sys.executable}' -I '{here}' {tool} \"$@\"\n", encoding="utf-8", newline="\n"
            )
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.save_gh({"releases": {}, "attestations": {}, "immutable": True})
        self.set_clock(START)

    # The time asp's stand-in reads, in Unix seconds.
    def now(self):
        return int((self.root / "clock").read_text(encoding="utf-8"))

    def set_clock(self, now):
        (self.root / "clock").write_text(f"{now}\n", encoding="utf-8")

    def wait(self, days):
        self.set_clock(self.now() + days * DAY)

    # GitHub's state, as gh sees it.
    def gh(self):
        return json.loads((self.root / "gh.json").read_text(encoding="utf-8"))

    def save_gh(self, state):
        (self.root / "gh.json").write_text(json.dumps(state, indent=1), encoding="utf-8")

    def calls(self, tool):
        log = self.root / "calls.jsonl"
        if not log.exists():
            return []
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        return [r["args"] for r in rows if r["tool"] == tool]

    def commit(self, files, message):
        """Commits `files` ({path: text, or None to remove}) to main, as a
        merged pull request would. Returns (before, after)."""
        before = git("rev-parse", "--verify", "-q", "HEAD", cwd=self.work, check=False) or ZERO
        if before != ZERO:
            git("fetch", "-q", "origin", "main", cwd=self.work)
            git("reset", "-q", "--hard", "FETCH_HEAD", cwd=self.work)
            before = git("rev-parse", "HEAD", cwd=self.work)
        for path, text in files.items():
            file = self.work / path
            if text is None:
                file.unlink()
                continue
            file.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(text, bytes):
                file.write_bytes(text)
            else:
                file.write_text(text, encoding="utf-8", newline="\n")
        git("add", "-A", cwd=self.work)
        git("commit", "-q", "--allow-empty", "-m", message, cwd=self.work)
        git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=self.work)
        return before, git("rev-parse", "HEAD", cwd=self.work)

    def main(self):
        return git("--git-dir", str(self.bare), "rev-parse", "refs/heads/main")

    def show(self, path, rev="main"):
        done = subprocess.run(
            ["git", "--git-dir", str(self.bare), "show", f"{rev}:{path}"], capture_output=True, check=False
        )
        return done.stdout.decode("utf-8") if done.returncode == 0 else None

    def run(self, before, sha, event="push", secrets=None, fail=(), unattested=(), attest=None,
            asp_fails=False, extra=None, hooks=None, workflow="publish.yml"):
        """One run of `workflow` for a push from `before` to `sha`, or for
        `event` (workflow_dispatch, schedule) at `sha`, the commit GitHub
        names then. `fail`: (id, version) builds that fail; `unattested`:
        builds whose package isn't attested; `attest`: the attestation
        fields to use instead (workflow, ref); `extra`: more artifacts a
        build leaves ({name: {file: bytes}}); `hooks`: {job: callable} run
        before that job."""
        self.runs += 1
        return Run(self, before, sha, event, secrets, set(fail), set(unattested), attest or {},
                   asp_fails, extra or {}, hooks or {}, workflow).go()


class Run:
    def __init__(self, world, before, sha, event, secrets, fail, unattested, attest, asp_fails, extra, hooks,
                 workflow):
        import yaml

        self.world, self.before, self.sha, self.event = world, before, sha, event
        self.fail, self.unattested, self.attest = fail, unattested, attest
        self.asp_fails, self.extra, self.hooks = asp_fails, extra, hooks
        self.id = str(41 + world.runs)
        self.dir = world.root / f"run-{world.runs}"
        self.artifacts = self.dir / "artifacts"
        self.artifacts.mkdir(parents=True)
        self.doc = yaml.safe_load((world.workflows / workflow).read_text(encoding="utf-8"))
        event_data = {"push": {"before": before}, "schedule": {"schedule": "17 4 * * 1"}}.get(event, {})
        self.github = {
            "sha": sha, "repository": REPO, "token": "ghs_dryrun", "event_name": event,
            "ref": "refs/heads/main", "server_url": "https://github.com", "run_id": self.id, "event": event_data,
        }
        self.secrets = dict(secrets if secrets is not None else {
            "CATALOG_KEY": KEY, "CATALOG_KEY_PASSWORD": PASSWORD, "CATALOG_DEPLOY_KEY": "dry-run-deploy-key",
        })
        self.jobs = {}
        self.log = []

    def go(self):
        for jid, job in self.doc["jobs"].items():
            if jid in self.hooks:
                self.hooks[jid]()
            self.job(jid, job)
        return self

    def needs(self, job):
        needs = job.get("needs", [])
        return [needs] if isinstance(needs, str) else list(needs)

    def upstream(self, jid, seen=None):
        seen = set() if seen is None else seen
        for n in self.needs(self.doc["jobs"][jid]):
            if n not in seen:
                seen.add(n)
                self.upstream(n, seen)
        return seen

    def context(self, jid, steps=None, ok=True):
        above = self.upstream(jid)
        results = [self.jobs[n]["result"] for n in above]
        if steps is not None:
            status = {"success": ok, "failure": not ok, "cancelled": False, "always": True}
        else:
            status = {
                "success": all(r == "success" for r in results),
                "failure": any(r == "failure" for r in results),
                "cancelled": False, "always": True,
            }
        return {
            "github": self.github, "secrets": self.secrets, "_status": status,
            "needs": {n: self.jobs[n] for n in self.needs(self.doc["jobs"][jid])},
            "steps": steps or {},
        }

    def say(self, line):
        self.log.append(line)

    def job(self, jid, job):
        if not condition(job.get("if"), self.context(jid)):
            self.jobs[jid] = {"result": "skipped", "outputs": {}}
            self.say(f"== {jid}: skipped")
            return
        self.say(f"== {jid}")
        if jid == "asp":
            self.jobs[jid] = self.asp_job()
        elif "uses" in job:
            self.jobs[jid] = self.build_job(jid)
        else:
            self.jobs[jid] = self.steps_job(jid, job)
        self.say(f"== {jid}: {self.jobs[jid]['result']}")

    def asp_job(self):
        if self.asp_fails:
            return {"result": "failure", "outputs": {}}
        folder = self.artifacts / "tool-asp-sign"
        folder.mkdir()
        shutil.copy(self.world.tools / "asp", folder / "asp")
        return {"result": "success", "outputs": {"sha256": sha256_file(folder / "asp")}}

    def build_job(self, jid):
        versions = json.loads(self.jobs["plan"]["outputs"]["versions"])
        state = self.world.gh()
        failed = False
        for item in versions:
            key = (item["id"], item["version"])
            if key in self.fail:
                failed = True
                self.say(f"build {key}: fails")
                continue
            data = self.world.package(*key)
            folder = self.artifacts / f"asp-{key[0]}-{key[1]}"
            folder.mkdir()
            (folder / f"{key[0]}-{key[1]}.asp").write_bytes(data)
            if key not in self.unattested:
                state["attestations"][hashlib.sha256(data).hexdigest()] = {
                    "workflow": self.attest.get("workflow", SIGNER),
                    "ref": self.attest.get("ref", "refs/heads/main"),
                    "digest": self.sha,
                }
        for name, files in self.extra.items():
            folder = self.artifacts / name
            folder.mkdir()
            for file, data in files.items():
                (folder / file).write_bytes(data)
        self.world.save_gh(state)
        return {"result": "failure" if failed else "success", "outputs": {}}

    def steps_job(self, jid, job):
        base = self.dir / jid
        workspace, temp, home = base / "ws", base / "tmp", base / "home"
        for d in (workspace, temp, home):
            d.mkdir(parents=True)
        steps, ok = {}, True
        for i, step in enumerate(job.get("steps", [])):
            ctx = self.context(jid, steps, ok)
            if not condition(step.get("if"), ctx):
                self.say(f"-- {step.get('name')}: skipped")
                if "id" in step:
                    steps[step["id"]] = {"outputs": {}, "outcome": "skipped", "conclusion": "skipped"}
                continue
            env = {**render(job.get("env") or {}, ctx), **render(step.get("env") or {}, ctx)}
            out_file = temp / f"output-{i}"
            out_file.write_text("", encoding="utf-8")
            if "uses" in step:
                good, outputs = self.action(step, render(step.get("with") or {}, ctx), workspace)
            else:
                good = self.script(step["run"], env, workspace, temp, home, out_file, step.get("name"))
                outputs = read_outputs(out_file)
            self.say(f"-- {step.get('name')}: {'ok' if good else 'FAILED'}")
            if "id" in step:
                result = "success" if good else "failure"
                steps[step["id"]] = {"outputs": outputs, "outcome": result, "conclusion": result}
            ok = ok and good
        ctx = self.context(jid, steps, ok)
        return {"result": "success" if ok else "failure", "outputs": render(job.get("outputs") or {}, ctx),
                "workspace": workspace}

    def script(self, run, env, workspace, temp, home, out_file, name):
        script = temp / "step.sh"
        script.write_text(run, encoding="utf-8", newline="\n")
        for f in ("summary", "env", "path"):
            (temp / f).touch()
        full = {
            "PATH": f"{self.world.tools}:{os.environ['PATH']}", "HOME": str(home), "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "CI": "true",
            "GITHUB_ACTIONS": "true", "GITHUB_SHA": self.sha, "GITHUB_REPOSITORY": REPO,
            "GITHUB_SERVER_URL": "https://github.com", "GITHUB_RUN_ID": self.id, "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": self.event, "GITHUB_WORKSPACE": str(workspace), "RUNNER_TEMP": str(temp),
            "GITHUB_OUTPUT": str(out_file), "GITHUB_STEP_SUMMARY": str(temp / "summary"),
            "GITHUB_ENV": str(temp / "env"), "GITHUB_PATH": str(temp / "path"),
            "DRY_RUN": str(self.world.root), "DRY_RUN_ARTIFACTS": str(self.artifacts),
            **env,
        }
        done = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
            cwd=workspace, env=full, capture_output=True, check=False,
        )
        text = (done.stdout + done.stderr).decode("utf-8", "replace").rstrip()
        if text:
            self.say(text)
        return done.returncode == 0

    def action(self, step, given, workspace):
        name = step["uses"].split("@")[0]
        try:
            if name == "actions/checkout":
                dest = workspace / given.get("path", ".")
                git("clone", "-q", str(self.world.bare), str(dest))
                ref = given.get("ref") or self.sha
                git("checkout", "-q", "--detach", "origin/main" if ref == "main" else ref, cwd=dest)
                git("config", f"url.{self.world.bare}.insteadOf", f"git@github.com:{REPO}.git", cwd=dest)
                return True, {}
            if name == "actions/download-artifact":
                if "name" not in given or "pattern" in given:
                    raise RuntimeError("download-artifact takes one artifact by its name here")
                source = self.artifacts / given["name"]
                if not source.is_dir():
                    raise RuntimeError(f"no artifact {given['name']}")
                shutil.copytree(source, workspace / given.get("path", "."), dirs_exist_ok=True)
                return True, {}
            if name == "actions/upload-artifact":
                target = self.artifacts / given["name"]
                if target.exists():
                    raise RuntimeError(f"an artifact {given['name']} is there already")
                source = workspace / given["path"]
                if source.is_dir():
                    shutil.copytree(source, target)
                elif source.is_file():
                    target.mkdir()
                    shutil.copy(source, target / source.name)
                else:
                    raise RuntimeError(f"nothing at {given['path']}")
                return True, {}
            if name == "actions/configure-pages":
                return True, {}
            if name == "actions/upload-pages-artifact":
                self.pages = {
                    str(p.relative_to(workspace / given["path"])): p.read_bytes()
                    for p in (workspace / given["path"]).rglob("*") if p.is_file()
                }
                return True, {}
            if name == "actions/deploy-pages":
                self.world.deployed.append(self.pages)
                return True, {"page_url": "https://tcmmi.github.io/autoscene-plugins/"}
            raise RuntimeError(f"no stand-in for {step['uses']}")
        except RuntimeError as e:
            self.say(f"{name}: {e}")
            return False, {}


def read_outputs(path):
    outputs, lines = {}, path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            key, mark = line.split("<<", 1)
            end = lines.index(mark, i + 1)
            outputs[key] = "\n".join(lines[i + 1 : end])
            i = end + 1
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            outputs[key] = value
        i += 1
    return outputs


# The stand-ins -------------------------------------------------------------------


def fail(message, code=1):
    sys.stderr.write(message + "\n")
    sys.exit(code)


def flag(args, name):
    """`name value` or `name=value` from args (removed), or None."""
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            value = args[i + 1]
            del args[i : i + 2]
            return value
        if a.startswith(name + "="):
            del args[i]
            return a.split("=", 1)[1]
    return None


def switch(args, name):
    if name in args:
        args.remove(name)
        return True
    return False


def gh(args, world):
    state = json.loads((world / "gh.json").read_text(encoding="utf-8"))
    if not os.environ.get("GH_TOKEN") or os.environ.get("GH_REPO") not in (None, REPO):
        fail("gh needs GH_TOKEN, and GH_REPO must be the catalog")
    releases = state["releases"]

    def save():
        (world / "gh.json").write_text(json.dumps(state, indent=1), encoding="utf-8")

    if args[:2] == ["release", "view"]:
        tag, rest = args[2], args[3:]
        fields, jq = flag(rest, "--json"), flag(rest, "--jq")
        if tag not in releases:
            fail("release not found")
        r = releases[tag]
        data = {"isDraft": r["draft"], "isImmutable": r["immutable"],
                "assets": [{"name": n} for n in r["assets"]]}
        if jq == ".assets[].name":
            print("\n".join(r["assets"]))
        elif jq == ".isImmutable":
            print("true" if r["immutable"] else "false")
        elif jq is None:
            print(json.dumps({f: data[f] for f in fields.split(",")}))
        else:
            fail(f"no --jq {jq} here")
        return
    if args[:2] == ["release", "delete"]:
        tag, rest = args[2], args[3:]
        if not switch(rest, "--yes") or tag not in releases:
            fail("delete needs --yes and a release")
        if not releases[tag]["draft"] and releases[tag]["immutable"]:
            fail("an immutable release isn't deleted here")
        del releases[tag]
        return save()
    if args[:2] == ["release", "download"]:
        tag, rest = args[2], args[3:]
        pattern, folder = flag(rest, "--pattern"), flag(rest, "--dir")
        if tag not in releases or pattern not in releases[tag]["assets"]:
            fail("no assets match the file pattern")
        Path(folder).mkdir(parents=True, exist_ok=True)
        shutil.copy(releases[tag]["assets"][pattern], Path(folder) / pattern)
        return
    if args[:2] == ["release", "create"]:
        tag, rest = args[2], args[3:]
        if tag in releases:
            fail("a release with the same tag name already exists")
        draft, latest = switch(rest, "--draft"), flag(rest, "--latest")
        target, title, notes = flag(rest, "--target"), flag(rest, "--title"), flag(rest, "--notes")
        if rest or latest != "false" or not draft:
            fail(f"release create: unexpected {rest} latest={latest} draft={draft}")
        releases[tag] = {"draft": True, "immutable": False, "assets": {}, "target": target,
                         "title": title, "notes": notes}
        return save()
    if args[:2] == ["release", "upload"]:
        tag, file = args[2], args[3]
        if args[4:]:
            fail(f"release upload: unexpected {args[4:]} (never --clobber)")
        r = releases.get(tag) or fail("release not found")
        if r["immutable"]:
            fail("an immutable release can't change")
        # GitHub changes characters it doesn't take in an asset's name.
        name = Path(file).name.replace("+", ".")
        if name in r["assets"]:
            fail("an asset with that name already exists")
        store = world / "assets" / tag
        store.mkdir(parents=True, exist_ok=True)
        shutil.copy(file, store / name)
        r["assets"][name] = str(store / name)
        return save()
    if args[:2] == ["release", "edit"]:
        tag, rest = args[2], args[3:]
        draft, latest = flag(rest, "--draft"), flag(rest, "--latest")
        if rest or draft != "false" or latest != "false" or tag not in releases:
            fail("release edit: unexpected arguments")
        releases[tag]["draft"] = False
        releases[tag]["immutable"] = state["immutable"]
        return save()
    if args[:2] == ["attestation", "verify"]:
        file, rest = args[2], args[3:]
        repo, signer = flag(rest, "--repo"), flag(rest, "--signer-workflow")
        ref, digest = flag(rest, "--source-ref"), flag(rest, "--source-digest")
        hosted = switch(rest, "--deny-self-hosted-runners")
        if rest or repo != REPO:
            fail(f"attestation verify: unexpected {rest} repo={repo}")
        found = state["attestations"].get(hashlib.sha256(Path(file).read_bytes()).hexdigest())
        if found is None:
            fail("no attestations found")
        # As gh: only what is asked is checked.
        for asked, have in ((signer, found["workflow"]), (ref, found["ref"]), (digest, found["digest"])):
            if asked is not None and asked != have:
                fail(f"the attestation's {have} isn't {asked}")
        print(f"verified (self-hosted runners {'denied' if hosted else 'allowed'})")
        return
    if args[:2] == ["run", "download"]:
        run, rest = args[2], args[3:]
        name, folder = flag(rest, "--name"), flag(rest, "--dir")
        if rest or run != os.environ["GITHUB_RUN_ID"] or not name or not folder:
            fail("run download: this run's artifact by --name into --dir")
        source = Path(os.environ["DRY_RUN_ARTIFACTS"]) / name
        if not source.is_dir():
            fail("no valid artifacts found to download")
        shutil.copytree(source, folder, dirs_exist_ok=True)
        return
    fail(f"no stand-in for gh {' '.join(args[:2])}")


def curl(args, world):
    url = args[-1]
    out = flag(args, "--output")
    if flag(args, "--proto") != "=https" or flag(args, "--proto-redir") != "=https":
        fail("curl: https only", 2)
    m = re.fullmatch(rf"https://github\.com/{re.escape(REPO)}/releases/download/([^/]+)/([^/]+)", url)
    state = json.loads((world / "gh.json").read_text(encoding="utf-8"))
    if m:
        tag, asset = urllib.parse.unquote(m.group(1)), urllib.parse.unquote(m.group(2))
        r = state["releases"].get(tag)
        if r and not r["draft"] and asset in r["assets"]:
            shutil.copy(r["assets"][asset], out)
            return
    fail("curl: (22) The requested URL returned error: 404", 22)


def read_index(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_index(path, index):
    Path(path).write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def signature(data, key):
    return hmac.new(key.encode(), data, hashlib.sha256).hexdigest()


def asp(args, world):
    if args[0] != "catalog":
        fail(f"asp {args[0]}: not here")
    command, path, rest = args[1], args[2], args[3:]
    now = int((world / "clock").read_text(encoding="utf-8"))
    if command == "new":
        if Path(path).exists():
            fail("the index is there already")
        return write_index(path, {"epoch": 1, "seq": 0, "published": 0, "expires": 0, "plugins": [],
                                  "withdrawn": []})
    index = read_index(path)
    if command == "add":
        pkg, rest = rest[0], rest[1:]
        fields = {k: flag(rest, f"--{k}") for k in ("url", "repo", "commit", "path", "author", "author-key")}
        with zipfile.ZipFile(pkg) as z:
            manifest = json.loads(z.read("plugin.json"))
        if any(p["id"] == manifest["id"] and p["version"] == manifest["version"] for p in index["plugins"]):
            fail("the index lists this version already")
        entry = {"id": manifest["id"], "version": manifest["version"], "sha256": sha256_file(pkg)}
        entry.update({k: v for k, v in fields.items() if v is not None})
        index["plugins"] = sorted(index["plugins"] + [entry], key=lambda p: (p["id"], p["version"]))
    elif command == "withdrawn":
        index["withdrawn"] = json.loads(Path(rest[0]).read_text(encoding="utf-8"))
    elif command == "epoch":
        n = int(re.search(r"epoch-([0-9]+)\.minisig$", rest[0]).group(1))
        if n <= index["epoch"]:
            fail("the epoch must rise")
        index["epoch"] = n
    elif command == "bump":
        days = int(flag(rest, "--days"))
        if not 1 <= days <= MAX_LIFETIME // DAY:
            fail("asp catalog bump: --days must be 1 to 31")
        index["seq"] += 1
        index["published"], index["expires"] = now, now + days * DAY
    elif command == "sign":
        key = os.environ.get(flag(rest, "--key-env") or "")
        password = os.environ.get(flag(rest, "--password-env") or "")
        if not key or password != PASSWORD:
            fail("asp catalog sign: no key, or the wrong password")
        data = Path(path).read_bytes()
        Path(path + ".minisig").write_text(signature(data, key) + "\n", encoding="utf-8")
        return
    elif command == "verify":
        previous = flag(rest, "--previous")
        sig = Path(path + ".minisig")
        if not sig.is_file() or sig.read_text(encoding="utf-8").strip() != signature(Path(path).read_bytes(), KEY):
            fail("asp catalog verify: the index isn't signed by a key asp knows")
        # As catalog::check: the signature first, then the dates.
        if index["published"] > index["expires"] or index["expires"] - index["published"] > MAX_LIFETIME:
            fail("asp catalog verify: The catalog's dates can't be right.")
        if index["expires"] <= now:
            fail(f"asp catalog verify: The catalog ran out at {index['expires']}.")
        if previous:
            old = read_index(previous)
            keep = {(p["id"], p["version"], p["sha256"]) for p in index["plugins"]}
            if not {(p["id"], p["version"], p["sha256"]) for p in old["plugins"]} <= keep:
                fail("asp catalog verify: an entry of the index before is gone")
            if index["seq"] <= old["seq"] or index["epoch"] < old["epoch"]:
                fail("asp catalog verify: seq must rise")
        return
    else:
        fail(f"asp catalog {command}: not here")
    write_index(path, index)


def jq(args):
    """The jq filters the workflows use."""
    raw = switch(args, "-r")
    compact = switch(args, "-c")
    named = {}
    while "--arg" in args:
        i = args.index("--arg")
        named[args[i + 1]] = args[i + 2]
        del args[i : i + 3]
    program, files = args[0], args[1:]
    data = json.loads(Path(files[0]).read_text(encoding="utf-8") if files else sys.stdin.read())
    if program == ". + [{id: $id, version: $version, sha256: $sha256}]":
        out = [data + [{"id": named["id"], "version": named["version"], "sha256": named["sha256"]}]]
    elif program == ".[] | [.id, .version] | @tsv":
        out = ["\t".join((i["id"], i["version"])) for i in data]
    elif re.fullmatch(r"\.[A-Za-z][A-Za-z0-9]*", program):
        out = [data.get(program[1:])]
    else:
        fail(f"jq: no filter {program} here")
    for value in out:
        if raw and isinstance(value, str):
            print(value)
        elif isinstance(value, bool):
            print("true" if value else "false")
        else:
            print(json.dumps(value, separators=(",", ":")) if compact else json.dumps(value))


def main(argv):
    tool, args = argv[1], argv[2:]
    world = Path(os.environ["DRY_RUN"])
    with open(world / "calls.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"tool": tool, "args": args}) + "\n")
    if tool == "gh":
        gh(list(args), world)
    elif tool == "curl":
        curl(list(args), world)
    elif tool == "asp":
        asp(list(args), world)
    elif tool == "jq":
        jq(list(args))
    else:
        fail(f"no stand-in {tool}")


if __name__ == "__main__":
    main(sys.argv)
