#!/usr/bin/env python3
"""Tests of plan.py: the rules a pull request and a plugin's folder are
held to (R28), and what publish.yml asks of it before a release and around
the signing. Then the workflows: the lint (lint_workflows.py), publish.yml's
keyed steps word for word, and a dry run of publish.yml (dry_run.py).
check.yml's first job runs them as `python3 -I scripts/test_plan.py`. They
need git; the workflow tests need PyYAML, and the dry run a POSIX system with
bash. Under CI those fail instead of skipping."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True  # no __pycache__ beside plan.py
_spec = importlib.util.spec_from_file_location("plan", HERE / "plan.py")
plan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plan)

ID = "io.github.alice.clip-renamer"
REPO = "https://github.com/alice/clip-renamer"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
REV = "89abcdef0123456789abcdef0123456789abcdef"
HOLDER = 'author = "Alice Example"\ngithub = ["alice"]\n'
SETTINGS = f'uv = "0.13.7"\nasp = {{ rev = "{REV}" }}\ndays = 30\n'
BINS = {
    "windows-x86_64": "target/release/clip-renamer.exe",
    "macos-aarch64": "target/release/clip-renamer",
    "linux-x86_64": "target/release/clip-renamer",
}
BUILT = sorted(f"bin/{p}/{Path(b).name}" for p, b in BINS.items())
UV = ["uv", "run", "--script", "--locked", "main.py"]
WASM = {"type": "wasm", "component": "plugin.wasm"}
ARTIFACT = "target/wasm32-wasip2/release/clip_renamer.wasm"
RUST_KEYS = f'artifact = "{ARTIFACT}"\n'
PYTHON_KEYS = 'componentize_py = "0.25.1"\n'
# A pin with two hashes, as uv pip compile --generate-hashes writes it.
PIN = (
    f"idna==3.11 \\\n    --hash=sha256:{'a' * 64} \\\n    --hash=sha256:{'b' * 64}\n"
    "    # via -r requirements.in\n"
)
LOCK = (
    'version = 1\nrevision = 3\nrequires-python = ">=3.10"\n\n'
    '[manifest]\nrequirements = [{ name = "truststore", specifier = "==0.10.5" }]\n\n'
    '[[package]]\nname = "truststore"\nversion = "0.10.5"\nsource = { registry = "https://pypi.org/simple" }\n'
)
KEY = "RWQf6LRCGA9i53mlYecO4IzT51TGPpvWucNSCh1CBM0QTaLn73Y7GFO3"
# The first bytes of an ONNX file as PyTorch writes one: ir_version 8, then
# producer_name and producer_version.
ONNX = b"\x08\x08\x12\x07pytorch\x1a\x052.9.0" + b"\0" * 32
# A tar's first block: a name, then "ustar" at byte 257.
TAR = b"weights.bin".ljust(257, b"\0") + b"ustar\x0000" + b"\0" * 244

_saved_env = {}


def setUpModule():
    # git as these tests need it, whatever the computer's own settings say.
    folder = tempfile.mkdtemp(prefix="test-plan-git-")
    empty = Path(folder) / "gitconfig"
    empty.write_text("", encoding="utf-8")
    values = {
        "GIT_CONFIG_GLOBAL": str(empty),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GITHUB_OUTPUT": "",
        "GITHUB_STEP_SUMMARY": "",
    }
    for name, value in values.items():
        _saved_env[name] = os.environ.get(name)
        os.environ[name] = value
    for name in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"):
        del os.environ[name]


def tearDownModule():
    for name, value in _saved_env.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def version_toml(kind="uv-script", repo=REPO, commit=COMMIT, path=".", bins=None, extra=""):
    text = f'[source]\nrepo = "{repo}"\ncommit = "{commit}"\npath = "{path}"\n\n[build]\nkind = "{kind}"\n'
    if bins is None and kind == "cargo":
        bins = BINS
    if bins:
        text += "\n[build.bins]\n" + "".join(f'"{p}" = "{b}"\n' for p, b in bins.items())
    return text + extra


def manifest(id_=ID, version="1.0.0", repo=REPO, runtime=None, **more):
    m = {
        "manifestVersion": 2,
        "id": id_,
        "version": version,
        "name": "Clip Renamer",
        "description": "Renames clips.",
        "author": {"name": "Alice Example"},
        "repository": repo,
        "engines": {"autoscene": ">=1.1.0, <2", "protocol": 1},
        "runtime": runtime if runtime is not None else {"type": "process", "command": {"*": UV}},
    }
    m.update(more)
    return m


def hf_url(name):
    return f"https://huggingface.co/alice/weights/resolve/{COMMIT}/{name}"


def resource(name, data, urls=None, more_urls=()):
    """A plugin.json resource for `data`, at a pinned Hugging Face URL."""
    urls = list(urls if urls is not None else [hf_url(name)]) + list(more_urls)
    return {"name": name, "urls": urls, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def index_json(plugins=(), epoch=1, seq=0, withdrawn=()):
    """An index.json as `asp catalog` writes one (spec C3), with the fields
    plan.py reads."""
    return {
        "catalogVersion": 1, "epoch": epoch, "seq": seq, "published": 1793750400, "expires": 1796342400,
        "plugins": [{"id": i, "version": v, "lock": "0" * 64} for i, v in plugins],
        "withdrawn": list(withdrawn),
    }


def make_package(folder, id_=ID, version="1.0.0", **manifest_fields):
    """An .asp at <folder>/asp-<id>-<version>/<id>-<version>.asp, as the
    build's artifact lays it out."""
    pkg = Path(folder) / f"asp-{id_}-{version}" / f"{id_}-{version}.asp"
    pkg.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(pkg, "w") as z:
        z.writestr("files.lock", json.dumps({"lockVersion": 1, "files": []}))
        z.writestr("plugin.json", json.dumps(manifest(id_=id_, version=version, **manifest_fields)))
    return pkg


def cargo_runtime(**command):
    command = command or {p: [f"bin/{p}/{Path(b).name}"] for p, b in BINS.items()}
    return {"type": "process", "command": command}


class Repo:
    """A catalog repo in a temp folder."""

    def __init__(self, test):
        self.dir = Path(tempfile.mkdtemp(prefix="test-plan-repo-"))
        test.addCleanup(lambda: _remove(self.dir))
        self.git("init", "-q", "-b", "main")
        self.write("catalog.toml", SETTINGS)
        self.write("README.md", "The catalog.\n")
        self.base = self.commit("R1")

    def git(self, *args):
        done = subprocess.run(["git", "-C", str(self.dir), *args], capture_output=True, text=True, check=True)
        return done.stdout.strip()

    def write(self, path, text):
        file = self.dir / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8", newline="\n")

    def remove(self, path):
        (self.dir / path).unlink()

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def add_plugin(self, version="1.0.0", text=None, id_=ID):
        self.write(f"plugins/{id_}/plugin.toml", HOLDER)
        self.write(f"plugins/{id_}/{version}.toml", text if text is not None else version_toml())


def _remove(path):
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def problems_of(call, *args, **kwargs):
    try:
        call(*args, **kwargs)
    except plan.Refused as e:
        return "\n".join(e.problems)
    raise AssertionError("it wasn't refused")


def curl_child(body=b"", mib=0, said="", led="https://cdn.example.org/x 93.184.215.14", code=0, calls=None):
    """plan.curl_args patched to a child process that does what curl would:
    writes `body` (and `mib` MiB more) to stdout, then `said` and the line
    with the mark and where it `led` (None: no such line) to stderr, and
    exits with `code`. `calls` gets the url, size and seconds of each run."""
    script = (
        "import sys\n"
        f"sys.stdout.buffer.write({body!r})\n"
        f"for _ in range({mib}): sys.stdout.buffer.write(b'x' * (1 << 20))\n"
        "sys.stdout.flush()\n"
        f"sys.stderr.write({said!r})\n"
        + (f"sys.stderr.write('\\n' + sys.argv[1] + ' ' + {led!r} + '\\n')\n" if led is not None else "")
        + f"sys.exit({code})\n"
    )

    def args(url, size, seconds, mark):
        if calls is not None:
            calls.append((url, size, seconds))
        return [sys.executable, "-I", "-c", script, mark]

    return unittest.mock.patch.object(plan, "curl_args", args)


class PullRequests(unittest.TestCase):
    def test_a_new_version_is_planned_with_who_holds_its_id(self):
        repo = Repo(self)
        repo.add_plugin()
        repo.commit("add")
        versions, notes, index, _ = plan.changes(repo.dir, repo.base)
        self.assertEqual([(v.id, v.version, v.kind) for v in versions], [(ID, "1.0.0", "uv-script")])
        self.assertTrue(index)
        self.assertTrue(any("plugin.toml" in n for n in notes))

    def test_a_changed_approved_version_file_is_refused(self):
        repo = Repo(self)
        repo.add_plugin()
        approved = repo.commit("approved")
        repo.write(f"plugins/{ID}/1.0.0.toml", version_toml(commit="f" * 40))
        repo.commit("change it")
        self.assertIn("never changes", problems_of(plan.changes, repo.dir, approved))
        repo.remove(f"plugins/{ID}/1.0.0.toml")
        repo.commit("remove it")
        self.assertIn("never changes", problems_of(plan.changes, repo.dir, approved))

    def test_a_source_outside_github_is_refused(self):
        for repo_url in (
            "https://gitlab.com/alice/clip-renamer",
            "http://github.com/alice/clip-renamer",
            "https://github.com.example.com/alice/clip-renamer",
            "https://github.com/alice/clip-renamer.git",
            "https://github.com/alice/clip-renamer/",
            "https://github.com/alice",
        ):
            with self.subTest(repo_url):
                repo = Repo(self)
                repo.add_plugin(text=version_toml(repo=repo_url))
                repo.commit("add")
                self.assertIn("must be a repo on GitHub", problems_of(plan.changes, repo.dir, repo.base))

    def test_a_folder_not_named_as_an_id_and_a_file_not_named_as_a_version_are_refused(self):
        repo = Repo(self)
        repo.add_plugin(id_="Clip_Renamer")
        repo.add_plugin(version="1.0", id_="io.github.alice.other")
        repo.commit("add")
        problems = problems_of(plan.changes, repo.dir, repo.base)
        self.assertIn("plugins/Clip_Renamer/ must be named after a plugin id", problems)
        self.assertIn("must be named after the version", problems)

    def test_the_commit_must_be_whole_and_the_kind_known(self):
        repo = Repo(self)
        repo.add_plugin(text=version_toml(commit="0123456"))
        repo.add_plugin(version="1.0.2", text=version_toml(kind="npm"))
        repo.commit("add")
        problems = problems_of(plan.changes, repo.dir, repo.base)
        self.assertIn("40 lowercase hex digits", problems)
        self.assertIn("not npm", problems)

    def test_cargo_names_a_program_for_each_platform_ci_builds(self):
        repo = Repo(self)
        bins = {"windows-x86_64": "target/release/x", "linux-x86_64": "../x", "linux-aarch64": "target/x"}
        repo.add_plugin(text=version_toml(kind="cargo", bins=bins))
        repo.add_plugin(version="1.0.1", text=version_toml(kind="uv-script", bins=BINS))
        repo.commit("add")
        problems = problems_of(plan.changes, repo.dir, repo.base)
        self.assertIn("needs an entry for macos-aarch64", problems)
        self.assertIn("must be an .exe file", problems)
        self.assertIn("can't step into another folder", problems)
        self.assertIn("CI builds only", problems)
        self.assertIn("is for build kind cargo only", problems)

    def test_webassembly_kinds_name_what_they_build(self):
        hashed = 'sha256 = "' + "a" * 64 + '"\n'
        for kind, extra, expected in (
            ("wasm-rust", "", "needs build.artifact"),
            ("wasm-rust", 'artifact = "../x.wasm"\n', "can't step into another folder"),
            ("wasm-rust", "artifact = 7\n", "it must be a path"),
            ("wasm-rust", 'artifact = "target/x.exe"\n', "must be a .wasm file"),
            ("wasm-rust", 'artifact = ".hg/x.wasm"\n', "is in .hg/"),
            ("wasm-rust", RUST_KEYS + 'sha256 = "abc"\n', "64 lowercase hex digits"),
            ("wasm-rust", RUST_KEYS + PYTHON_KEYS, "build.componentize_py is for build kind wasm-python only"),
            ("wasm-rust", RUST_KEYS + 'requirements = "r.txt"\n', "build.requirements is for build kind wasm-python"),
            ("wasm-rust", RUST_KEYS + '[build.bins]\n"linux-x86_64" = "x"\n', "is for build kind cargo only"),
            ("wasm-python", "", "must name a version CI builds with (0.25.1), not None"),
            ("wasm-python", 'componentize_py = "0.24.0"\n', "not 0.24.0"),
            ("wasm-python", 'componentize_py = ["x"]\n', "must name a version CI builds with"),
            # A Python build differs on every run: the index signs what CI built.
            ("wasm-python", PYTHON_KEYS + hashed, "build.sha256 is for build kind wasm-rust only"),
            ("wasm-python", PYTHON_KEYS + RUST_KEYS, "build.artifact is for build kind wasm-rust only"),
            ("wasm-python", PYTHON_KEYS + 'requirements = "../r.txt"\n', "can't step into another folder"),
            ("wasm-python", PYTHON_KEYS + "requirements = 7\n", "it must be a path"),
            ("uv-script", RUST_KEYS, "build.artifact is for build kind wasm-rust only"),
            ("files", PYTHON_KEYS, "build.componentize_py is for build kind wasm-python only"),
        ):
            with self.subTest(kind=kind, extra=extra):
                repo = Repo(self)
                repo.add_plugin(text=version_toml(kind=kind, extra=extra))
                repo.commit("add")
                self.assertIn(expected, problems_of(plan.changes, repo.dir, repo.base))
        repo = Repo(self)
        repo.add_plugin(text=version_toml(kind="wasm-rust", path="plugin", extra=RUST_KEYS + hashed))
        python_file = version_toml(kind="wasm-python", path="crates/x/plugin", extra=PYTHON_KEYS)
        repo.add_plugin(version="1.0.1", text=python_file)
        repo.commit("add")
        rust, python = plan.changes(repo.dir, repo.base)[0]
        self.assertEqual((rust.artifact, rust.sha256, rust.folder, rust.tool()), (ARTIFACT, "a" * 64, ".", ""))
        self.assertEqual(python.folder, "crates/x")
        self.assertEqual(python.tool(), f"componentize-py==0.25.1 --hash=sha256:{plan.COMPONENTIZE_PY['0.25.1']}")

    def test_an_unknown_key_is_refused(self):
        repo = Repo(self)
        repo.add_plugin(text=version_toml(extra='\n[extra]\nrun = "curl x | sh"\n'))
        repo.commit("add")
        self.assertIn("doesn't know", problems_of(plan.changes, repo.dir, repo.base))

    def test_a_new_version_needs_a_plugin_toml(self):
        repo = Repo(self)
        repo.write(f"plugins/{ID}/1.0.0.toml", version_toml())
        repo.commit("add")
        self.assertIn("plugin.toml is missing", problems_of(plan.changes, repo.dir, repo.base))

    def test_plugin_toml_is_checked(self):
        repo = Repo(self)
        repo.add_plugin()
        repo.write(f"plugins/{ID}/plugin.toml", 'author = ""\ngithub = ["-alice"]\nauthorKey = "RWQ"\n')
        repo.commit("add")
        problems = problems_of(plan.changes, repo.dir, repo.base)
        self.assertIn("author must be a name", problems)
        self.assertIn("github must list", problems)
        self.assertIn("authorKey must be", problems)

    def test_site_is_written_by_ci_only(self):
        repo = Repo(self)
        repo.write("site/index.json", "{}\n")
        repo.commit("edit the index")
        self.assertIn("site/ is written by CI only", problems_of(plan.changes, repo.dir, repo.base))

    def test_plugins_holds_only_plugin_and_version_files(self):
        repo = Repo(self)
        repo.add_plugin()
        repo.write(f"plugins/{ID}/README.md", "hi\n")
        repo.write(f"plugins/{ID}/1.0.0/plugin.json", "{}\n")
        repo.commit("add")
        problems = problems_of(plan.changes, repo.dir, repo.base)
        self.assertEqual(problems.count("plugins/ holds only"), 2)

    def test_withdrawn_toml_changes_the_index_and_is_checked(self):
        repo = Repo(self)
        repo.write(
            "withdrawn.toml",
            f'[[withdrawn]]\nid = "{ID}"\nversions = "<=1.0.4"\nreason = "It sent export paths to a server."\n'
            "since = 2026-11-03\n",
        )
        good = repo.commit("withdraw")
        versions, _, index, _ = plan.changes(repo.dir, repo.base, good)
        self.assertEqual((versions, index), ([], True))
        repo.write("withdrawn.toml", '[[withdrawn]]\nid = "x"\nversions = ""\n')
        repo.commit("break it")
        problems = problems_of(plan.changes, repo.dir, good)
        self.assertIn("id must be a plugin id", problems)
        self.assertIn("reason must say why", problems)
        self.assertIn("since must be", problems)

    def test_epochs_holds_only_new_certificates(self):
        repo = Repo(self)
        repo.write("epochs/epoch-2.minisig", "untrusted comment: x\n")
        added = repo.commit("epoch 2")
        _, notes, index, epoch = plan.changes(repo.dir, repo.base)
        self.assertTrue(any("epoch-2.minisig" in n for n in notes))
        self.assertFalse(index)
        self.assertTrue(epoch)
        repo.write("epochs/epoch-2.minisig", "untrusted comment: y\n")
        repo.write("epochs/epoch-1.minisig", "untrusted comment: z\n")
        repo.commit("change it")
        problems = problems_of(plan.changes, repo.dir, added)
        self.assertIn("never changes", problems)
        self.assertIn("n from 2 up", problems)

    def test_a_change_to_catalog_toml_is_checked(self):
        repo = Repo(self)
        repo.write("catalog.toml", 'uv = "0.13.7"\nasp = { rev = "<AutoScene commit>" }\ndays = 40\n')
        repo.commit("change the settings")
        problems = problems_of(plan.changes, repo.dir, repo.base)
        self.assertIn("asp.rev must be the full id", problems)
        self.assertIn("days must be a whole number from 15 to 31", problems)

    def test_a_change_to_catalog_toml_names_the_old_and_new_asp_and_uv(self):
        repo = Repo(self)
        new_rev = "f" * 40
        repo.write("catalog.toml", f'uv = "0.14.0"\nasp = {{ rev = "{new_rev}" }}\ndays = 30\n')
        repo.commit("move asp")
        versions, notes, index, epoch = plan.changes(repo.dir, repo.base)
        self.assertEqual((versions, index, epoch), ([], False, False))
        note = next(n for n in notes if "catalog.toml" in n)
        self.assertIn(f"asp.rev `{new_rev}` (was `{REV}`)", note)
        self.assertIn("uv `0.14.0` (was `0.13.7`)", note)
        self.assertIn("days `30`.", note)
        self.assertIn("AutoScene's `main`", note)

    def test_a_withdrawal_is_never_taken_back_or_narrowed(self):
        entry = '[[withdrawn]]\nid = "{id}"\nversions = "{versions}"\nreason = "{reason}"\nsince = 2026-11-03\n\n'
        malware = entry.format(id=ID, versions="<=1.0.4", reason="It sent export paths to a server.")
        repo = Repo(self)
        repo.write("withdrawn.toml", malware)
        base = repo.commit("withdraw")
        for label, text in (
            ("emptied", ""),
            ("narrowed", malware.replace("<=1.0.4", "=1.0.4")),
            ("another id", malware.replace(ID, "io.github.alice.other")),
        ):
            with self.subTest(label):
                repo.write("withdrawn.toml", text)
                repo.commit(label)
                problems = problems_of(plan.changes, repo.dir, base)
                self.assertIn(f"no longer withdraws {ID} <=1.0.4", problems)
        # A new entry, and a clearer reason for an old one, are fine.
        more = entry.format(id=ID, versions="=2.0.0", reason="Broken.")
        repo.write("withdrawn.toml", malware.replace("It sent", "It quietly sent") + more)
        repo.commit("add one")
        _, _, index, _ = plan.changes(repo.dir, base)
        self.assertTrue(index)

    def test_the_first_push_counts_every_version_as_new(self):
        repo = Repo(self)
        repo.add_plugin()
        repo.add_plugin(version="1.1.0")
        repo.write("site/index.json", "{}\n")
        head = repo.commit("all of it")
        versions, notes, index, _ = plan.changes(repo.dir, plan.ZERO, head)
        self.assertEqual([v.version for v in versions], ["1.0.0", "1.1.0"])
        self.assertTrue(index)
        self.assertIn("first push", notes[0])
        # A base that isn't an ancestor (a push that rewrote main) counts the same.
        repo.git("checkout", "-q", "--orphan", "elsewhere")
        repo.git("rm", "-r", "-q", "--cached", ".")
        other = repo.commit("unrelated")
        repo.git("checkout", "-q", "-f", "main")
        versions, _, _, _ = plan.changes(repo.dir, other, head)
        self.assertEqual([v.version for v in versions], ["1.0.0", "1.1.0"])
        # And so does a base this clone doesn't have.
        versions, _, _, _ = plan.changes(repo.dir, "e" * 40, head)
        self.assertEqual(len(versions), 2)

    def test_publish_leaves_out_versions_the_index_lists_and_sees_new_epochs(self):
        repo = Repo(self)
        repo.add_plugin()
        repo.add_plugin(version="1.1.0")
        repo.write("epochs/epoch-2.minisig", "untrusted comment: x\n")
        head = repo.commit("all of it")
        index = repo.dir / "listed.json"
        index.write_text(json.dumps(index_json(plugins=[(ID, "1.0.0")])), encoding="utf-8")
        listed = plan.listed(plan.read_index(index))
        versions, notes, changed, epoch = plan.changes(repo.dir, plan.ZERO, head, listed)
        self.assertEqual([v.version for v in versions], ["1.1.0"])
        self.assertTrue(changed and epoch)
        self.assertIn(f"The index already lists {ID} 1.0.0: left as it is", "\n".join(notes))
        # Every version listed: nothing to build, and the index doesn't change for them.
        index.write_text(json.dumps(index_json(plugins=[(ID, "1.0.0"), (ID, "1.1.0")])), encoding="utf-8")
        out = repo.dir / "out.txt"
        os.environ["GITHUB_OUTPUT"] = str(out)
        os.environ["GITHUB_STEP_SUMMARY"] = str(repo.dir / "summary.md")
        try:
            argv = ["--root", str(repo.dir), "changes", "--base", plan.ZERO, "--head", head, "--listed", str(index)]
            self.assertEqual(plan.main(argv), 0)
        finally:
            del os.environ["GITHUB_OUTPUT"], os.environ["GITHUB_STEP_SUMMARY"]
        self.assertEqual(
            out.read_text(encoding="utf-8").splitlines(), ["versions=[]", "any=false", "index=false", "epoch=true"]
        )
        index.write_text("{", encoding="utf-8")
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            self.assertEqual(plan.main(argv), 1)
        self.assertIn("listed.json isn't JSON", printed.getvalue())

    def test_publish_plans_every_version_the_index_doesn_t_list(self):
        repo = Repo(self)
        repo.add_plugin()
        repo.add_plugin(version="1.1.0")
        base = repo.commit("two versions")
        repo.write("README.md", "The catalog, and how to submit.\n")
        head = repo.commit("only the README")
        # A pull request's rules see only base..head.
        self.assertEqual(plan.changes(repo.dir, base, head)[0], [])
        # A run that stopped before its index was pushed: the next push plans
        # what the index doesn't list.
        versions, notes, changed, _ = plan.changes(repo.dir, base, head, {(ID, "1.0.0")}, unlisted=True)
        self.assertEqual([v.version for v in versions], ["1.1.0"])
        self.assertTrue(changed)
        self.assertIn(f"The index doesn't list {ID} 1.1.0 yet: planned again", "\n".join(notes))
        # A run by hand has no base: base and head are the same commit.
        out = repo.dir / "out.txt"
        os.environ["GITHUB_OUTPUT"] = str(out)
        os.environ["GITHUB_STEP_SUMMARY"] = str(repo.dir / "summary.md")
        try:
            argv = ["--root", str(repo.dir), "changes", "--base", head, "--head", head, "--unlisted"]
            self.assertEqual(plan.main(argv), 0)
        finally:
            del os.environ["GITHUB_OUTPUT"], os.environ["GITHUB_STEP_SUMMARY"]
        self.assertEqual(out.read_text(encoding="utf-8").splitlines(), [
            f'versions=[{{"id":"{ID}","version":"1.0.0"}},{{"id":"{ID}","version":"1.1.0"}}]',
            "any=true", "index=true", "epoch=false",
        ])
        # An unlisted version file is held to the rules like a new one.
        repo.write(f"plugins/{ID}/1.2.0.toml", version_toml(commit="abc"))
        repo.git("add", "-A")
        repo.git("commit", "-q", "--no-verify", "-m", "a broken file")
        broken = repo.git("rev-parse", "HEAD")
        self.assertIn("commit", problems_of(plan.changes, repo.dir, broken, broken, frozenset(), unlisted=True))

    def test_the_command_line_sets_outputs_and_reports_each_problem(self):
        repo = Repo(self)
        repo.add_plugin()
        repo.commit("add")
        out = repo.dir / "out.txt"
        os.environ["GITHUB_OUTPUT"] = str(out)
        os.environ["GITHUB_STEP_SUMMARY"] = str(repo.dir / "summary.md")
        try:
            self.assertEqual(plan.main(["--root", str(repo.dir), "changes", "--base", repo.base]), 0)
        finally:
            del os.environ["GITHUB_OUTPUT"], os.environ["GITHUB_STEP_SUMMARY"]
        lines = out.read_text(encoding="utf-8").splitlines()
        self.assertIn(f'versions=[{{"id":"{ID}","version":"1.0.0"}}]', lines)
        self.assertIn("any=true", lines)
        self.assertIn("index=true", lines)
        self.assertIn("epoch=false", lines)
        self.assertIn(f"{REPO}/tree/{COMMIT}", (repo.dir / "summary.md").read_text(encoding="utf-8"))

        repo.write(f"plugins/{ID}/1.0.0.toml", version_toml(repo="https://gitlab.com/a/b", commit="x"))
        repo.commit("break it")
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            self.assertEqual(plan.main(["--root", str(repo.dir), "changes", "--base", repo.base]), 1)
        errors = printed.getvalue().splitlines()
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(line.startswith("::error::") for line in errors))

    def test_it_runs_isolated_with_the_standard_library_only(self):
        repo = Repo(self)
        done = subprocess.run(
            [sys.executable, "-I", str(HERE / "plan.py"), "--root", str(repo.dir), "settings"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(done.stdout.splitlines(), ["uv=0.13.7", f"asp_rev={REV}", "days=30"])


class Folders(unittest.TestCase):
    """check-folder (before asp pack), source (before the build) and assemble."""

    def folder(self, files=None, **manifest_fields):
        root = Path(tempfile.mkdtemp(prefix="test-plan-folder-"))
        self.addCleanup(lambda: _remove(root))
        (root / "plugin.json").write_text(json.dumps(manifest(**manifest_fields)), encoding="utf-8")
        for path, data in (files or {}).items():
            file = root / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data if isinstance(data, bytes) else data.encode())
        return root

    def check(self, folder, kind="uv-script", built=(), fetch=None):
        return plan.check_folder(kind, folder, list(built), ID, "1.0.0", REPO, fetch or self.no_download)

    def no_download(self, url, size, seconds):
        raise AssertionError(f"downloaded {url}")

    def test_a_plugin_json_with_another_id_is_refused(self):
        problems = problems_of(self.check, self.folder(id_="io.github.mallory.clip-renamer"))
        self.assertIn(f"its folder here is plugins/{ID}/", problems)

    def test_a_plugin_json_with_another_version_is_refused(self):
        problems = problems_of(self.check, self.folder(version="1.0.1"))
        self.assertIn("its version file is 1.0.0.toml", problems)

    def test_the_repository_must_be_the_source_repo(self):
        problems = problems_of(self.check, self.folder(repo="https://github.com/mallory/clip-renamer"))
        self.assertIn("must name the same repo", problems)

    def test_kinds_whose_launches_dont_fit_are_refused(self):
        program = {"type": "process", "command": {"*": ["bin/linux-x86_64/clip-renamer"]}}
        cases = [
            ("uv-script", program, (), "runs only uv scripts"),
            ("uv-script", {"type": "none"}, (), "Use build kind files"),
            ("cargo", program, BUILT, "which CI doesn't build"),
            ("cargo", cargo_runtime(**{"linux-aarch64": ["bin/linux-aarch64/clip-renamer"]}), BUILT, "doesn't build"),
            ("cargo", {"type": "process", "command": {"*": UV}}, BUILT, "starts none"),
            ("files", {"type": "process", "command": {"*": UV}}, (), 'runtime.type "none"'),
            ("files", WASM, (), 'runtime.type "wasm" needs build kind wasm-rust or wasm-python, not files'),
            ("uv-script", WASM, (), "needs build kind wasm-rust or wasm-python"),
            ("cargo", WASM, BUILT, "needs build kind wasm-rust or wasm-python"),
            ("wasm-rust", {"type": "process", "command": {"*": UV}}, ("plugin.wasm",), "is for a WebAssembly plugin"),
            ("wasm-python", {"type": "none"}, ("plugin.wasm",), "is for a WebAssembly plugin"),
            ("wasm-rust", {"type": "wasm"}, ("plugin.wasm",), "it must be a path"),
            ("wasm-rust", {"type": "wasm", "component": ["x.wasm"]}, ("plugin.wasm",), "it must be a path"),
            ("wasm-rust", {"type": "wasm", "component": "../x.wasm"}, ("plugin.wasm",), "can't step into"),
            ("wasm-python", {"type": "wasm", "component": "plugin.txt"}, ("plugin.txt",), "must be a .wasm file"),
            ("wasm-python", {"type": "wasm", "component": ".wasm"}, (".wasm",), "must be a .wasm file"),
            ("wasm-rust", {"type": "wasm", "component": ".hg/x.wasm"}, ("plugin.wasm",), "leaves that folder out"),
            ("wasm-rust", {"type": "wasm", "component": ".vscode/x.wasm"}, ("plugin.wasm",), "leaves that folder out"),
            ("wasm-rust", {"type": "wasm", "component": "\ud800.wasm"}, ("plugin.wasm",), "use only letters"),
            ("wasm-rust", WASM, ("other.wasm",), "which CI didn't build"),
            ("uv-script", {"type": "process", "command": {"*": ["uv", "run", "main.py"]}}, (), "no command"),
        ]
        for kind, runtime, built, expected in cases:
            with self.subTest(kind=kind, runtime=runtime):
                folder = self.folder(runtime=runtime, files={b: b"#!/bin/sh\n" for b in built})
                self.assertIn(expected, problems_of(self.check, folder, kind, built))

    def test_kinds_that_fit_pass(self):
        services = {"type": "service", "command": {"*": UV}}
        cases = [
            ("uv-script", None, ()),
            ("uv-script", services, ()),
            ("cargo", cargo_runtime(), BUILT),
            ("cargo", {"type": "process", "command": {"linux-x86_64": [BUILT[0]], "*": UV}}, BUILT),
            ("files", {"type": "none"}, ()),
            ("wasm-rust", WASM, ("plugin.wasm",)),
            ("wasm-python", {"type": "wasm", "component": "bin/plugin.wasm"}, ("bin/plugin.wasm",)),
        ]
        for kind, runtime, built in cases:
            with self.subTest(kind=kind, runtime=runtime):
                fields = {} if runtime is None else {"runtime": runtime}
                folder = self.folder(files={b: b"\x7fELF built by CI" for b in built}, **fields)
                self.check(folder, kind, built)

    def test_an_exe_an_elf_file_and_a_pyc_from_the_author_are_refused(self):
        files = {
            "tool.exe": b"MZ\x90\x00",
            "lib/helper": b"\x7fELF\x02\x01",
            "__pycache__/main.cpython-313.pyc": b"\xf3\x0d\x0d\x0a",
            "lib/x.dylib": b"text",
            "mac": b"\xcf\xfa\xed\xfe",
            "fat": b"\xca\xfe\xba\xbe",
            "plugin.wasm": b"\0asm\x01\0\0\0",
            "native.node": b"",
            "wheels/native-1.0-cp313-cp313-linux_x86_64.whl": b"PK\x03\x04",
            "data.bin": b"PK\x03\x04 a zip under another name",
            "setup.dat": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
            "blob": b"\x1f\x8b\x08\x00",
            "README.md": b"Just text.\n",
            ".git/objects/x": b"\x7fELF in version control, which asp leaves out",
        }
        problems = problems_of(self.check, self.folder(files=files))
        for name in ("tool.exe", "lib/helper", "main.cpython-313.pyc", "lib/x.dylib", "mac", "fat", "plugin.wasm",
                     "native.node", "linux_x86_64.whl", "data.bin", "setup.dat", "blob"):
            self.assertIn(name, problems)
        self.assertNotIn("README.md", problems)
        self.assertNotIn(".git", problems)

    def test_a_component_ci_didnt_build_is_refused(self):
        files = {"plugin.wasm": b"\0asm built by CI", "lib/other.wasm": b"\0asm from the author"}
        problems = problems_of(self.check, self.folder(runtime=WASM, files=files), "wasm-rust", ["plugin.wasm"])
        self.assertIn("lib/other.wasm is WebAssembly", problems)
        self.assertNotIn("plugin.wasm is WebAssembly", problems)

    def test_the_summary_says_a_component_runs_in_a_sandbox(self):
        built = b"\0asm built"
        folder = self.folder(
            runtime=WASM, files={"plugin.wasm": built}, permissions=["network", "exports:*"],
            network={"hosts": ["discord.com"]},
        )
        text = self.check(folder, "wasm-python", ["plugin.wasm"])
        self.assertIn("### Runs in a sandbox", text)
        self.assertIn("- Permissions: network, exports:\\*\n", text)
        self.assertIn("- Hosts: discord.com\n", text)
        self.assertIn(f"| plugin.wasm | 10 | `{hashlib.sha256(built).hexdigest()}` |", text)
        self.assertNotIn("sandbox", self.check(self.folder()))
        self.assertIn("- Permissions: none\n- Hosts: none\n", self.check(
            self.folder(runtime=WASM, files={"plugin.wasm": b"\0asm"}, permissions="all", network=["x"]),
            "wasm-rust", ["plugin.wasm"],
        ))

    def test_a_file_in_bin_that_ci_didnt_build_is_refused(self):
        files = {b: b"MZ" for b in BUILT}
        files["bin/notes.txt"] = b"text"
        folder = self.folder(runtime=cargo_runtime(), files=files)
        self.assertIn("bin/notes.txt is in bin/", problems_of(self.check, folder, "cargo", BUILT))

    @unittest.skipIf(os.name == "nt", "links need a privilege on Windows")
    def test_links_are_refused(self):
        folder = self.folder()
        (folder / "secrets").symlink_to("/etc")
        (folder / "passwd").symlink_to("/etc/passwd")
        problems = problems_of(self.check, folder)
        self.assertIn("secrets is a link", problems)
        self.assertIn("passwd is a link", problems)

    def test_the_summary_lists_what_ci_built(self):
        files = {b: b"MZ built" for b in BUILT}
        text = self.check(self.folder(runtime=cargo_runtime(), files=files), "cargo", BUILT)
        sha = hashlib.sha256(b"MZ built").hexdigest()
        self.assertIn(f"| bin/linux-x86\\_64/clip-renamer | 8 | `{sha}` |", text)
        self.assertIn("None: the package holds the author's folder", self.check(self.folder()))

    def source_repo(self, kind="cargo", toolchain='[toolchain]\nchannel = "nightly-2026-10-01"\n', path=".",
                    files=None, lock=LOCK, bins=None, extra="", **manifest_fields):
        root = Repo(self)
        root.add_plugin(text=version_toml(kind=kind, path=path, bins=bins, extra=extra))
        root.commit("add")
        checkout = Path(tempfile.mkdtemp(prefix="test-plan-src-"))
        self.addCleanup(lambda: _remove(checkout))
        folder = checkout / path
        folder.mkdir(parents=True, exist_ok=True)
        if kind == "cargo" and "runtime" not in manifest_fields:
            programs = {p: [f"bin/{p}/{Path(b).name}"] for p, b in BINS.items()}
            manifest_fields["runtime"] = {"type": "process", "command": {"*": UV, **programs}}
        (folder / "plugin.json").write_text(json.dumps(manifest(**manifest_fields)), encoding="utf-8")
        if toolchain is not None:
            (checkout / "rust-toolchain.toml").write_text(toolchain, encoding="utf-8")
        if lock is not None:
            (folder / "main.py.lock").write_text(lock, encoding="utf-8")
        for name, data in (files or {}).items():
            (checkout / name).parent.mkdir(parents=True, exist_ok=True)
            (checkout / name).write_bytes(data)
        return root.dir, checkout

    def rust_repo(self, files=None, drop=(), extra=RUST_KEYS, path="plugin", **kwargs):
        """A repo laid out as `asp init --wasm rust` lays it out."""
        given = {"Cargo.toml": b"[package]\n", "Cargo.lock": b"version = 4\n", **(files or {})}
        given = {k: v for k, v in given.items() if k not in drop}
        return self.source_repo(kind="wasm-rust", path=path, lock=None, runtime=WASM, extra=extra, files=given,
                                **kwargs)

    def python_repo(self, files=None, drop=(), extra=PYTHON_KEYS, path="plugin", **kwargs):
        """A repo laid out as `asp init --wasm python` lays it out."""
        given = {"app.py": b"", "wit/plugin.wit": b"", **(files or {})}
        given = {k: v for k, v in given.items() if k not in drop}
        return self.source_repo(kind="wasm-python", path=path, toolchain=None, lock=None, runtime=WASM, extra=extra,
                                files=given, **kwargs)

    def test_source_takes_a_webassembly_plugin_with_or_without_a_committed_component(self):
        committed = {"plugin/plugin.wasm": b"\0asm from the author"}
        for repo in (self.rust_repo, self.python_repo):
            for files in (None, committed):
                with self.subTest(repo=repo.__name__, files=files):
                    root, checkout = repo(files)
                    self.assertEqual(plan.check_source(root, ID, "1.0.0", checkout), ([], []))
        # The folder above the plugin folder is the top of the repo here.
        extra = 'artifact = "crates/x/target/wasm32-wasip2/release/x.wasm"\n'
        given = {"crates/x/Cargo.toml": b"[package]\n"}
        root, checkout = self.rust_repo(given, drop=("Cargo.toml",), extra=extra, path="crates/x/plugin")
        plan.check_source(root, ID, "1.0.0", checkout)  # Cargo.lock from the workspace above

    def test_source_refuses_what_a_webassembly_build_can_t_use(self):
        check = plan.check_source
        root, checkout = self.rust_repo(drop=("Cargo.toml",))
        self.assertIn("./Cargo.toml is missing", problems_of(check, root, ID, "1.0.0", checkout))
        root, checkout = self.rust_repo(drop=("Cargo.lock",))
        self.assertIn("The repo has no Cargo.lock", problems_of(check, root, ID, "1.0.0", checkout))
        root, checkout = self.rust_repo(toolchain='[toolchain]\nchannel = "stable"\n')
        self.assertIn("channel is stable", problems_of(check, root, ID, "1.0.0", checkout))
        root, checkout = self.rust_repo(toolchain=None)
        self.assertIn("no rust-toolchain.toml", problems_of(check, root, ID, "1.0.0", checkout))
        # A file the repo holds can't pass as what cargo built.
        root, checkout = self.rust_repo({ARTIFACT: b"\0asm"})
        self.assertIn(f"build.artifact names {ARTIFACT}, which the repo holds",
                      problems_of(check, root, ID, "1.0.0", checkout))
        for drop in ("app.py", "wit/plugin.wit"):
            root, checkout = self.python_repo(drop=(drop,))
            self.assertIn("needs app.py and wit/", problems_of(check, root, ID, "1.0.0", checkout))
        # A component elsewhere, and a folder or a file in its way.
        root, checkout = self.rust_repo({"plugin/lib/other.wasm": b"\0asm"})
        self.assertIn("lib/other.wasm is WebAssembly", problems_of(check, root, ID, "1.0.0", checkout))
        root, checkout = self.rust_repo({"plugin/plugin.wasm/x": b"text"})
        self.assertIn("is a folder in the repo", problems_of(check, root, ID, "1.0.0", checkout))
        nested = {"type": "wasm", "component": "lib/plugin.wasm"}
        root, checkout = self.source_repo(
            kind="wasm-rust", path="plugin", lock=None, runtime=nested, extra=RUST_KEYS,
            files={"Cargo.toml": b"", "Cargo.lock": b"", "plugin/lib": b"text"},
        )
        self.assertIn("goes through lib, which isn't a folder", problems_of(check, root, ID, "1.0.0", checkout))
        root, checkout = self.source_repo(kind="wasm-python", path="plugin", toolchain=None, lock=None,
                                          extra=PYTHON_KEYS, runtime={"type": "process", "command": {"*": UV}})
        self.assertIn("is for a WebAssembly plugin", problems_of(check, root, ID, "1.0.0", checkout))

    def test_requirements_are_hash_locked_pins_from_the_package_index(self):
        def source(data):
            extra = PYTHON_KEYS + 'requirements = "requirements.txt"\n'
            root, checkout = self.python_repo({"requirements.txt": data}, extra=extra)
            return lambda: plan.check_source(root, ID, "1.0.0", checkout)

        hashed = f" --hash=sha256:{'c' * 64}"
        for good in (
            PIN,
            PIN.replace("\n", "\r\n"),
            "# autogenerated\n\n" + PIN + f"a-b.c[x,y]==2.0.post1{hashed}\n",
            f"tomli==2.0 ; python_full_version < '3.11' and sys_platform == \"linux\"{hashed}\n",
            f"idna==3.11{hashed} # why\n",
        ):
            with self.subTest(good):
                self.assertEqual(source(good.encode())(), ([], []))
        for bad, expected in (
            ("idna==3.11\n", "holds idna==3.11."),
            (f"idna>=3.11{hashed}\n", "holds idna>=3.11"),
            (f"idna==3.*{hashed}\n", "holds idna==3.*"),
            (f"idna{hashed}\n", "holds idna --hash"),
            (f"idna==3.11 --hash=md5:{'c' * 32}\n", "--hash=md5"),
            (f"--index-url https://mallory.example/simple\n{PIN}", "holds --index-url"),
            (f"--extra-index-url https://mallory.example/simple\n{PIN}", "holds --extra-index-url"),
            (f"--find-links wheels\n{PIN}", "holds --find-links"),
            (f"--no-index\n{PIN}", "holds --no-index"),
            (f"-r other.txt\n{PIN}", "holds -r other.txt"),
            (f"-c constraints.txt\n{PIN}", "holds -c constraints.txt"),
            (f"-e .\n{PIN}", "holds -e ."),
            (f"idna @ https://mallory.example/idna.whl{hashed}\n", "holds idna @"),
            (f"idna @ file:///src/idna.whl{hashed}\n", "holds idna @"),
            (f"./wheels/idna.whl{hashed}\n", "holds ./wheels/idna.whl"),
            (f"https://mallory.example/idna.whl{hashed}\n", "holds https://mallory"),
            (f"idna==${{VERSION}}{hashed}\n", "holds idna==$"),
            (f"idna==3.11 ; --index-url https://mallory.example{hashed}\n", "holds idna==3.11 ;"),
            # uv and pip could read the next line as an option.
            (f"# a note \\\n--index-url https://mallory.example/simple\n{PIN}", "a comment that ends in a backslash"),
            (f"idna==3.11{hashed} # a note \\\n--no-index\n", "a comment that ends in a backslash"),
            ("idna==3.11 \\", "ends in the middle of a line"),
            # uv ends a comment at a bare \r, and the rest would be an option.
            (f"# a note\r--index-url https://mallory.example/simple\n{PIN}", "isn't plain ASCII text"),
            (f"{PIN}# a note\r-r other.txt\n", "isn't plain ASCII text"),
            (f"idna==3.11{hashed}\x0b--index-url x\n", "isn't plain ASCII text"),
            (f"idna==3.11{hashed}\x0c--no-index\n", "isn't plain ASCII text"),
            ("\ufeff" + PIN, "isn't plain ASCII text"),
            # A line this long costs no backtracking.
            ("idna==1;" + " " * 200_000 + "x\n", "holds idna==1;"),
        ):
            with self.subTest(bad):
                self.assertIn(expected, problems_of(source(bad.encode())))
        self.assertIn("isn't UTF-8", problems_of(source(b"\xff\n")))
        self.assertIn("larger than 4 MiB", problems_of(source(b"#" * ((4 << 20) + 1))))
        extra = PYTHON_KEYS + 'requirements = "missing.txt"\n'
        root, checkout = self.python_repo(extra=extra)
        self.assertIn("names missing.txt, which the repo doesn't hold",
                      problems_of(plan.check_source, root, ID, "1.0.0", checkout))

    @unittest.skipIf(os.name == "nt", "links need a privilege on Windows")
    def test_source_refuses_links_on_a_webassembly_plugin_s_way(self):
        check = plan.check_source
        root, checkout = self.rust_repo()
        (checkout / "plugin/plugin.wasm").symlink_to("/etc/passwd")
        self.assertIn("plugin.wasm is a link", problems_of(check, root, ID, "1.0.0", checkout))
        root, checkout = self.rust_repo()
        (checkout / "target").symlink_to("prebuilt")
        self.assertIn("goes through a link", problems_of(check, root, ID, "1.0.0", checkout))
        extra = PYTHON_KEYS + 'requirements = "requirements.txt"\n'
        root, checkout = self.python_repo({"pins.txt": PIN.encode()}, extra=extra)
        (checkout / "requirements.txt").symlink_to("pins.txt")
        self.assertIn("goes through a link", problems_of(check, root, ID, "1.0.0", checkout))

    def test_source_checks_the_toolchain_and_lists_the_uv_scripts(self):
        root, checkout = self.source_repo(path="plugin")
        self.assertEqual(plan.check_source(root, ID, "1.0.0", checkout), (["main.py"], []))
        root, checkout = self.source_repo(kind="files", toolchain=None, lock=None, runtime={"type": "none"})
        self.assertEqual(plan.check_source(root, ID, "1.0.0", checkout), ([], []))
        for toolchain, expected in (
            ('[toolchain]\nchannel = "stable"\n', "channel is stable"),
            ('[toolchain]\nchannel = "nightly"\n', "channel is nightly"),
            (None, "no rust-toolchain.toml"),
        ):
            with self.subTest(toolchain):
                root, checkout = self.source_repo(toolchain=toolchain)
                self.assertIn(expected, problems_of(plan.check_source, root, ID, "1.0.0", checkout))
        root, checkout = self.source_repo(toolchain='[toolchain]\nchannel = "1.91.0"\n')
        plan.check_source(root, ID, "1.0.0", checkout)

    def test_source_refuses_a_bin_folder_and_binaries_before_anything_is_built(self):
        root, checkout = self.source_repo(files={"bin/linux-x86_64/clip-renamer": b"#!/bin/sh\n", "x.so": b""})
        problems = problems_of(plan.check_source, root, ID, "1.0.0", checkout)
        self.assertIn("The plugin folder has bin/", problems)
        self.assertIn("x.so is a .so file", problems)

    def test_cargo_bins_must_be_built_not_committed(self):
        # A program the author committed, outside the plugin folder or in a
        # folder that the checks and asp pack leave out, can't pass as built.
        prebuilt = {p: "prebuilt/" + Path(b).name for p, b in BINS.items()}
        files = {"prebuilt/clip-renamer.exe": b"MZ", "prebuilt/clip-renamer": b"\x7fELF"}
        root, checkout = self.source_repo(path="plugin", bins=prebuilt, files=files)
        problems = problems_of(plan.check_source, root, ID, "1.0.0", checkout)
        self.assertIn("build.bins names prebuilt/clip-renamer, which the repo holds", problems)
        self.assertIn("build.bins names prebuilt/clip-renamer.exe, which the repo holds", problems)
        # Where cargo builds is fine while nothing is committed there.
        root, checkout = self.source_repo(path="plugin")
        plan.check_source(root, ID, "1.0.0", checkout)
        root, checkout = self.source_repo(path="plugin", files={"target/release/clip-renamer": b"\x7fELF"})
        self.assertIn("which the repo holds", problems_of(plan.check_source, root, ID, "1.0.0", checkout))

        repo = Repo(self)
        hidden = {p: ".hg/" + Path(b).name for p, b in BINS.items()}
        repo.add_plugin(text=version_toml(kind="cargo", bins=hidden))
        nested = {p: "target/.SVN/" + Path(b).name for p, b in BINS.items()}
        repo.add_plugin(version="1.0.1", text=version_toml(kind="cargo", bins=nested))
        repo.commit("add")
        problems = problems_of(plan.changes, repo.dir, repo.base)
        self.assertIn("is in .hg/, which the checks and asp pack leave out", problems)
        self.assertIn("is in .SVN/", problems)

    @unittest.skipIf(os.name == "nt", "links need a privilege on Windows")
    def test_cargo_bins_through_a_link_are_refused(self):
        root, checkout = self.source_repo()
        (checkout / "target").symlink_to("prebuilt")
        self.assertIn("goes through a link", problems_of(plan.check_source, root, ID, "1.0.0", checkout))

    def test_uv_locks_take_packages_from_an_index_or_git_only(self):
        for source in (
            '{ path = "wheels/native-1.0-cp313-cp313-linux_x86_64.whl" }',
            '{ directory = "vendor/native" }',
            '{ editable = "vendor/native" }',
            '{ url = "https://alice.example/native-1.0-py3-none-any.whl" }',
        ):
            with self.subTest(source):
                lock = LOCK + f'\n[[package]]\nname = "native"\nversion = "1.0"\nsource = {source}\n'
                root, checkout = self.source_repo(kind="uv-script", toolchain=None, lock=lock)
                problems = problems_of(plan.check_source, root, ID, "1.0.0", checkout)
                self.assertIn("main.py.lock installs native from", problems)
        git = 'source = { git = "https://github.com/a/t?rev=x#0123456789abcdef0123456789abcdef01234567" }'
        lock = LOCK + f'\n[[package]]\nname = "tool"\nversion = "1.0"\n{git}\n'
        root, checkout = self.source_repo(kind="uv-script", toolchain=None, lock=lock)
        self.assertEqual(plan.check_source(root, ID, "1.0.0", checkout), (["main.py"], []))
        other = LOCK.replace("https://pypi.org/simple", "https://download.pytorch.org/whl/cu128")
        root, checkout = self.source_repo(kind="uv-script", toolchain=None, lock=other)
        _, notes = plan.check_source(root, ID, "1.0.0", checkout)
        self.assertIn("download.pytorch.org/whl/cu128", notes[0])
        root, checkout = self.source_repo(kind="uv-script", toolchain=None, lock=None)
        self.assertIn("main.py.lock is missing", problems_of(plan.check_source, root, ID, "1.0.0", checkout))

    def test_assemble_adds_what_ci_built_to_the_author_s_folder(self):
        root, checkout = self.source_repo(path="plugin", files={"plugin/.git/HEAD": b"ref", "plugin/main.py": b"x"})
        raw = Path(tempfile.mkdtemp(prefix="test-plan-raw-"))
        self.addCleanup(lambda: _remove(raw))
        digests = {}
        for platform, binary in BINS.items():
            made = raw / f"raw-{ID}-1.0.0-{platform}" / Path(binary).name
            made.parent.mkdir(parents=True)
            made.write_bytes(f"built for {platform}".encode())
            digests[platform] = hashlib.sha256(f"built for {platform}".encode()).hexdigest()
        folder = raw.parent / (raw.name + "-pkg")
        self.addCleanup(lambda: _remove(folder))
        # A raw output that isn't what its own build job made, or one whose
        # job gave no SHA-256, is refused.
        swapped = dict(digests, **{"macos-aarch64": "0" * 64})
        problems = problems_of(plan.assemble, root, ID, "1.0.0", checkout, raw, folder, swapped)
        self.assertIn("downloaded for macos-aarch64 isn't what its build made", problems)
        _remove(folder)
        problems = problems_of(plan.assemble, root, ID, "1.0.0", checkout, raw, folder, {"linux-x86_64": ""})
        self.assertEqual(problems.count("gave no SHA-256"), 3)
        _remove(folder)
        built = plan.assemble(root, ID, "1.0.0", checkout, raw, folder, digests)
        self.assertEqual(built, BUILT)
        self.assertEqual((folder / "bin/macos-aarch64/clip-renamer").read_bytes(), b"built for macos-aarch64")
        self.assertTrue((folder / "main.py").is_file())
        self.assertFalse((folder / ".git").exists())
        plan.check_folder("cargo", folder, built, ID, "1.0.0", REPO)

    def raw_component(self, data=b"\0asm built by CI"):
        """The raw output of a WebAssembly build, and its --digests."""
        raw = Path(tempfile.mkdtemp(prefix="test-plan-raw-"))
        self.addCleanup(lambda: _remove(raw))
        made = raw / f"raw-{ID}-1.0.0-any" / "plugin.wasm"
        made.parent.mkdir()
        made.write_bytes(data)
        return raw, {"any": hashlib.sha256(data).hexdigest()}

    def test_assemble_puts_ci_s_component_over_the_author_s(self):
        for repo, kind in ((self.rust_repo, "wasm-rust"), (self.python_repo, "wasm-python")):
            with self.subTest(kind):
                root, checkout = repo({"plugin/plugin.wasm": b"\0asm from the author", "plugin/notes.txt": b"text"})
                raw, digests = self.raw_component()
                folder = raw / "pkg"
                self.assertEqual(plan.assemble(root, ID, "1.0.0", checkout, raw, folder, digests), ["plugin.wasm"])
                self.assertEqual((folder / "plugin.wasm").read_bytes(), b"\0asm built by CI")
                self.assertEqual((folder / "notes.txt").read_bytes(), b"text")
                if os.name != "nt":
                    self.assertEqual((folder / "plugin.wasm").stat().st_mode & 0o777, 0o644)
                self.assertFalse((folder / "app.py").exists())
                plan.check_folder(kind, folder, ["plugin.wasm"], ID, "1.0.0", REPO)
        # A folder the manifest names that the author didn't commit is made.
        nested = {"type": "wasm", "component": "lib/x/plugin.wasm"}
        root, checkout = self.source_repo(kind="wasm-python", path="plugin", toolchain=None, lock=None,
                                          extra=PYTHON_KEYS, runtime=nested)
        raw, digests = self.raw_component()
        built = plan.assemble(root, ID, "1.0.0", checkout, raw, raw / "pkg", digests)
        self.assertEqual(built, ["lib/x/plugin.wasm"])
        self.assertTrue((raw / "pkg/lib/x/plugin.wasm").is_file())

    def test_assemble_takes_only_what_the_build_made(self):
        root, checkout = self.rust_repo()
        raw, digests = self.raw_component()
        for given, expected in (
            ({"any": "0" * 64}, "isn't what its build made"),
            ({"any": ""}, "gave no SHA-256"),
            ({}, "gave no SHA-256"),
            ({"linux-x86_64": digests["any"]}, "gave no SHA-256"),
        ):
            with self.subTest(given):
                problems = problems_of(plan.assemble, root, ID, "1.0.0", checkout, raw, raw / "pkg", given)
                self.assertIn(expected, problems)
                _remove(raw / "pkg")
        problems = problems_of(plan.assemble, root, ID, "1.0.0", checkout, raw / "none", raw / "pkg", digests)
        self.assertIn("The build left no plugin.wasm", problems)
        _remove(raw / "pkg")
        # build.sha256 is the author's: a build that differs fails.
        data = b"\0asm built by CI"
        extra = RUST_KEYS + 'sha256 = "' + "0" * 64 + '"\n'
        root, checkout = self.rust_repo(extra=extra)
        problems = problems_of(plan.assemble, root, ID, "1.0.0", checkout, raw, raw / "pkg", digests)
        self.assertIn(f"CI built a component with the SHA-256 {hashlib.sha256(data).hexdigest()}", problems)
        _remove(raw / "pkg")
        extra = RUST_KEYS + f'sha256 = "{hashlib.sha256(data).hexdigest()}"\n'
        root, checkout = self.rust_repo(extra=extra)
        self.assertEqual(plan.assemble(root, ID, "1.0.0", checkout, raw, raw / "pkg", digests), ["plugin.wasm"])

    def test_assemble_refuses_a_component_path_it_can_t_trust(self):
        # The manifest is read again from the copy: pack doesn't lean on read.
        for component, expected in (
            ("../plan.py", "can't step into another folder"),
            ("../../plan.wasm", "can't step into another folder"),
            ("plugin.json", "must be a .wasm file"),
            (["x.wasm"], "it must be a path"),
            (None, "it must be a path"),
        ):
            with self.subTest(component):
                root, checkout = self.rust_repo()
                runtime = {"type": "wasm", "component": component}
                (checkout / "plugin/plugin.json").write_text(json.dumps(manifest(runtime=runtime)), encoding="utf-8")
                raw, digests = self.raw_component()
                problems = problems_of(plan.assemble, root, ID, "1.0.0", checkout, raw, raw / "pkg", digests)
                self.assertIn(expected, problems)
        root, checkout = self.rust_repo({"plugin/plugin.wasm/x": b"text"})
        raw, digests = self.raw_component()
        self.assertIn("is a folder in the repo",
                      problems_of(plan.assemble, root, ID, "1.0.0", checkout, raw, raw / "pkg", digests))

    @unittest.skipIf(os.name == "nt", "links need a privilege on Windows")
    def test_assemble_never_writes_through_a_link_of_the_author_s(self):
        victim = Path(tempfile.mkdtemp(prefix="test-plan-victim-"))
        self.addCleanup(lambda: _remove(victim))
        (victim / "plan.py").write_text("the catalog's own", encoding="utf-8")
        # The component's own path: the link is replaced, what it names stays.
        root, checkout = self.rust_repo()
        (checkout / "plugin/plugin.wasm").symlink_to(victim / "plan.py")
        raw, digests = self.raw_component()
        folder = raw / "pkg"
        self.assertEqual(plan.assemble(root, ID, "1.0.0", checkout, raw, folder, digests), ["plugin.wasm"])
        self.assertFalse((folder / "plugin.wasm").is_symlink())
        self.assertEqual((folder / "plugin.wasm").read_bytes(), b"\0asm built by CI")
        self.assertEqual((victim / "plan.py").read_text(encoding="utf-8"), "the catalog's own")
        # A link on the way there: refused, and nothing is written.
        nested = {"type": "wasm", "component": "lib/plugin.wasm"}
        root, checkout = self.source_repo(kind="wasm-rust", path="plugin", lock=None, runtime=nested, extra=RUST_KEYS,
                                          files={"Cargo.toml": b"", "Cargo.lock": b""})
        (checkout / "plugin/lib").symlink_to(victim)
        raw, digests = self.raw_component()
        problems = problems_of(plan.assemble, root, ID, "1.0.0", checkout, raw, raw / "pkg", digests)
        self.assertIn("goes through lib, which isn't a folder", problems)
        self.assertEqual(sorted(p.name for p in victim.iterdir()), ["plan.py"])
        # A linked plugin.json is no manifest.
        root, checkout = self.rust_repo()
        (victim / "plugin.json").write_text(json.dumps(manifest(runtime=WASM)), encoding="utf-8")
        (checkout / "plugin/plugin.json").unlink()
        (checkout / "plugin/plugin.json").symlink_to(victim / "plugin.json")
        raw, digests = self.raw_component()
        self.assertIn("has no plugin.json",
                      problems_of(plan.assemble, root, ID, "1.0.0", checkout, raw, raw / "pkg", digests))

    def test_assemble_refuses_the_author_s_bin_and_a_missing_build(self):
        root, checkout = self.source_repo(files={"bin/README": b"x"})
        raw = Path(tempfile.mkdtemp(prefix="test-plan-raw-"))
        self.addCleanup(lambda: _remove(raw))
        folder = raw / "pkg"
        self.assertIn("has bin/", problems_of(plan.assemble, root, ID, "1.0.0", checkout, raw / "none", folder, {}))
        root, checkout = self.source_repo()
        problems = problems_of(plan.assemble, root, ID, "1.0.0", checkout, raw / "none", raw / "pkg2", {})
        self.assertEqual(problems.count("left no"), 3)

    # Resources (design-i91 5.6): fetched, hashed and checked before review.

    def served(self, files, asked=None):
        """A fetch that serves `files` ({url: bytes}) and a 404 for the rest."""
        def fetch(url, size, seconds):
            self.assertTrue(1 <= seconds <= plan.DOWNLOAD_SECONDS, seconds)
            if asked is not None:
                asked.append(url)
            if url not in files:
                raise plan.Refused(["curl: (22) The requested URL returned error: 404"])
            return plan.read_stream(io.BytesIO(files[url]), size)
        return fetch

    def test_a_resource_with_its_size_and_hash_passes(self):
        mirror = "https://github.com/alice/clip-renamer/releases/download/v1.0.0/model.onnx"
        folder = self.folder(resources=[resource("model.onnx", ONNX, more_urls=[mirror])])
        asked = []
        text = self.check(folder, fetch=self.served({hf_url("model.onnx"): ONNX}, asked))
        self.assertEqual(asked, [hf_url("model.onnx")])  # the first URL only
        self.assertIn(f"| model.onnx | {len(ONNX)} | ONNX | github.com, huggingface.co |", text)
        self.assertIn("None of the author's files starts like a pickle", text)
        self.assertNotIn("A pickle runs code", text)

    def test_the_command_line_downloads_resources_with_curl(self):
        # check-folder as the pack job runs it: run() and curl_fetch, with a
        # stand-in for curl.
        folder = self.folder(resources=[resource("model.onnx", ONNX)])
        built = folder.parent / f"{folder.name}-built.txt"
        built.write_text("", encoding="utf-8")
        self.addCleanup(built.unlink)
        argv = ["check-folder", "uv-script", str(folder), str(built), "--id", ID, "--version", "1.0.0", "--repo", REPO]
        calls, out = [], io.StringIO()
        with curl_child(ONNX, calls=calls), contextlib.redirect_stdout(out):
            plan.run(argv)
        self.assertEqual([c[:2] for c in calls], [(hf_url("model.onnx"), len(ONNX))])
        self.assertTrue(plan.DOWNLOAD_SECONDS - 60 <= calls[0][2] <= plan.DOWNLOAD_SECONDS, calls)
        self.assertIn(f"| model.onnx | {len(ONNX)} | ONNX | huggingface.co |", out.getvalue())
        out = io.StringIO()
        with curl_child(ONNX[:-1] + b"!"), contextlib.redirect_stdout(out):
            self.assertEqual(plan.main(argv), 1)
        self.assertIn(f"::error::The resource model.onnx from {hf_url('model.onnx')} has the SHA-256", out.getvalue())

    def test_downloads_stop_when_their_time_is_up(self):
        folder = self.folder(resources=[resource("a.onnx", ONNX), resource("b.onnx", ONNX)])
        files = {hf_url("a.onnx"): ONNX, hf_url("b.onnx"): ONNX}
        clock = iter([0.0, 0.0, plan.DOWNLOAD_SECONDS - 0.5])
        asked = []
        with unittest.mock.patch.object(plan, "time", types.SimpleNamespace(monotonic=lambda: next(clock))):
            problems = problems_of(self.check, folder, fetch=self.served(files, asked))
        self.assertEqual(asked, [hf_url("a.onnx")])
        self.assertIn(f"The resource b.onnx from {hf_url('b.onnx')} wasn't downloaded: the downloads before it "
                      "took the 20 minutes", problems)

    def test_a_resource_unlike_its_pin_is_refused(self):
        url = hf_url("model.onnx")
        for served, expected in (
            (ONNX[:-1] + b"!", "has the SHA-256"),
            (ONNX[:-1], f"is {len(ONNX) - 1} bytes, but plugin.json gives {len(ONNX)}"),
            (ONNX + b"!", f"is larger than the {len(ONNX)} bytes plugin.json gives"),
            (None, "couldn't be downloaded: curl: (22) The requested URL returned error: 404"),
        ):
            with self.subTest(expected):
                folder = self.folder(resources=[resource("model.onnx", ONNX)])
                files = {} if served is None else {url: served}
                problems = problems_of(self.check, folder, fetch=self.served(files))
                self.assertIn(f"The resource model.onnx from {url}", problems)
                self.assertIn(expected, problems)

    def test_an_elf_file_is_refused_as_a_resource_as_in_the_folder(self):
        elf = b"\x7fELF\x02\x01\x01\0" + b"\0" * 40
        for name, data, expected in (
            ("model.bin", elf, "is a Linux program or library (ELF)"),
            ("native.so", b"plain data", "is a .so file"),
            ("model.pt", b"PK\x03\x04 torch.save writes zips", "is a zip archive"),
            ("model.gz", b"\x1f\x8b\x08\0", "is a .gz file"),
            ("weights.bin", TAR, "is a tar archive"),
            ("weights.bin", b"BZh91AY&SY\x00\x01", "is a bzip2 archive"),
            ("weights.bin", b"\x04\x22\x4d\x18\x64\x40", "is an lz4 archive"),
            ("weights.bin", b"\x5d\x00\x00\x80\x00\xff\xff", "is an lzma archive"),
            ("weights.bin", b"x\x9c\x6b\x60\x99\x2a", "is zlib-compressed data"),
        ):
            with self.subTest(name=name, data=data[:12]):
                folder = self.folder(resources=[resource(name, data)])
                problems = problems_of(self.check, folder, fetch=self.served({hf_url(name): data}))
                self.assertIn(f"The resource {name} {expected}", problems)
        problems = problems_of(self.check, self.folder(files={"weights/model.bin": elf}))
        self.assertIn("weights/model.bin is a Linux program or library (ELF)", problems)
        problems = problems_of(self.check, self.folder(files={"weights/model.bin": TAR}))
        self.assertIn("weights/model.bin is a tar archive", problems)
        # A problem in the folder stops the check before any download.
        asked = []
        folder = self.folder(files={"weights/model.bin": elf}, resources=[resource("model.onnx", ONNX)])
        problems = problems_of(self.check, folder, fetch=self.served({hf_url("model.onnx"): ONNX}, asked))
        self.assertIn("weights/model.bin is a Linux program or library (ELF)", problems)
        self.assertEqual(asked, [])
        with unittest.mock.patch.object(plan, "MAX_PACKAGE_BYTES", 1 << 10):
            folder = self.folder(files={"table.csv": b"x" * 2000}, resources=[resource("model.onnx", ONNX)])
            problems = problems_of(self.check, folder, fetch=self.served({hf_url("model.onnx"): ONNX}, asked))
            self.assertIn("Move large files to resources", problems)
            self.assertEqual(asked, [])

    def test_a_hugging_face_branch_is_refused_before_anything_is_downloaded(self):
        for url in (
            "https://huggingface.co/alice/weights/resolve/main/model.onnx",
            "https://huggingface.co/alice/weights/resolve/v1.0/model.onnx",
            "https://huggingface.co/alice/weights/resolve/refs%2Fpr%2F1/model.onnx",
            f"https://huggingface.co/alice/weights/blob/{COMMIT}/model.onnx",
            f"https://huggingface.co/alice/weights/resolve/{COMMIT}/",
            f"https://huggingface.co/resolve/{COMMIT}/model.onnx",
            f"https://HF.co/alice/weights/resolve/main/resolve/{COMMIT}/model.onnx",
            "https://www.huggingface.co/alice/weights/resolve/main/model.onnx?download=true",
        ):
            with self.subTest(url):
                asked = []
                folder = self.folder(resources=[resource("model.onnx", ONNX, urls=[url])])
                problems = problems_of(self.check, folder, fetch=self.served({url: ONNX}, asked))
                self.assertIn("follows a branch of Hugging Face", problems)
                self.assertEqual(asked, [])
        # curl and AutoScene ask for /alice/weights/resolve/main/model.onnx: a branch.
        for url in (
            f"https://huggingface.co/alice/weights/resolve/{COMMIT}/../main/model.onnx",
            f"https://huggingface.co/alice/weights/resolve/{COMMIT}/%2e%2E/main/model.onnx",
            f"https://huggingface.co/alice/weights/resolve/{COMMIT}/./model.onnx",
            "https://cdn.example.org/a/%2E/model.onnx",
        ):
            with self.subTest(url):
                asked = []
                folder = self.folder(resources=[resource("model.onnx", ONNX, urls=[url])])
                problems = problems_of(self.check, folder, fetch=self.served({url: ONNX}, asked))
                self.assertIn("may not hold a . or .. part", problems)
                self.assertEqual(asked, [])
        for url in (
            f"https://hf.co/alice/weights/resolve/{COMMIT}/onnx/model.onnx?download=true",
            f"https://huggingface.co/datasets/alice/voices/resolve/{COMMIT}/voices.onnx",
            f"https://huggingface.co/gpt2/resolve/{COMMIT}/model.onnx",
        ):
            with self.subTest(url):
                self.assertIsNone(plan.url_problem(url))

    def test_a_pickle_passes_with_its_kind_in_the_summary(self):
        pickled = b"\x80\x04\x95\x10\0\0\0\0\0\0\0}\x94."
        folder = self.folder(
            resources=[resource("voices.pkl", pickled)],
            files={"tables/speakers.pkl": pickled, "weights.gguf": b"GGUF\x03\0\0\0", "main.py": b"print(1)\n"},
        )
        text = self.check(folder, fetch=self.served({hf_url("voices.pkl"): pickled}))
        self.assertIn(f"| voices.pkl | {len(pickled)} | pickle | huggingface.co |", text)
        self.assertIn(f"| tables/speakers.pkl | {len(pickled)} | pickle |", text)
        self.assertIn("| weights.gguf | 8 | GGUF |", text)
        self.assertNotIn("main.py", text)
        self.assertIn("A pickle runs code when it is loaded", text)

    def test_a_package_over_256_mib_is_refused(self):
        self.assertEqual(plan.MAX_PACKAGE_BYTES, 256 << 20)
        with unittest.mock.patch.object(plan, "MAX_PACKAGE_BYTES", 1 << 20):
            json_bytes = len(json.dumps(manifest()).encode())
            folder = self.folder(files={"data/table.csv": b"x" * ((1 << 20) - json_bytes)})
            self.check(folder)  # exactly at the cap
            (folder / "data/more.csv").write_bytes(b"x")
            problems = problems_of(self.check, folder)
            self.assertIn(f"The plugin folder holds {(1 << 20) + 1} bytes, more than the 1 MiB", problems)
            self.assertIn("Move large files to resources", problems)
            root, checkout = self.source_repo(kind="uv-script", toolchain=None, files={"big.bin": b"x" * (1 << 20)})
            self.assertIn("Move large files to resources", problems_of(plan.check_source, root, ID, "1.0.0", checkout))

    def test_resources_are_held_to_autoscene_s_rules_before_anything_is_downloaded(self):
        ok = resource("model.onnx", ONNX)
        many = [resource(f"m{i}.onnx", ONNX) for i in range(17)]
        big = [dict(resource(f"m{i}.onnx", ONNX), size=3 << 30) for i in range(3)]
        cases = [
            ({"manifestVersion": 1, "resources": [ok]}, 'need "manifestVersion": 2'),
            ({"runtime": {"type": "none"}, "resources": [ok]}, "no use for resources"),
            ({"resources": {"model.onnx": ok}}, "resources must be a list"),
            ({"resources": [ok, "x"]}, "resources[1] must be an object"),
            ({"resources": many}, "names 17 resources. A plugin has at most 16."),
            ({"resources": big}, f"come to {9 << 30} bytes"),
            ({"resources": [dict(ok, size=(4 << 30) + 1)]}, "size must be a whole number of bytes"),
        ]
        for name, expected in (
            ("weights/model.onnx", "it must be one file name"),
            ("../model.onnx", "it must be one file name"),
            ("con.onnx", "Windows reserves that name"),
            ("model onnx", "use only letters"),
            ("m" * 101, "longer than 100 bytes"),
            ("model.onnx.part", "ends in .part, which AutoScene keeps beside its downloads"),
            ("Model.VERIFIED", "ends in .verified"),
            ("x.lock", "ends in .lock"),
            ("x.unpacked", "ends in .unpacked"),
            (None, "can't be used"),
        ):
            cases.append(({"resources": [dict(ok, name=name)]}, expected))
        cases.append(({"resources": [ok, dict(ok, name="MODEL.onnx")]}, "Two resources are named MODEL.onnx"))
        for key in ("url", "sha", "unpack"):
            cases.append(({"resources": [dict(ok, **{key: "x"})]}, f"resources[0] has the key {key}, which"))
        for urls, expected in (
            ([], "needs 1 to 3 urls"),
            ([hf_url("model.onnx")] * 4, "needs 1 to 3 urls"),
            ("https://example.org/model.onnx", "needs 1 to 3 urls"),
            (["http://cdn.example.org/model.onnx"], "plain https:// address"),
            (["https://cdn.example.org/my model.onnx"], "plain https:// address"),
            (["https://cdn.example.org/model.onnx\n"], "plain https:// address"),
            (["https://cdn.example.org\\@evil.example.org/x"], "plain https:// address"),
            (["https://alice@cdn.example.org/model.onnx"], "must name a public host"),
            (["https://10.0.0.1/model.onnx"], "must name a public host"),
            (["https://[::1]/model.onnx"], "must name a public host"),
            (["https://0x7f.1/model.onnx"], "must name a public host"),
            (["https://localhost/model.onnx"], "must name a public host"),
            (["https://files.internal/model.onnx"], "must name a public host"),
            (["https://nas.home.arpa/model.onnx"], "must name a public host"),
            (["https://cdn-.example.org/model.onnx"], "must name a public host"),
            (["https://cdn.example.org:8443/model.onnx"], "port 443 only"),
            (["https://cdn.example.org:/model.onnx"], "port 443 only"),
            ([7], "plain https:// address"),
        ):
            cases.append(({"resources": [dict(ok, urls=urls)]}, expected))
        for sha256 in ("A" * 64, "a" * 63, 7):
            cases.append(({"resources": [dict(ok, sha256=sha256)]}, "sha256 must be 64 lowercase hex digits"))
        for size in (0, True, 1.5, "8", None):
            cases.append(({"resources": [dict(ok, size=size)]}, "size must be a whole number of bytes"))
        for fields, expected in cases:
            with self.subTest(expected=expected, fields=str(fields)[:120]):
                kind = "files" if fields.get("runtime") == {"type": "none"} else "uv-script"
                asked = []
                problems = problems_of(self.check, self.folder(**fields), kind, (), self.served({}, asked))
                self.assertIn(expected, problems)
                self.assertEqual(asked, [])
        port = dict(ok, urls=[f"https://huggingface.co:443/alice/weights/resolve/{COMMIT}/model.onnx"])
        self.check(self.folder(resources=[port]), fetch=self.served({port["urls"][0]: ONNX}))

    def test_source_refuses_a_resource_that_breaks_a_rule_before_the_build(self):
        branch = dict(resource("model.onnx", ONNX), urls=["https://huggingface.co/a/b/resolve/main/model.onnx"])
        root, checkout = self.source_repo(kind="uv-script", toolchain=None, resources=[branch])
        self.assertIn("follows a branch", problems_of(plan.check_source, root, ID, "1.0.0", checkout))
        root, checkout = self.source_repo(kind="uv-script", toolchain=None, resources=[resource("model.onnx", ONNX)])
        self.assertEqual(plan.check_source(root, ID, "1.0.0", checkout), (["main.py"], []))



class TheCatalog(unittest.TestCase):
    """This repo's own files, as a first push reads them."""

    def test_catalog_toml_and_every_plugin_file_pass(self):
        root = HERE.parent
        plan.load_settings(root)
        found = []
        for file in sorted((root / "plugins").glob("*/*.toml")):
            rel = file.relative_to(root).as_posix()
            text = plan.read_text(file, rel)
            if file.name == "plugin.toml":
                plan.check_holder(rel, text)
            else:
                v = plan.parse_version_file(rel, text)
                self.assertTrue((file.parent / "plugin.toml").is_file(), rel)
                found.append((v.id, v.version))
        self.assertIn(("dev.autoscene.hello-export", "1.0.0"), found)
        self.assertIn(("dev.autoscene.discord-notify", "2.1.0"), found)


class Packages(unittest.TestCase):
    """check-package, after asp pack."""

    def package(self, files, id_=ID, version="1.0.0"):
        folder = Path(tempfile.mkdtemp(prefix="test-plan-pkg-"))
        self.addCleanup(lambda: _remove(folder))
        pkg = folder / "x.asp"
        lock = {"lockVersion": 1, "files": [
            {"path": path, "size": 1, "sha256": "0" * 64, "exec": run} for path, run in files.items()
        ]}
        with zipfile.ZipFile(pkg, "w") as z:
            z.writestr("files.lock", json.dumps(lock))
            z.writestr("plugin.json", json.dumps(manifest(id_=id_, version=version)))
        return pkg

    def test_a_package_ci_built_passes(self):
        plan.check_package(self.package({"plugin.json": False, BUILT[0]: True}), set(BUILT), ID, "1.0.0")

    def test_a_file_the_lock_marks_to_run_that_ci_didnt_build_is_refused(self):
        problems = problems_of(plan.check_package, self.package({"tool.sh": True}), set(), ID, "1.0.0")
        self.assertIn("files.lock marks tool.sh to run", problems)

    def test_a_package_of_another_id_or_version_is_refused(self):
        problems = problems_of(plan.check_package, self.package({}, "io.github.bob.x", "2.0.0"), set(), ID, "1.0.0")
        self.assertIn("plugin.json's id is io.github.bob.x", problems)
        self.assertIn("plugin.json's version is 2.0.0", problems)


class Pieces(unittest.TestCase):
    def test_author_binary_knows_each_magic_and_ending(self):
        self.assertIsNone(plan.author_binary("main.py", b"# /// script"))
        self.assertIsNone(plan.author_binary("MZ.txt", b"Hello"))
        self.assertIn("Windows", plan.author_binary("x", b"MZ\x90\x00"))
        self.assertIn("Mach-O", plan.author_binary("x", b"\xfe\xed\xfa\xce"))
        self.assertIn(".cwasm", plan.author_binary("x.CWASM", b""))
        # R28's list in full, and the archives beside it.
        magics = [b"MZ", b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe",
                  b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\0asm"]
        self.assertEqual([m for m, _ in plan.MAGICS], magics)
        for magic, _ in plan.MAGICS + plan.ARCHIVE_MAGICS:
            with self.subTest(magic=magic):
                self.assertIsNotNone(plan.author_binary("data", magic + b"\0\0"))
        self.assertEqual(list(plan.BINARY_ENDINGS), [".pyc", ".so", ".dylib", ".dll", ".node", ".cwasm"])
        for ending in plan.BINARY_ENDINGS + plan.ARCHIVE_ENDINGS + (".tar.gz", ".WHL"):
            with self.subTest(ending=ending):
                why = plan.author_binary("x" + ending, b"text") or ""
                self.assertTrue(why.startswith("a .") and ending.lower().endswith(why[2:-5]), why)

    def test_a_file_s_kind_is_read_from_its_first_bytes(self):
        header = b'{"w":{"dtype":"F32","shape":[1],"data_offsets":[0,4]}}'
        for head, kind in (
            (b"PK\x03\x04\x14\0", "zip"),
            (b"\x1f\x8b\x08\0", "gzip"),
            (b"\x80\x02}q\0.", "pickle"),
            (b"\x80\x05\x95\x10\0", "pickle"),
            (b"GGUF\x03\0\0\0", "GGUF"),
            (len(header).to_bytes(8, "little") + header, "safetensors"),
            (ONNX, "ONNX"),
            (b"\x08\x0a\x3a\x90\x01", "ONNX"),
            (b"# /// script\n", "other"),
            (b"\x80\x01", "other"),
            (b"\x80", "other"),
            ((200 << 20).to_bytes(8, "little") + b"{", "other"),
            (b"\x08\x08pytorch", "other"),
            (b"", "other"),
        ):
            with self.subTest(head=head):
                self.assertEqual(plan.data_kind(head[: plan.HEAD_BYTES]), kind)
                if kind in ("zip", "gzip"):
                    self.assertIsNotNone(plan.author_binary("model.pt", head))
        # Archives whose mark isn't in the first 16 bytes, or that text could
        # start like.
        self.assertGreaterEqual(plan.HEAD_BYTES, 512)
        for head, what in (
            (TAR, "a tar archive"),
            (b"BZh91AY&SY\x00", "a bzip2 archive"),
            (b"BZh9\x17rE8P\x90\0\0\0\0", "a bzip2 archive"),  # an empty one
            (b"\x04\x22\x4d\x18\x64\x40\xa7", "an lz4 archive"),
            (b"\x02\x21\x4c\x18\x00\x00", "an lz4 archive"),
            (b"\x5d\x00\x00\x01\x00\xff\xff\xff", "an lzma archive"),
            (b"\x5d\x00\x00\xc0\x00\xff\xff\xff", "an lzma archive"),  # 12 MiB
            (b"\x5d\x00\x00\x00\x00\x07\x00\x00", None),  # the int32 93, then 0
            (b"\x5d\x00\x00\x50\x00\xff\xff\xff", None),
            (b"\x5d", None),
            (b"x\x01\x01\x00\x00", "zlib-compressed data"),
            (b"x\x9c\xcb\x48\xcd", "zlib-compressed data"),
            (b"x\xda\x2b\x49\x2d", "zlib-compressed data"),
            (b"x^\x8d\x90\x01", "zlib-compressed data"),
            (b"x^2 + y^2 = r^2\n", None),
            (b"x\x01", "zlib-compressed data"),
            (b"BZh9 is a name\n", None),
            (b"a" * 300, None),
        ):
            with self.subTest(head=head[:12]):
                why = plan.author_binary("weights.bin", head[: plan.HEAD_BYTES])
                self.assertEqual(why[: len(what)] if what and why else why, what)
                self.assertEqual(plan.data_kind(head[: plan.HEAD_BYTES]), "other")
        for ending in (".lz4", ".lzma"):
            self.assertEqual(plan.author_binary("x" + ending, b"text"), f"a {ending} file")

    def test_curl_fetches_over_https_only_and_never_more_than_the_size(self):
        mark = "0123456789abcdef" * 2
        args = plan.curl_args("https://huggingface.co/a/b/resolve/x/{m,n}[1-2].onnx", 1234, 600, mark)
        self.assertEqual(args[:2], ["curl", "-q"])  # -q only works first
        self.assertEqual(args[-2:], ["--url", "https://huggingface.co/a/b/resolve/x/{m,n}[1-2].onnx"])
        pairs = {(a, b) for a, b in zip(args, args[1:])}
        for pair in (("--proto", "=https"), ("--proto-redir", "=https"), ("--max-redirs", "10"),
                     ("--max-filesize", "1234"), ("--max-time", "600"),
                     ("--write-out", f"%{{stderr}}\\n{mark} %{{url_effective}} %{{remote_ip}}\\n")):
            self.assertIn(pair, pairs)
        for flag in ("--fail", "--location", "--globoff"):
            self.assertIn(flag, args)
        self.assertNotIn("--output", args)
        self.assertNotIn("--retry", args)  # a retry would write the body to stdout twice

    def test_curl_s_output_is_hashed_and_cut_at_the_size(self):
        url = "https://a.example.org/x"
        calls = []
        with curl_child(b"x" * 3000, calls=calls):
            self.assertEqual(plan.curl_fetch(url, 3000, 600),
                             (3000, hashlib.sha256(b"x" * 3000).hexdigest(), b"x" * plan.HEAD_BYTES))
            self.assertEqual(calls, [(url, 3000, 600)])
        with curl_child(mib=64, led=None):
            got, _, _ = plan.curl_fetch(url, 3000, 600)
            self.assertEqual(got, 3001)  # stopped one byte past the size
        with curl_child(said="curl: (22) The requested URL returned error: 404\n", code=22):
            problems = problems_of(plan.curl_fetch, url, 3000, 600)
            self.assertEqual(problems, "curl: (22) The requested URL returned error: 404 (curl's exit code 22)")
        for code, expected in (
            (63, "the server says it is larger than the 3000 bytes"),
            (28, "it didn't arrive in time. CI gives all of a plugin's downloads 20 minutes"),
            (47, "curl: (47) Maximum (10) redirects followed (curl's exit code 47)"),
            (1, "curl: (1) Protocol \"http\" not supported (curl's exit code 1)"),
        ):
            with self.subTest(code=code), curl_child(said=expected.split(" (curl's")[0] + "\n", code=code):
                self.assertIn(expected, problems_of(plan.curl_fetch, url, 3000, 600))
        with curl_child(b"x" * 3000, led=None):
            self.assertEqual(problems_of(plan.curl_fetch, url, 3000, 600), "curl didn't say where the download led")

    def test_a_download_that_ends_off_the_public_internet_is_refused_without_what_it_got(self):
        public = "https://cdn.example.org/x 2606:2800:21f:cb07:6820:80da:af6b:8b2c"
        with curl_child(b"x" * 3000, led=public):
            self.assertEqual(plan.curl_fetch("https://a.example.org/x", 3000, 600)[0], 3000)
        for led, expected in (
            ("https://10.0.0.5/admin 10.0.0.5", "which isn't a public host name on https port 443"),
            ("https://169.254.169.254/latest 169.254.169.254", "which isn't a public host name"),
            ("https://intranet.internal/x 10.1.2.3", "which isn't a public host name"),
            ("https://cdn.example.org:8443/x 93.184.215.14", "which isn't a public host name on https port 443"),
            ("http://cdn.example.org/x ", "which isn't a public host name on https port 443"),
            ("https://cdn.example.org/x 10.1.2.3", "whose server isn't at a public address"),
            ("https://cdn.example.org/x ::1", "whose server isn't at a public address"),
            ("https://cdn.example.org/x fe80::1", "whose server isn't at a public address"),
            ("https://cdn.example.org/x nonsense", "whose server isn't at a public address"),
        ):
            # Nor does a private server's error reach the messages.
            runs = ((0, ""), (22, "curl: (22) The requested URL returned error: 404\n"))
            for code, said in runs if " 10." in led else runs[:1]:
                with self.subTest(led=led, code=code), curl_child(b"secret" * 500, said=said, led=led, code=code):
                    problems = problems_of(plan.curl_fetch, "https://a.example.org/x", 3000, 600)
                    self.assertIn(f"it led to {led.split(' ')[0]}, {expected}", problems)
                    self.assertNotIn("404", problems)
                    self.assertNotIn("3000", problems)

    def test_hash_stream_checks_stdin_by_size_and_sha256(self):
        sha = hashlib.sha256(b"hello").hexdigest()

        def hash_stream(data, *args):
            return subprocess.run([sys.executable, "-I", str(HERE / "plan.py"), "hash-stream", *args],
                                  input=data, capture_output=True, check=False)

        done = hash_stream(b"hello", "5", sha)
        self.assertEqual((done.returncode, done.stdout.splitlines()), (0, [b"kind=other"]), done.stderr)
        done = hash_stream(b"\x80\x04\x95", "3", hashlib.sha256(b"\x80\x04\x95").hexdigest())
        self.assertEqual(done.stdout.splitlines(), [b"kind=pickle"])
        elf = b"\x7fELF\x02\x01\x01\0"
        elf_sha = hashlib.sha256(elf).hexdigest()
        tar_sha = hashlib.sha256(TAR).hexdigest()
        for data, args, expected in (
            (elf, ("8", elf_sha), b"The file read from stdin is a Linux program or library (ELF)"),
            (elf, ("8", elf_sha, "--name", "model.bin"), b"is a Linux program or library (ELF)"),
            (TAR, (str(len(TAR)), tar_sha), b"is a tar archive"),
            (b"hello", ("5", sha, "--name", "native.so"), b"The file read from stdin is a .so file"),
            (b"hellp", ("5", sha), b"has the SHA-256"),
            (b"hello!", ("5", sha), b"is larger than the 5 bytes"),
            (b"hell", ("5", sha), b"is 4 bytes, but plugin.json gives 5"),
            (b"hello", ("0", sha), b"The size must be 1 to"),
            (b"hello", ("05", sha), b"The size must be 1 to"),
            (b"hello", ("5", sha.upper()), b"64 lowercase hex digits"),
        ):
            with self.subTest(args=args):
                done = hash_stream(data, *args)
                self.assertEqual(done.returncode, 1)
                self.assertTrue(done.stdout.startswith(b"::error::"), done.stdout)
                self.assertIn(expected, done.stdout)

    def test_text_from_authors_is_printed_safely(self):
        self.assertEqual(plan.annotation("a\n::warning::b 100%"), "a%0A::warning::b 100%25")
        self.assertEqual(plan.shown("a\nb‮c"), "a\\u000ab\\u202ec")
        self.assertEqual(plan.markdown("a|b`c"), "a\\|b\\`c")

    def test_a_tool_s_output_goes_into_the_summary_as_a_block_it_can_t_close(self):
        hostile = "Name: x\n````\n</details>\n## No new permissions\n<img src=x onerror=alert(1)>\n``````` y\x1b[2J\n"
        block, body = plan.fenced("What it asks for", hostile)
        fence = "`" * 8
        self.assertTrue(block.startswith(f"### What it asks for\n\n{fence}text\n"), block)
        self.assertTrue(block.endswith(f"\n{fence}\n\n"))
        self.assertEqual(block.count(fence), 2)
        self.assertIn("\\u001b[2J", body)
        folder = Path(tempfile.mkdtemp(prefix="test-plan-block-"))
        self.addCleanup(lambda: _remove(folder))
        (folder / "diff.txt").write_text("::error::fake\n", encoding="utf-8")
        printed = io.StringIO()
        os.environ["GITHUB_STEP_SUMMARY"] = str(folder / "summary.md")
        try:
            with contextlib.redirect_stdout(printed):
                self.assertEqual(plan.main(["block", "asp verify", str(folder / "diff.txt")]), 0)
        finally:
            del os.environ["GITHUB_STEP_SUMMARY"]
        lines = printed.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("::stop-commands::"))
        self.assertEqual(lines[1], "::error::fake")
        self.assertEqual(lines[2], f"::{lines[0].split('::')[2]}::")
        self.assertIn("```text\n::error::fake\n```", (folder / "summary.md").read_text(encoding="utf-8"))

    def test_settings(self):
        self.assertEqual(plan.check_settings(SETTINGS), {"uv": "0.13.7", "asp_rev": REV, "days": 30})
        problems = problems_of(plan.check_settings, 'uv = "latest"\nasp = { rev = "main" }\ndays = 0\n')
        self.assertIn("uv must be", problems)
        self.assertIn("asp.rev must be", problems)
        self.assertIn("days must be", problems)
        # A weekly re-sign that misses a week still leaves one: 15 days at
        # least, and 31 at most, as AutoScene takes.
        for days in (14, 32):
            self.assertIn("days must be a whole number from 15 to 31",
                          problems_of(plan.check_settings, SETTINGS.replace("days = 30", f"days = {days}")))
        for days in (15, 31):
            self.assertEqual(plan.check_settings(SETTINGS.replace("days = 30", f"days = {days}"))["days"], days)

    def test_withdrawn_toml_becomes_the_json_asp_reads(self):
        text = (
            f'[[withdrawn]]\nid = "{ID}"\nversions = "<=1.0.4"\nreason = "It sent export paths to a server."\n'
            "since = 2026-11-03\n\n"
            f'[[withdrawn]]\nid = "{ID}"\nversions = "=2.0.0"\nreason = "Broken."\nsince = 2026-11-03T01:00:00+01:00\n\n'
            f'[[withdrawn]]\nid = "{ID}"\nversions = "=2.0.1"\nreason = "Broken."\nsince = 1793750400\n'
        )
        items = plan.check_withdrawn(text)
        self.assertEqual([i["since"] for i in items], [1793664000, 1793664000, 1793750400])
        self.assertEqual(items[0]["reason"], "It sent export paths to a server.")
        self.assertEqual(plan.check_withdrawn(""), [])
        self.assertIn("time with its zone", problems_of(plan.check_withdrawn, text.replace("+01:00", "")))

    def test_add_names_the_release_the_source_and_who_holds_the_id(self):
        repo = Repo(self)
        repo.add_plugin(version="1.0.0-beta.1+build.5")
        repo.write(f"plugins/{ID}/plugin.toml", HOLDER + f'authorKey = "{KEY}"\n')
        v = plan.load_version(repo.dir, ID, "1.0.0-beta.1+build.5")
        args = plan.add_args(repo.dir, "asp", "index.json", "x.asp", "tcmmi/autoscene-plugins", v)
        self.assertEqual(args[:5], ["asp", "catalog", "add", "index.json", "x.asp"])
        url = args[args.index("--url") + 1]
        self.assertEqual(
            url,
            "https://github.com/tcmmi/autoscene-plugins/releases/download/"
            f"{ID}%401.0.0-beta.1%2Bbuild.5/{ID}-1.0.0-beta.1%2Bbuild.5.asp",
        )
        for flag, value in (("--repo", REPO), ("--commit", COMMIT), ("--path", "."),
                            ("--author", "Alice Example"), ("--author-key", KEY)):
            self.assertEqual(args[args.index(flag) + 1], value)

    def test_show_gives_the_build_its_matrix(self):
        repo = Repo(self)
        repo.add_plugin(text=version_toml(kind="cargo"))
        v = plan.load_version(repo.dir, ID, "1.0.0")
        self.assertEqual(
            [(m["platform"], m["os"], m["locks"]) for m in v.matrix()],
            [("windows-x86_64", "windows-latest", False), ("macos-aarch64", "macos-latest", False),
             ("linux-x86_64", "ubuntu-22.04", True)],
        )
        self.assertEqual(v.slug, "alice/clip-renamer")
        self.assertEqual(v.matrix()[0]["made"], f"src/{BINS['windows-x86_64']}")

    def show(self, text):
        repo = Repo(self)
        repo.add_plugin(text=text)
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            plan.run(["--root", str(repo.dir), "show", ID, "1.0.0"])
        return dict(line.split("=", 1) for line in printed.getvalue().splitlines())

    def test_show_says_how_each_kind_builds(self):
        rust = self.show(version_toml(kind="wasm-rust", path="a/plugin", extra=RUST_KEYS))
        self.assertEqual(json.loads(rust["matrix"]), [{
            "platform": "any", "os": "ubuntu-22.04", "bin": ARTIFACT, "made": "built/plugin.wasm", "locks": False,
        }])
        self.assertEqual((rust["folder"], rust["builds"], rust["wasm"], rust["tool"], rust["python"]),
                         ("a", "true", "true", "", ""))
        extra = PYTHON_KEYS + 'requirements = "a/requirements.txt"\n'
        python = self.show(version_toml(kind="wasm-python", path="plugin", extra=extra))
        self.assertEqual(json.loads(python["matrix"])[0]["made"], "built/plugin.wasm")
        self.assertEqual((python["folder"], python["builds"], python["wasm"]), (".", "true", "true"))
        self.assertEqual(python["tool"], f"componentize-py==0.25.1 --hash=sha256:{plan.COMPONENTIZE_PY['0.25.1']}")
        self.assertEqual((python["python"], python["requirements"]), (plan.BUILD_PYTHON, "a/requirements.txt"))
        uv = self.show(version_toml(path="plugin"))
        self.assertEqual(json.loads(uv["matrix"])[0]["made"], "")
        self.assertEqual((uv["folder"], uv["builds"], uv["wasm"]), ("plugin", "false", "false"))
        self.assertEqual(self.show(version_toml(kind="cargo", path="plugin"))["builds"], "true")

    def test_the_tools_a_webassembly_build_takes_are_pinned_exactly(self):
        self.assertEqual(list(plan.COMPONENTIZE_PY), ["0.25.1"])  # AutoScene's ci.yml and templates at asp.rev
        for version, sha256 in plan.COMPONENTIZE_PY.items():
            self.assertRegex(version, r"^[0-9]+\.[0-9]+\.[0-9]+$")
            self.assertRegex(sha256, r"^[0-9a-f]{64}$")
        self.assertRegex(plan.BUILD_PYTHON, r"^3\.14\.[0-9]+$")


class Publishing(unittest.TestCase):
    """What publish.yml asks of plan.py: release-info before a release, add
    from the releases, the epoch certificate and the commit message."""

    RUN = "https://github.com/tcmmi/autoscene-plugins/actions/runs/42"

    def setUp(self):
        self.repo = Repo(self)
        self.repo.add_plugin()
        self.repo.commit("add")
        self.work = Path(tempfile.mkdtemp(prefix="test-plan-publish-"))
        self.addCleanup(lambda: _remove(self.work))

    def index(self, **fields):
        path = self.work / "index.json"
        path.write_text(json.dumps(index_json(**fields), indent=2) + "\n", encoding="utf-8")
        return path

    def test_release_info_checks_the_package_and_names_its_release(self):
        pkg = make_package(self.work, name="Clip\nRenamer")
        info = plan.release_info(self.repo.dir, pkg, ID, "1.0.0", self.RUN)
        self.assertEqual(info["tag"], f"{ID}@1.0.0")
        self.assertEqual(info["asset"], f"{ID}-1.0.0.asp")
        self.assertEqual(info["title"], "Clip\\u000aRenamer 1.0.0")
        self.assertEqual(info["notes"], f"Built from {REPO}@{COMMIT} by {self.RUN}.")
        self.assertEqual(info["sha256"], hashlib.sha256(pkg.read_bytes()).hexdigest())
        self.assertEqual(plan.release_info(self.repo.dir, make_package(self.work / "x", name=" "), ID, "1.0.0",
                                           self.RUN)["title"], f"{ID} 1.0.0")
        self.assertEqual(len(plan.release_info(self.repo.dir, make_package(self.work / "y", name="n" * 300), ID,
                                               "1.0.0", self.RUN)["title"]), 100 + len(" 1.0.0"))
        # Filed under another id or version, or from another repo: refused.
        other = make_package(self.work / "z", id_="io.github.bob.x", version="2.0.0")
        problems = problems_of(plan.release_info, self.repo.dir, other, ID, "1.0.0", self.RUN)
        self.assertIn("plugin.json's id is io.github.bob.x", problems)
        self.assertIn("plugin.json's version is 2.0.0", problems)
        elsewhere = make_package(self.work / "w", repo="https://github.com/mallory/clip-renamer")
        self.assertIn("must name the same repo", problems_of(plan.release_info, self.repo.dir, elsewhere, ID,
                                                              "1.0.0", self.RUN))
        self.assertIn("--run must be", problems_of(plan.release_info, self.repo.dir, pkg, ID, "1.0.0", "http://x"))
        # On the command line it writes JSON for jq, and its problems to the log.
        out = self.work / "info.json"
        argv = ["--root", str(self.repo.dir), "release-info", str(pkg), ID, "1.0.0", str(out), "--run", self.RUN]
        self.assertEqual(plan.main(argv), 0)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8")), info)
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            argv[3] = str(other)
            self.assertEqual(plan.main(argv), 1)
        self.assertIn("::error::plugin.json's version is 2.0.0", printed.getvalue())

    def test_add_takes_each_package_from_its_release_and_checks_it(self):
        index = self.index()
        released = make_package(self.work / "released")
        sha256 = hashlib.sha256(released.read_bytes()).hexdigest()
        fetched, added = [], []

        def download(url, dest, seconds):
            self.assertLessEqual(seconds, plan.PACKAGE_SECONDS)
            fetched.append(url)
            Path(dest).write_bytes(released.read_bytes())

        def tool(args, refusal):
            added.append(args)

        packages = self.work / "packages"
        with unittest.mock.patch.object(plan, "download_package", download), \
                unittest.mock.patch.object(plan, "tool", tool):
            argv = ["--root", str(self.repo.dir), "add", "asp", str(index), str(packages),
                    "--catalog", "tcmmi/autoscene-plugins",
                    "--released", json.dumps([{"id": ID, "version": "1.0.0", "sha256": sha256}])]
            self.assertEqual(plan.main(argv), 0)
            url = f"https://github.com/tcmmi/autoscene-plugins/releases/download/{ID}%401.0.0/{ID}-1.0.0.asp"
            self.assertEqual(fetched, [url])
            self.assertEqual(len(added), 1)
            self.assertEqual(added[0][:4], ["asp", "catalog", "add", str(index)])
            self.assertEqual(added[0][added[0].index("--url") + 1], url)

            # A file other than the one the release job checked never reaches asp.
            items = [(ID, "1.0.0", "0" * 64)]
            problems = problems_of(plan.add_packages, self.repo.dir, "asp", index, packages,
                                   "tcmmi/autoscene-plugins", items)
            self.assertIn(f"has the SHA-256 {sha256}, not {'0' * 64}", problems)
            # Nor does one of another version under this one's name.
            wrong = make_package(self.work / "wrong", version="2.0.0")
            items = [(ID, "1.0.0", hashlib.sha256(wrong.read_bytes()).hexdigest())]
            released = wrong
            self.assertIn("plugin.json's version is 2.0.0", problems_of(
                plan.add_packages, self.repo.dir, "asp", index, packages, "tcmmi/autoscene-plugins", items))
            self.assertEqual(len(added), 1)

            # A version the index lists already is left as it is (a run again).
            index = self.index(plugins=[(ID, "1.0.0")])
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                plan.add_packages(self.repo.dir, "asp", index, packages, "tcmmi/autoscene-plugins", items)
            self.assertEqual((len(fetched), len(added)), (3, 1))
            self.assertIn(f"The index already lists {ID} 1.0.0: left as it is.", printed.getvalue())

        self.assertIn("--released must be a JSON list", problems_of(plan.parse_versions, '[{"id": "a.b", '
                                                                    '"version": "1.0.0"}]', "--released"))
        self.assertIn("needs one of --versions and --released", problems_of(
            plan.run, ["add", "asp", str(index), str(packages), "--catalog", "a/b"]))

    def test_add_takes_a_build_s_artifact_by_name_in_a_pull_request(self):
        index = self.index()
        packages = self.work / "packages"
        make_package(packages)
        added = []
        with unittest.mock.patch.object(plan, "tool", lambda args, refusal: added.append(args)):
            plan.add_packages(self.repo.dir, "asp", index, packages, "tcmmi/autoscene-plugins", [(ID, "1.0.0", None)])
            self.assertEqual(len(added), 1)
            make_package(packages, name="x", repo="https://github.com/mallory/clip-renamer")
            self.assertIn("must name the same repo", problems_of(
                plan.add_packages, self.repo.dir, "asp", index, packages, "tcmmi/autoscene-plugins",
                [(ID, "1.0.0", None)]))

    def test_asp_s_output_goes_to_the_log_with_workflow_commands_off(self):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            plan.tool([sys.executable, "-I", "-c", "print('::error::from a plugin name')"], "refused")
            problems = problems_of(plan.tool, [sys.executable, "-I", "-c", "raise SystemExit(3)"], "asp refused it.")
        self.assertEqual(problems, "asp refused it.")
        lines = printed.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("::stop-commands::"))
        self.assertEqual(lines[1], "::error::from a plugin name")
        self.assertEqual(lines[2], f"::{lines[0].split('::')[2]}::")

    def test_a_released_package_is_downloaded_over_https_only(self):
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 22, b"", b"curl: (22) The requested URL returned error: 404")

        with unittest.mock.patch.object(plan.subprocess, "run", run):
            problems = problems_of(plan.download_package, "https://github.com/a/b/x.asp", self.work / "x.asp", 90)
        self.assertIn("can't be downloaded (curl exit 22): curl: (22)", problems)
        args = calls[0]
        for flag, value in (("--proto", "=https"), ("--proto-redir", "=https"),
                            ("--max-filesize", str(plan.MAX_ASP_BYTES)), ("--output", str(self.work / "x.asp")),
                            ("--max-time", "90"), ("--retry-max-time", "90")):
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertIn("--fail", args)
        self.assertEqual(args[-1], "https://github.com/a/b/x.asp")

    def test_the_downloads_of_one_add_fit_the_prepare_job_s_time(self):
        def run(args, timeout=None, **kwargs):
            self.assertEqual(timeout, 120)
            raise subprocess.TimeoutExpired(args, timeout)

        with unittest.mock.patch.object(plan.subprocess, "run", run):
            problems = problems_of(plan.download_package, "https://github.com/a/b/x.asp", self.work / "x.asp", 90)
        self.assertIn("wasn't downloaded within 90 seconds", problems)
        # prepare's timeout-minutes holds every download, retries included.
        self.assertLessEqual(plan.ADD_SECONDS + 5 * 60, 60 * 60)
        released = make_package(self.work / "released")
        items = [(ID, "1.0.0", hashlib.sha256(released.read_bytes()).hexdigest())]
        with unittest.mock.patch.object(plan, "ADD_SECONDS", 30):
            problems = problems_of(plan.add_packages, self.repo.dir, "asp", self.index(), self.work / "packages",
                                   "tcmmi/autoscene-plugins", items)
        self.assertIn(f"The downloads took longer than 0 minutes; {ID} 1.0.0 wasn't.", problems)

    def test_the_newest_epoch_certificate_above_the_index_s_is_found(self):
        self.assertEqual(plan.epoch_certificate(self.repo.dir, index_json()), "")
        for name in ("epoch-2.minisig", "epoch-3.minisig", "epoch-10.txt", "epoch-04.minisig", "notes.md"):
            self.repo.write(f"epochs/{name}", "untrusted comment: x\n")
        self.assertEqual(plan.epoch_certificate(self.repo.dir, index_json()), "epochs/epoch-3.minisig")
        self.assertEqual(plan.epoch_certificate(self.repo.dir, index_json(epoch=2)), "epochs/epoch-3.minisig")
        self.assertEqual(plan.epoch_certificate(self.repo.dir, index_json(epoch=3)), "")
        out = self.work / "out.txt"
        os.environ["GITHUB_OUTPUT"] = str(out)
        try:
            self.assertEqual(plan.main(["--root", str(self.repo.dir), "epoch", str(self.index())]), 0)
        finally:
            del os.environ["GITHUB_OUTPUT"]
        self.assertEqual(out.read_text(encoding="utf-8"), "certificate=epochs/epoch-3.minisig\n")

    def test_the_commit_message_says_what_the_index_approves(self):
        first = index_json(plugins=[(ID, "1.0.0")], seq=1)
        text = plan.message(None, first, self.RUN)
        self.assertEqual(text.splitlines()[0], f"catalog: {ID} 1.0.0 approved (seq 1)")
        self.assertIn(f"- {ID} 1.0.0", text)
        self.assertTrue(text.endswith(f"\nSigned by {self.RUN}\n"))

        two = index_json(plugins=[(ID, "1.0.0"), (ID, "1.1.0"), ("io.github.bob.x", "0.1.0")], seq=2)
        text = plan.message(first, two, self.RUN)
        self.assertEqual(text.splitlines()[0], "catalog: 2 versions approved (seq 2)")
        self.assertIn(f"- {ID} 1.1.0\n- io.github.bob.x 0.1.0\n", text)
        self.assertNotIn(f"- {ID} 1.0.0", text)

        gone = {"id": ID, "versions": "<=1.0.4", "reason": "It sent export paths to a server.", "since": 1}
        withdrawn = index_json(plugins=[(ID, "1.0.0")], seq=2, withdrawn=[gone])
        text = plan.message(first, withdrawn, self.RUN)
        self.assertEqual(text.splitlines()[0], "catalog: withdrawals signed (seq 2)")
        self.assertIn("withdrawn.toml changed: the index lists 1 withdrawal now.", text)

        epoch = index_json(plugins=[(ID, "1.0.0")], epoch=2, seq=1)
        self.assertEqual(plan.message(first, epoch, self.RUN).splitlines()[0], "catalog: epoch 2 starts (seq 1)")
        same = index_json(plugins=[(ID, "1.0.0")], seq=2)
        self.assertEqual(plan.message(first, same, self.RUN).splitlines()[0], "catalog: signed again (seq 2)")
        self.assertIn("--run must be", problems_of(plan.message, first, same, "a run"))

        previous, index, out = self.work / "previous.json", self.index(plugins=[(ID, "1.0.0")], seq=1), self.work / "m"
        argv = ["message", "-", str(index), str(out), "--run", self.RUN]
        self.assertEqual(plan.main(argv), 0)
        self.assertEqual(out.read_bytes(), plan.message(None, first, self.RUN).encode())
        previous.write_text(json.dumps(first), encoding="utf-8")
        self.assertEqual(plan.main(["message", str(previous), str(index), str(out), "--run", self.RUN]), 0)
        self.assertTrue(out.read_text(encoding="utf-8").startswith("catalog: signed again (seq 1)\n"))

    def test_an_index_plan_py_can_t_read_is_refused(self):
        for text, why in (
            ("[]", "must be a JSON object"),
            (json.dumps({**index_json(), "seq": "1"}), "seq must be a whole number"),
            (json.dumps({**index_json(), "epoch": True}), "epoch must be a whole number"),
            (json.dumps({**index_json(), "plugins": [{"id": ID}]}), "plugins must be a list of entries"),
            (json.dumps({**index_json(), "withdrawn": {}}), "withdrawn must be a list"),
        ):
            with self.subTest(why):
                path = self.work / "bad.json"
                path.write_text(text, encoding="utf-8")
                self.assertIn(why, problems_of(plan.read_index, path))
        self.assertIn("is missing", problems_of(plan.read_index, self.work / "none.json"))


class Workflows(unittest.TestCase):
    """scripts/lint_workflows.py over .github/workflows. It needs PyYAML; under
    CI a missing PyYAML fails the test instead of skipping it."""

    def lint(self, root):
        try:
            import yaml  # noqa: F401
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("PyYAML isn't installed")
        spec = importlib.util.spec_from_file_location("lint_workflows", HERE / "lint_workflows.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.lint(root)

    def test_the_workflows_pass_the_lint(self):
        self.assertEqual(self.lint(HERE.parent), [])

    def test_the_lint_finds_broken_wiring(self):
        root = Path(tempfile.mkdtemp(prefix="test-plan-lint-"))
        self.addCleanup(lambda: _remove(root))
        flows = root / ".github/workflows"
        flows.mkdir(parents=True)
        for name, swaps in (
            ("check.yml", [
                ("if: ${{ !cancelled() }}", "if: always()"),
                ("needs.plan.outputs.versions", "needs.plan.outputs.version"),
                ("    timeout-minutes: 10\n", ""),
                ("      id-token: write\n", ""),
            ]),
            ("build-plugin.yml", [
                ("value: ${{ jobs.pack.outputs.sha256 }}", "value: ${{ jobs.pack.outputs.sha }}"),
                ("uses: actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6 # v4.2.2",
                 "uses: actions/attest@v4.2.2"),
                ("uses: astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2 # v10.3.3",
                 "uses: astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2"),
                ("${{ steps.pack.outputs.sha256 }}", "${{ steps.pack.outputs.digest }}"),
            ]),
            ("publish.yml", [
                ("      attestations: write\n", ""),
                ("needs.sign.outputs.commit != ''", "needs.sign.outputs.sha != ''"),
                ("    environment: catalog-signing\n", ""),
                ("      INDEX: catalog/site/index.json\n",
                 "      INDEX: catalog/site/index.json\n      LEAK: ${{ secrets.CATALOG_KEY }}\n"
                 "      LEAK2: ${{ toJSON(secrets) }}\n      LEAK3: ${{ secrets['CATALOG_DEPLOY_KEY'] }}\n"),
                ("CATALOG_KEY_PASSWORD: ${{ secrets.CATALOG_KEY_PASSWORD }}",
                 "CATALOG_KEY_PASSWORD: ${{ secrets['CATALOG_KEY_PASSWORD'] }}"),
                ("    timeout-minutes: 10\n", "    timeout-minutes: 10\n    env:\n      ALL: ${{ toJSON(secrets) }}\n"),
                ("      attest: true\n", "      attest: true\n    secrets: inherit\n"),
            ]),
            ("resign.yml", [
                ("    environment: catalog-signing\n", ""),
                ("ref: ${{ needs.prepare.outputs.main }}", "ref: ${{ needs.prepare.outputs.commit }}"),
            ]),
        ):
            text = (HERE.parent / ".github/workflows" / name).read_text(encoding="utf-8")
            for old, new in swaps:
                self.assertIn(old, text)
                text = text.replace(old, new, 1)
            (flows / name).write_text(text, encoding="utf-8", newline="\n")
        problems = "\n".join(self.lint(root))
        self.assertIn("always()", problems)
        self.assertIn("plan has no output version", problems)
        self.assertIn("check.yml:test: no timeout-minutes", problems)
        self.assertIn("attest asks for id-token: write, but the call grants None", problems)
        self.assertIn("jobs.pack.outputs.sha, which isn't there", problems)
        self.assertRegex(problems, r"build-plugin\.yml:\d+: actions/attest@v4\.2\.2 isn't pinned to a commit")
        self.assertRegex(problems, r"build-plugin\.yml:\d+: astral-sh/setup-uv@68559e9\w+ isn't pinned to a commit")
        self.assertIn("steps.pack.outputs.digest isn't set", problems)
        self.assertIn("publish.yml:build: attest asks for attestations: write, but the call grants None", problems)
        self.assertIn("publish.yml:pages job.if: sign has no output sha", problems)
        self.assertRegex(
            problems, r"publish\.yml:sign job\.steps\[\d+\]\.env\.CATALOG_KEY: a secret in a job without an environment"
        )
        for leak in ("LEAK", "LEAK2", "LEAK3"):
            self.assertIn(f"publish.yml:sign job.env.{leak}: a secret outside a step's env", problems)
        self.assertRegex(
            problems, r"publish\.yml:sign job\.steps\[\d+\]\.env\.CATALOG_KEY_PASSWORD: a secret other than "
            r"\$\{\{ secrets\.NAME \}\} alone"
        )
        self.assertIn("publish.yml:plan job.env.ALL: a secret outside a step's env", problems)
        self.assertIn("publish.yml:build: passes secrets to a called workflow", problems)
        self.assertRegex(
            problems, r"resign\.yml:sign job\.steps\[\d+\]\.env\.CATALOG_KEY: a secret in a job without an environment"
        )
        self.assertIn("resign.yml:sign job.steps[0].with.ref: prepare has no output commit", problems)
        # Every action, in every workflow, is pinned: the swaps above are the only finds.
        self.assertEqual(problems.count("isn't pinned"), 2)

    def test_author_code_runs_only_in_build_which_gets_no_token(self):
        try:
            import yaml
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("PyYAML isn't installed")
        source = (HERE.parent / ".github/workflows/build-plugin.yml").read_text(encoding="utf-8")
        doc = yaml.safe_load(source)
        self.assertEqual(doc["permissions"], {"contents": "read"})
        jobs = doc["jobs"]
        self.assertEqual(list(jobs), ["read", "asp", "build", "pack", "attest"])
        build = jobs["build"]
        self.assertEqual(build["permissions"], {"contents": "read"})
        self.assertEqual(set(build), {"name", "needs", "runs-on", "timeout-minutes", "permissions", "outputs",
                                      "strategy", "steps"})
        self.assertEqual(build["needs"], "read")  # not asp: no job after the build's own hands it anything

        # No word for a token or a secret anywhere in the job, and nothing a
        # later job restores.
        text = json.dumps(build)
        for word in ("secrets", "github.token", "GITHUB_TOKEN", "GH_TOKEN", "id-token", "ACTIONS_", "cache@"):
            self.assertNotIn(word, text)
        uses = [s["uses"].split("@")[0] for s in build["steps"] if "uses" in s]
        self.assertEqual(uses, ["actions/checkout", "astral-sh/setup-uv", "actions/upload-artifact"])
        checkout, uv, upload = (s for s in build["steps"] if "uses" in s)
        self.assertIs(checkout["with"]["persist-credentials"], False)
        self.assertIs(uv["with"]["enable-cache"], False)
        self.assertEqual(uv["with"]["github-token"], "")
        self.assertEqual(upload["with"]["path"], "${{ matrix.made }}")

        # Each step that runs the author's code names its env, and it is
        # paths and names only.
        names = [s["name"] for s in build["steps"]]
        for name, env in (
            ("Build with cargo", {"BIN"}),
            ("Build the component with cargo", {"ARTIFACT", "MADE"}),
            ("Build the component with componentize-py", {"MADE"}),
        ):
            step = build["steps"][names.index(name)]
            self.assertEqual(set(step.get("env") or {}), env)
            self.assertIn("working-directory", step)
        self.assertEqual(names[names.index("Build the component with componentize-py") + 1:],
                         ["Note what was built", "Upload what was built"])
        # A plugin's packages are the ones it locks, wheels only, and no
        # config of the author's chooses where they come from.
        runs = [s["run"] for s in build["steps"] if "uv " in s.get("run", "") and "uv lock" not in s["run"]]
        self.assertEqual(len(runs), 2)
        for run in runs:
            for line in run.splitlines():
                if line.strip().startswith("uv "):
                    self.assertIn("--no-config", line)
            self.assertIn("--require-hashes --no-build", run)

        # Only build runs what the author wrote; asp builds AutoScene.
        for jid in ("read", "pack", "attest"):
            for step in jobs[jid]["steps"]:
                self.assertIsNone(re.search(r"\b(cargo|rustup|rustc|uv|uvx|pip|componentize-py|app\.py)\b",
                                            step.get("run", "")), (jid, step.get("name")))
        self.assertEqual(jobs["pack"]["permissions"], {"contents": "read"})
        self.assertEqual(jobs["attest"]["permissions"],
                         {"contents": "read", "id-token": "write", "attestations": "write"})
        self.assertEqual(jobs["attest"]["needs"], "pack")
        self.assertEqual([s.get("uses", "").split("@")[0] for s in jobs["attest"]["steps"]],
                         ["actions/download-artifact", "", "actions/attest"])

        # asp check comes before the package, and in pack, not in build.
        names = [s["name"] for s in jobs["pack"]["steps"]]
        self.assertLess(names.index("Check the plugin folder"), names.index("Check the component"))
        self.assertLess(names.index("Check the component"), names.index("Pack"))

    def test_publish_gives_its_keys_to_one_step_each(self):
        try:
            import yaml
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("PyYAML isn't installed")
        text = (HERE.parent / ".github/workflows/publish.yml").read_text(encoding="utf-8")
        doc = yaml.safe_load(text)
        on = doc.get(True, doc.get("on"))
        self.assertEqual(on, {"push": {"branches": ["main"], "paths-ignore": ["site/**"]}, "workflow_dispatch": None})
        # resign.yml shares the group; no waiting run is dropped.
        self.assertEqual(doc["concurrency"], {"group": "publish", "cancel-in-progress": False, "queue": "max"})
        self.assertEqual(doc["permissions"], {"contents": "read"})
        jobs = doc["jobs"]
        self.assertEqual(list(jobs), ["plan", "asp", "build", "release", "prepare", "sign", "pages"])
        self.assertEqual(jobs["build"]["with"]["attest"], True)
        # Nothing is released that no signer could sign.
        self.assertEqual(jobs["release"]["needs"], ["plan", "asp", "build"])
        self.assertIn("needs.asp.result == 'success'", jobs["release"]["if"])
        self.assertEqual(jobs["sign"]["environment"], "catalog-signing")
        self.assertEqual(jobs["sign"]["permissions"], {"contents": "read"})
        self.assertNotIn("environment", jobs["prepare"])
        self.assertEqual(jobs["prepare"]["permissions"], {"contents": "read"})

        # Each key reaches one step of sign, and nothing else.
        holders = {}
        for jid, job in jobs.items():
            for i, step in enumerate(job.get("steps", [])):
                for name, value in (step.get("env") or {}).items():
                    if re.search(r"\bsecrets\b", str(value)):
                        holders.setdefault((jid, step["name"]), set()).add(name)
        self.assertEqual(holders, {
            ("sign", "Sign"): {"CATALOG_KEY", "CATALOG_KEY_PASSWORD"},
            ("sign", "Push"): {"DEPLOY_KEY"},
        })
        self.assertEqual(text.count("secrets."), 3)
        # sign runs these steps and no others: no package, plugin file or
        # author text reaches it, and no action but GitHub's checkout and
        # download, pinned.
        sign = {s["name"]: s for s in jobs["sign"]["steps"]}
        self.assertEqual(list(sign), [
            "Check out main as prepare found it", "Download asp", "Download the unsigned index",
            "Check asp and the unsigned index", "Sign", "Verify", "Commit", "Push",
        ])
        self.assertEqual(
            {s["uses"].split("@")[0] for s in sign.values() if "uses" in s},
            {"actions/checkout", "actions/download-artifact"},
        )
        self.assertEqual(
            [s["with"].get("name") for s in sign.values() if "download-artifact" in s.get("uses", "")],
            ["tool-asp-sign", "index-unsigned"],
        )
        self.assertNotIn("python", "".join(s.get("run", "") for s in sign.values()))
        self.assertEqual(sign["Sign"]["run"], SIGN_SCRIPT)
        self.assertEqual(sign["Push"]["run"], PUSH_SCRIPT)
        self.assertEqual(sign["Check asp and the unsigned index"]["run"], CHECK_SCRIPT)
        self.assertEqual(sign["Commit"]["run"], COMMIT_SCRIPT)

        # The release job: an attestation from build-plugin.yml on main, for
        # this commit when the release is new; an artifact by its exact name;
        # never --clobber.
        release = jobs["release"]["steps"][1]["run"]
        self.assertIn('signer="$GITHUB_REPOSITORY/.github/workflows/build-plugin.yml"', release)
        self.assertIn(
            'gh attestation verify "$file" --repo "$GITHUB_REPOSITORY" --signer-workflow "$signer" \\\n'
            '      --source-ref refs/heads/main --deny-self-hosted-runners "${made[@]}"', release)
        self.assertIn('made=(--source-digest "$GITHUB_SHA")', release)
        self.assertIn('gh run download "$GITHUB_RUN_ID" --name "asp-$id-$version" --dir "$dir"', release)
        code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
        self.assertEqual([line for line in code if "--clobber" in line or "pattern:" in line], [])
        self.assertEqual([s.get("uses", "").split("@")[0] for s in jobs["release"]["steps"]],
                         ["actions/checkout", "", ""])

    def test_resign_signs_main_s_index_again_as_publish_signs_it(self):
        try:
            import yaml
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("PyYAML isn't installed")
        flows = HERE.parent / ".github/workflows"
        text = (flows / "resign.yml").read_text(encoding="utf-8")
        doc = yaml.safe_load(text)
        publish = yaml.safe_load((flows / "publish.yml").read_text(encoding="utf-8"))
        on = doc.get(True, doc.get("on"))
        self.assertEqual(on, {"schedule": [{"cron": "17 4 * * 1"}], "workflow_dispatch": None})
        # publish.yml's group: two signers never write the same seq, and no
        # waiting run is dropped.
        self.assertEqual(doc["concurrency"], {"group": "publish", "cancel-in-progress": False, "queue": "max"})
        self.assertEqual(doc["concurrency"], publish["concurrency"])
        self.assertEqual(doc["permissions"], {"contents": "read"})
        jobs, theirs = doc["jobs"], publish["jobs"]
        self.assertEqual(list(jobs), ["plan", "asp", "prepare", "sign", "pages", "unsigned"])

        # No author code and nothing but a bump: no build call, no release,
        # no download; plan.py only reads catalog.toml, finds a certificate,
        # reads withdrawn.toml and writes the message, and asp only checks
        # main's index, applies withdrawn.toml to a copy, bumps, signs and
        # verifies.
        self.assertFalse(any("uses" in job for job in jobs.values()))
        runs = "\n".join(s.get("run", "") for job in jobs.values() for s in job.get("steps", []))
        self.assertIsNone(re.search(r"\b(gh|curl|jq)\b", runs))
        self.assertEqual(re.findall(r"plan\.py (\S+)", runs),
                         ["settings", "settings", "epoch", "withdrawn", "message"])
        self.assertEqual(re.findall(r'"\$ASP" catalog (\S+)', runs),
                         ["verify", "withdrawn", "bump", "sign", "verify"])

        # The jobs without the keys, in full: no grant but contents: read,
        # no environment, and only GitHub's own actions. asp runs
        # AutoScene's cargo build, and sign signs what prepare hands it.
        self.assertEqual(set(jobs["plan"]), {"name", "if", "runs-on", "timeout-minutes", "outputs", "steps"})
        self.assertEqual(jobs["plan"]["if"], "github.ref == 'refs/heads/main'")
        self.assertEqual(set(jobs["asp"]), {"name", "needs", "if", "runs-on", "timeout-minutes", "outputs", "steps"})
        self.assertEqual((jobs["asp"]["needs"], jobs["asp"]["if"]), ("plan", "needs.plan.outputs.index == 'true'"))
        for key in ("runs-on", "timeout-minutes", "outputs"):
            self.assertEqual(jobs["asp"][key], theirs["asp"][key], key)
        self.assertEqual(
            set(jobs["prepare"]),
            {"name", "needs", "runs-on", "timeout-minutes", "permissions", "outputs", "env", "steps"},
        )
        self.assertEqual(jobs["prepare"]["needs"], ["plan", "asp"])
        self.assertEqual(jobs["prepare"]["permissions"], {"contents": "read"})
        self.assertEqual(set(jobs["unsigned"]),
                         {"name", "needs", "if", "runs-on", "timeout-minutes", "permissions", "steps"})
        self.assertEqual(jobs["unsigned"]["permissions"], {})
        self.assertEqual(jobs["unsigned"]["needs"], ["prepare", "pages"])
        self.assertEqual(jobs["unsigned"]["if"], "${{ !cancelled() && needs.prepare.outputs.unsigned != '' }}")
        # sign and pages: publish.yml's keys, without its if (each runs only
        # after the job before it succeeded).
        for jid in ("sign", "pages"):
            self.assertEqual(set(jobs[jid]), set(theirs[jid]) - {"if"}, jid)
        for jid, job in jobs.items():
            if jid not in ("sign", "pages"):
                self.assertNotIn("environment", job, jid)
        uses = {
            jid: [s["uses"].split("@")[0] for s in job["steps"] if "uses" in s] for jid, job in jobs.items()
        }
        self.assertEqual(uses, {
            "plan": ["actions/checkout"],
            "asp": ["actions/checkout", "actions/checkout", "actions/upload-artifact"],
            "prepare": ["actions/checkout", "actions/download-artifact", "actions/upload-artifact"],
            "sign": ["actions/checkout", "actions/download-artifact", "actions/download-artifact"],
            "pages": ["actions/checkout", "actions/configure-pages", "actions/upload-pages-artifact",
                      "actions/deploy-pages"],
            "unsigned": [],
        })
        self.assertEqual([s["name"] for s in jobs["prepare"]["steps"]], [
            "Check out main as plan found it", "Read catalog.toml", "Download asp", "Check asp",
            "Check main's index", "Find a new epoch certificate", "See what main's index lacks", "Bump",
            "Hand the index to sign", "Upload the unsigned index",
        ])
        # main's index is checked before anything is made from it.
        self.assertEqual(jobs["prepare"]["steps"][4]["run"].splitlines()[0],
                         'if ! "$ASP" catalog verify catalog/site/index.json; then')

        # plan takes main as it is when the run starts, and every job reads
        # that commit, not the one the schedule named.
        self.assertEqual(jobs["plan"]["steps"][0]["with"]["ref"], "main")
        for jid in ("asp", "prepare"):
            self.assertEqual(jobs[jid]["steps"][0]["with"]["ref"], "${{ needs.plan.outputs.main }}")
        self.assertEqual(jobs["prepare"]["outputs"]["main"], "${{ needs.plan.outputs.main }}")
        self.assertNotIn("github.sha", text)

        def without_ref(steps):
            steps = json.loads(json.dumps(steps))
            steps[0]["with"].pop("ref", None)
            return steps

        # asp is publish.yml's, for that commit.
        self.assertEqual(without_ref(jobs["asp"]["steps"]), without_ref(theirs["asp"]["steps"]))
        # sign is publish.yml's sign job, step for step.
        for key in ("runs-on", "timeout-minutes", "environment", "permissions", "outputs", "env", "steps"):
            self.assertEqual(jobs["sign"][key], theirs["sign"][key], key)
        self.assertEqual(jobs["sign"]["needs"], ["asp", "prepare"])
        self.assertEqual(jobs["sign"]["steps"][0]["with"]["ref"], "${{ needs.prepare.outputs.main }}")
        # pages is publish.yml's, for the commit sign pushed.
        for key in ("runs-on", "timeout-minutes", "environment", "permissions"):
            self.assertEqual(jobs["pages"][key], theirs["pages"][key], key)
        self.assertEqual(without_ref(jobs["pages"]["steps"]), without_ref(theirs["pages"]["steps"]))
        self.assertEqual(jobs["pages"]["steps"][0]["with"]["ref"], "${{ needs.sign.outputs.commit }}")

        # Each key reaches one step of sign, and nothing else.
        holders = {}
        for jid, job in jobs.items():
            for step in job.get("steps", []):
                for name, value in (step.get("env") or {}).items():
                    if re.search(r"\bsecrets\b", str(value)):
                        holders.setdefault((jid, step["name"]), set()).add(name)
        self.assertEqual(holders, {
            ("sign", "Sign"): {"CATALOG_KEY", "CATALOG_KEY_PASSWORD"},
            ("sign", "Push"): {"DEPLOY_KEY"},
        })
        self.assertEqual(text.count("secrets."), 3)


SECOND = "io.github.bob.notes"
SECOND_REPO = "https://github.com/bob/notes"
WITHDRAWN = (
    f'[[withdrawn]]\nid = "{ID}"\nversions = "<=1.0.0"\n'
    'reason = "It sent export paths to a server."\nsince = 2026-11-03\n'
)


class PublishDryRun(unittest.TestCase):
    """publish.yml run on this computer (scripts/dry_run.py): its jobs, ifs,
    env and scripts as GitHub runs them, with stand-ins for gh, curl, asp
    and the actions, and a bare repo as GitHub. It needs a POSIX system with
    bash, git and PyYAML; under CI it fails instead of skipping."""

    def setUp(self):
        why = None
        if os.name == "nt":
            why = "the dry run needs a POSIX system"
        elif not shutil.which("bash"):
            why = "the dry run needs bash"
        else:
            try:
                import yaml  # noqa: F401
            except ImportError:
                why = "PyYAML isn't installed"
        if why:
            if os.environ.get("CI"):
                self.fail(why)
            self.skipTest(why)
        spec = importlib.util.spec_from_file_location("dry_run", HERE / "dry_run.py")
        self.dry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.dry)
        self.packages = {}
        self.world = self.new_world()

    def new_world(self):
        root = Path(tempfile.mkdtemp(prefix="test-plan-dry-"))
        self.addCleanup(lambda: _remove(root))
        return self.dry.World(root, self.package, HERE.parent / ".github/workflows")

    def package(self, id_, version):
        """The package a build of `id_` `version` makes; self.packages
        changes its plugin.json."""
        fields = {"name": "Notes", "repo": SECOND_REPO} if id_ == SECOND else {}
        fields.update(self.packages.get((id_, version), {}))
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as z:
            z.writestr("files.lock", json.dumps({"lockVersion": 1, "files": []}))
            z.writestr("plugin.json", json.dumps(manifest(**{"id_": id_, "version": version, **fields})))
        return data.getvalue()

    def catalog(self, versions=("1.0.0",)):
        files = {
            "catalog.toml": SETTINGS,
            "README.md": "The catalog.\n",
            "scripts/plan.py": (HERE / "plan.py").read_text(encoding="utf-8"),
            f"plugins/{ID}/plugin.toml": HOLDER,
            f"plugins/{SECOND}/plugin.toml": 'author = "Bob Example"\ngithub = ["bob"]\n',
            f"plugins/{SECOND}/1.0.0.toml": version_toml(repo=SECOND_REPO),
        }
        for version in versions:
            files[f"plugins/{ID}/{version}.toml"] = version_toml()
        for flow in (HERE.parent / ".github/workflows").glob("*.yml"):
            files[f".github/workflows/{flow.name}"] = flow.read_text(encoding="utf-8")
        return files

    def first(self, world=None, versions=("1.0.0",), **kwargs):
        """The catalog's first push, and its run."""
        world = world or self.world
        before, sha = world.commit(self.catalog(versions), "catalog: the first plugins")
        self.assertEqual(before, plan.ZERO)
        return world.run(before, sha, **kwargs)

    def push(self, files, **kwargs):
        before, sha = self.world.commit(files, "a merged pull request")
        return self.world.run(before, sha, **kwargs)

    def assertJobs(self, run, **expected):
        got = {jid: run.jobs[jid]["result"] for jid in expected}
        self.assertEqual(got, expected, "\n".join(run.log[-60:]))

    def index(self, world=None):
        text = (world or self.world).show("site/index.json")
        return json.loads(text) if text is not None else None

    def title(self, world=None):
        world = world or self.world
        return self.dry.git("--git-dir", str(world.bare), "log", "-1", "--format=%s", "main")

    def entries(self, world=None):
        return [(p["id"], p["version"]) for p in self.index(world)["plugins"]]

    def resign(self, sha=None, event="schedule", **kwargs):
        """A run of resign.yml; `sha` is the commit the schedule named."""
        return self.world.run(None, sha or self.world.main(), event=event, workflow="resign.yml", **kwargs)

    def test_a_first_push_releases_signs_and_deploys_each_version(self):
        run = self.first()
        self.assertJobs(run, plan="success", asp="success", build="success", release="success",
                        prepare="success", sign="success", pages="success")
        state = self.world.gh()
        url = "https://github.com/tcmmi/autoscene-plugins/actions/runs/" + run.id
        for id_, name, repo in ((ID, "Clip Renamer", REPO), (SECOND, "Notes", SECOND_REPO)):
            r = state["releases"][f"{id_}@1.0.0"]
            self.assertEqual((r["draft"], r["immutable"], r["target"]), (False, True, run.sha))
            self.assertEqual(r["title"], f"{name} 1.0.0")
            self.assertEqual(r["notes"], f"Built from {repo}@{COMMIT} by {url}.")
            self.assertEqual(list(r["assets"]), [f"{id_}-1.0.0.asp"])
        for args in self.world.calls("gh"):
            self.assertNotIn("--clobber", args)
            if args[:2] == ["attestation", "verify"]:
                self.assertEqual(args[3:], [
                    "--repo", "tcmmi/autoscene-plugins", "--signer-workflow", self.dry.SIGNER,
                    "--source-ref", "refs/heads/main", "--deny-self-hosted-runners", "--source-digest", run.sha,
                ])
        index = self.index()
        self.assertEqual(
            [(p["id"], p["version"], p["url"], p["author"]) for p in index["plugins"]],
            [(ID, "1.0.0", plan.release_url("tcmmi/autoscene-plugins", ID, "1.0.0"), "Alice Example"),
             (SECOND, "1.0.0", plan.release_url("tcmmi/autoscene-plugins", SECOND, "1.0.0"), "Bob Example")],
        )
        self.assertEqual((index["seq"], index["published"], index["expires"]),
                         (1, self.dry.START, self.dry.START + 30 * self.dry.DAY))
        self.assertEqual(self.title(), "catalog: 2 versions approved (seq 1)")
        main = self.world.main()
        self.assertEqual(self.dry.git("--git-dir", str(self.world.bare), "rev-parse", "main^"), run.sha)
        self.assertEqual(
            self.dry.git("--git-dir", str(self.world.bare), "diff-tree", "--name-only", "-r", main).split(),
            [main, "site/index.json", "site/index.json.minisig"],
        )
        self.assertEqual(self.world.deployed[-1]["index.json"], self.world.show("site/index.json").encode())

    def test_webassembly_versions_go_through_a_push_like_any_other(self):
        self.first()
        self.packages[(ID, "1.1.0")] = {"runtime": WASM}
        self.packages[(ID, "1.2.0")] = {"runtime": WASM}
        run = self.push({
            f"plugins/{ID}/1.1.0.toml": version_toml(kind="wasm-rust", path="plugin", extra=RUST_KEYS),
            f"plugins/{ID}/1.2.0.toml": version_toml(kind="wasm-python", path="plugin", extra=PYTHON_KEYS),
        })
        self.assertJobs(run, plan="success", build="success", release="success", prepare="success", sign="success",
                        pages="success")
        self.assertEqual(self.entries(), [(ID, "1.0.0"), (ID, "1.1.0"), (ID, "1.2.0"), (SECOND, "1.0.0")])

    def test_a_run_again_adds_nothing_and_a_run_by_hand_deploys_main(self):
        run = self.first()
        main = self.world.main()
        again = self.world.run(run.before, run.sha)
        self.assertJobs(again, plan="success", asp="skipped", release="skipped", prepare="skipped",
                        sign="skipped", pages="skipped")
        self.assertEqual(self.world.main(), main)
        # After a failed deploy, "Run workflow" deploys main's site/ again.
        by_hand = self.world.run(None, main, event="workflow_dispatch")
        self.assertJobs(by_hand, plan="success", asp="success", build="skipped", release="skipped",
                        prepare="success", sign="skipped", pages="success")
        self.assertEqual(by_hand.jobs["prepare"]["outputs"]["changed"], "false")
        self.assertEqual(self.world.main(), main)
        self.assertEqual(len(self.world.deployed), 2)
        self.assertEqual(self.world.deployed[-1], self.world.deployed[0])

    def test_a_run_that_stopped_is_finished_by_the_next_push(self):
        # Until asp knows the catalog's key, sign stops at verify, after the
        # releases are published.
        secrets = {"CATALOG_KEY": "another-key", "CATALOG_KEY_PASSWORD": self.dry.PASSWORD,
                   "CATALOG_DEPLOY_KEY": "deploy"}
        run = self.first(secrets=secrets)
        self.assertJobs(run, release="success", prepare="success", sign="failure", pages="skipped")
        self.assertIn("the index isn't signed by a key asp knows", "\n".join(run.log))
        self.assertEqual(self.world.main(), run.sha)
        self.assertEqual(len(self.world.gh()["releases"]), 2)
        # The push that fixes asp.rev changes catalog.toml only; its run
        # plans what the index doesn't list, takes the published releases as
        # they are, and signs them.
        fixed = self.push({"catalog.toml": SETTINGS.replace(REV, "f" * 40)})
        self.assertJobs(fixed, plan="success", release="success", sign="success", pages="success")
        self.assertEqual(self.entries(), [(ID, "1.0.0"), (SECOND, "1.0.0")])
        creates = [a for a in self.world.calls("gh") if a[:2] == ["release", "create"]]
        self.assertEqual(len(creates), 2)
        verifies = [a for a in self.world.calls("gh") if a[:2] == ["attestation", "verify"]]
        self.assertEqual(len(verifies), 4)
        self.assertTrue(all("--source-digest" not in a for a in verifies[2:]))
        self.assertTrue(all("--signer-workflow" in a for a in verifies))

    def test_a_build_that_fails_holds_back_its_version_only(self):
        run = self.first(fail={(SECOND, "1.0.0")})
        self.assertJobs(run, build="failure", release="failure", prepare="success", sign="success",
                        pages="success")
        self.assertIn(f"Held back for the next run: {SECOND}@1.0.0", "\n".join(run.log))
        self.assertEqual(self.entries(), [(ID, "1.0.0")])
        self.assertEqual(self.title(), f"catalog: {ID} 1.0.0 approved (seq 1)")
        later = self.push({"README.md": "The catalog, and how to submit.\n"})
        self.assertJobs(later, release="success", sign="success", pages="success")
        self.assertEqual(self.entries(), [(ID, "1.0.0"), (SECOND, "1.0.0")])
        self.assertEqual(self.title(), f"catalog: {SECOND} 1.0.0 approved (seq 2)")

    def test_nothing_is_released_when_asp_doesn_t_build(self):
        run = self.first(asp_fails=True)
        self.assertJobs(run, asp="failure", build="success", release="skipped", prepare="skipped",
                        sign="skipped", pages="skipped")
        self.assertEqual(self.world.gh()["releases"], {})

    def test_a_package_that_doesn_t_pass_is_held_back_and_only_planned_artifacts_are_read(self):
        self.packages[(SECOND, "1.0.0")] = {"version": "2.0.0"}
        run = self.first(unattested={(ID, "1.0.0")}, extra={"asp-evil-1.0.0": {"evil-1.0.0.asp": b"PK"}})
        self.assertJobs(run, release="failure", prepare="success", sign="success")
        log = "\n".join(run.log)
        self.assertIn(f"{ID}@1.0.0: the package has no attestation from build-plugin.yml on main.", log)
        self.assertIn("plugin.json's version is 2.0.0", log)
        self.assertEqual(self.world.gh()["releases"], {})
        self.assertEqual(self.entries(), [])
        downloads = [a[a.index("--name") + 1] for a in self.world.calls("gh") if a[:2] == ["run", "download"]]
        self.assertEqual(downloads, [f"asp-{ID}-1.0.0", f"asp-{SECOND}-1.0.0"])
        # An attestation from another workflow, or from another ref, isn't one.
        for attest in ({"workflow": "tcmmi/autoscene-plugins/.github/workflows/check.yml"},
                       {"ref": "refs/pull/7/merge"}):
            world = self.new_world()
            run = self.first(world, attest=attest)
            self.assertJobs(run, release="failure")
            self.assertEqual(world.gh()["releases"], {}, attest)

    def test_a_package_github_renames_is_held_back(self):
        run = self.first(versions=("1.0.0+b.1",))
        self.assertJobs(run, release="failure", sign="success")
        self.assertIn(f"{ID}@1.0.0+b.1: GitHub keeps the package under another name", "\n".join(run.log))
        self.assertEqual(list(self.world.gh()["releases"]), [f"{SECOND}@1.0.0"])
        self.assertEqual(self.entries(), [(SECOND, "1.0.0")])

    def test_a_draft_left_behind_is_made_again(self):
        state = self.world.gh()
        state["releases"][f"{ID}@1.0.0"] = {"draft": True, "immutable": False, "assets": {}, "target": "x",
                                            "title": "old", "notes": "old"}
        self.world.save_gh(state)
        run = self.first()
        self.assertJobs(run, release="success", sign="success")
        self.assertIn(["release", "delete", f"{ID}@1.0.0", "--yes"], self.world.calls("gh"))
        r = self.world.gh()["releases"][f"{ID}@1.0.0"]
        self.assertEqual((r["draft"], r["title"]), (False, "Clip Renamer 1.0.0"))

    def test_main_moving_while_it_signs_puts_the_index_on_top_unless_site_changed(self):
        merged = {}

        def merge():
            merged["sha"] = self.world.commit({"README.md": "Merged while it signed.\n"}, "a merge")[1]

        run = self.first(hooks={"sign": merge})
        self.assertJobs(run, sign="success", pages="success")
        self.assertIn("main moved (try 1): putting the index on top of it.", "\n".join(run.log))
        main = self.world.main()
        self.assertEqual(self.dry.git("--git-dir", str(self.world.bare), "rev-parse", "main^"), merged["sha"])
        self.assertEqual(run.jobs["sign"]["outputs"]["commit"], main)
        self.assertEqual(self.world.show("README.md"), "Merged while it signed.\n")
        # site/ changed on main meanwhile: nothing is pushed over it.
        world = self.new_world()
        hooks = {"sign": lambda: world.commit({"site/index.json": "{}\n"}, "someone else's index")}
        run = self.first(world, hooks=hooks)
        self.assertJobs(run, sign="failure", pages="skipped")
        self.assertIn("site/ changed on main while this run signed the index.", "\n".join(run.log))
        self.assertEqual(world.show("site/index.json"), "{}\n")

    def test_withdrawals_and_a_new_epoch_are_signed(self):
        self.first()
        run = self.push({"withdrawn.toml": WITHDRAWN})
        self.assertJobs(run, build="skipped", release="skipped", prepare="success", sign="success")
        self.assertEqual(self.title(), "catalog: withdrawals signed (seq 2)")
        self.assertEqual([w["id"] for w in self.index()["withdrawn"]], [ID])
        run = self.push({"epochs/epoch-2.minisig": "untrusted comment: epoch 2\n"})
        self.assertJobs(run, asp="success", prepare="success", sign="success", pages="success")
        self.assertEqual(self.title(), "catalog: epoch 2 starts (seq 3)")
        self.assertEqual(self.index()["epoch"], 2)

    def test_without_the_keys_nothing_is_signed_or_pushed(self):
        run = self.first(secrets={})
        self.assertJobs(run, release="success", prepare="success", sign="failure", pages="skipped")
        self.assertIn("needs the secrets CATALOG_KEY and CATALOG_KEY_PASSWORD", "\n".join(run.log))
        self.assertEqual(self.world.main(), run.sha)

    def test_the_weekly_run_signs_main_s_index_again_and_deploys_it(self):
        self.first()
        before, main = self.index(), self.world.main()
        asp_calls, gh_calls = len(self.world.calls("asp")), len(self.world.calls("gh"))
        self.world.wait(7)
        run = self.resign()
        self.assertJobs(run, plan="success", asp="success", prepare="success", sign="success", pages="success",
                        unsigned="skipped")
        self.assertEqual(run.jobs["prepare"]["outputs"]["unsigned"], "")
        after = self.index()
        # Only the seq and the dates change: published is now, and the
        # index runs out a week later than before.
        day = self.dry.DAY
        self.assertEqual(after["seq"], 2)
        self.assertEqual((after["published"], after["expires"]),
                         (before["published"] + 7 * day, before["expires"] + 7 * day))
        self.assertEqual(after["expires"] - after["published"], 30 * day)
        self.assertEqual({**after, "seq": 1, "published": before["published"], "expires": before["expires"]},
                         before)
        self.assertEqual(self.title(), "catalog: signed again (seq 2)")
        body = self.dry.git("--git-dir", str(self.world.bare), "log", "-1", "--format=%b", "main")
        self.assertEqual(body, f"Signed by https://github.com/tcmmi/autoscene-plugins/actions/runs/{run.id}")
        bare = str(self.world.bare)
        self.assertEqual(self.dry.git("--git-dir", bare, "rev-parse", "main^"), main)
        self.assertEqual(
            self.dry.git("--git-dir", bare, "diff-tree", "--name-only", "-r", "main").split()[1:],
            ["site/index.json", "site/index.json.minisig"],
        )
        self.assertEqual(self.world.deployed[-1]["index.json"], self.world.show("site/index.json").encode())
        self.assertEqual(self.world.deployed[-1]["index.json.minisig"],
                         self.world.show("site/index.json.minisig").encode())
        # asp checked main's index, applied withdrawn.toml to a copy, bumped,
        # signed and verified; gh wasn't called: nothing built, released or
        # downloaded.
        self.assertEqual([a[:2] for a in self.world.calls("asp")[asp_calls:]], [
            ["catalog", "verify"], ["catalog", "withdrawn"], ["catalog", "bump"], ["catalog", "sign"],
            ["catalog", "verify"],
        ])
        self.assertEqual(self.world.calls("asp")[asp_calls], ["catalog", "verify", "catalog/site/index.json"])
        self.assertEqual(len(self.world.calls("gh")), gh_calls)
        # "Run workflow" signs it again too, and the next publish run goes on
        # from that seq.
        by_hand = self.resign(event="workflow_dispatch")
        self.assertJobs(by_hand, sign="success", pages="success")
        self.assertEqual(self.title(), "catalog: signed again (seq 3)")
        later = self.push({f"plugins/{ID}/1.1.0.toml": version_toml()})
        self.assertJobs(later, release="success", sign="success", pages="success")
        self.assertEqual(self.title(), f"catalog: {ID} 1.1.0 approved (seq 4)")
        self.assertEqual(self.entries(), [(ID, "1.0.0"), (ID, "1.1.0"), (SECOND, "1.0.0")])

    def test_the_weekly_run_does_nothing_before_the_first_index(self):
        self.world.commit(self.catalog(), "catalog: the first plugins")
        main = self.world.main()
        run = self.resign()
        self.assertJobs(run, plan="success", asp="skipped", prepare="skipped", sign="skipped", pages="skipped",
                        unsigned="skipped")
        self.assertEqual(run.jobs["plan"]["outputs"], {"index": "false", "main": main})
        self.assertIn(
            "main has no signed index yet: publish.yml makes the first one. Then check in the Actions tab that "
            "resign is on, and run it once by hand.", "\n".join(run.log))
        self.assertEqual(self.world.main(), main)
        self.assertEqual(self.world.deployed, [])

    def test_the_weekly_run_signs_only_an_index_a_catalog_key_signed(self):
        self.first()
        signed = self.world.show("site/index.json")
        index = json.loads(signed)
        # An index that reached main past check.yml (an admin's pull
        # request), its signature left as it was.
        index["plugins"].append({**index["plugins"][0], "version": "9.9.9"})
        self.world.commit({"site/index.json": json.dumps(index, indent=2, sort_keys=True) + "\n"}, "an edit")
        main, deployed, asp_calls = self.world.main(), len(self.world.deployed), len(self.world.calls("asp"))
        run = self.resign()
        self.assertJobs(run, plan="success", asp="success", prepare="failure", sign="skipped", pages="skipped",
                        unsigned="skipped")
        log = "\n".join(run.log)
        self.assertIn("the index isn't signed by a key asp knows", log)
        self.assertIn("main's site/index.json isn't one a catalog key signed, or it ran out", log)
        self.assertEqual([a[:2] for a in self.world.calls("asp")[asp_calls:]], [["catalog", "verify"]])
        self.assertEqual(self.world.main(), main)
        self.assertEqual(len(self.world.deployed), deployed)
        # Nor one that ran out: verify refuses it, signed or not.
        self.world.commit({"site/index.json": signed}, "the signed index back")
        self.world.wait(31)
        main = self.world.main()
        run = self.resign()
        self.assertJobs(run, prepare="failure", sign="skipped", pages="skipped")
        self.assertIn("The catalog ran out", "\n".join(run.log))
        self.assertEqual(self.world.main(), main)

    def test_the_weekly_run_fails_while_main_holds_what_its_index_lacks(self):
        self.first()
        # A withdrawal and a new epoch certificate are merged, and their
        # publish run fails to sign.
        run = self.push({"withdrawn.toml": WITHDRAWN, "epochs/epoch-2.minisig": "untrusted comment: epoch 2\n"},
                        secrets={})
        self.assertJobs(run, prepare="success", sign="failure")
        self.assertEqual((self.index()["seq"], self.index()["withdrawn"]), (1, []))
        self.world.wait(7)
        run = self.resign()
        # The index is signed again all the same, without them, and the run
        # is red.
        self.assertJobs(run, prepare="success", sign="success", pages="success", unsigned="failure")
        self.assertEqual(run.jobs["prepare"]["outputs"]["unsigned"], "withdrawn.toml and epochs/epoch-2.minisig")
        self.assertIn(
            "::error::main's index lacks withdrawn.toml and epochs/epoch-2.minisig: no publish run has signed it "
            "yet. Sign it with publish.yml's Run workflow.", "\n".join(run.log))
        index = self.index()
        self.assertEqual((index["seq"], index["epoch"], index["withdrawn"]), (2, 1, []))
        self.assertEqual(index["published"], self.dry.START + 7 * self.dry.DAY)
        self.assertEqual(self.title(), "catalog: signed again (seq 2)")
        # publish.yml's Run workflow signs them, and the next weekly run is
        # green.
        by_hand = self.world.run(None, self.world.main(), event="workflow_dispatch")
        self.assertJobs(by_hand, prepare="success", sign="success", pages="success")
        self.assertEqual(self.title(), "catalog: epoch 2 starts (seq 3)")
        self.assertEqual([w["id"] for w in self.index()["withdrawn"]], [ID])
        run = self.resign()
        self.assertJobs(run, prepare="success", sign="success", pages="success", unsigned="skipped")
        self.assertEqual(run.jobs["prepare"]["outputs"]["unsigned"], "")
        # A withdrawn.toml that doesn't apply counts as one the index lacks.
        self.world.commit({"withdrawn.toml": "withdrawn = 3\n"}, "a broken withdrawn.toml")
        run = self.resign()
        self.assertJobs(run, prepare="success", sign="success", unsigned="failure")
        self.assertEqual(run.jobs["prepare"]["outputs"]["unsigned"], "withdrawn.toml")

    def test_the_weekly_run_signs_main_as_it_is_when_the_run_starts(self):
        self.first()
        # The schedule fired at this commit, and the run then waited in the
        # group `publish` behind a merged version's run, which pushed a new
        # index.
        fired = self.world.main()
        self.push({f"plugins/{ID}/1.1.0.toml": version_toml()})
        self.assertEqual(self.index()["seq"], 2)
        merged = {}

        def merge():
            merged["sha"] = self.world.commit({"README.md": "Merged while it signed.\n"}, "a merge")[1]

        run = self.resign(sha=fired, hooks={"sign": merge})
        self.assertJobs(run, plan="success", prepare="success", sign="success", pages="success")
        self.assertEqual((self.index()["seq"], self.title()), (3, "catalog: signed again (seq 3)"))
        self.assertEqual(self.entries(), [(ID, "1.0.0"), (ID, "1.1.0"), (SECOND, "1.0.0")])
        # A pull request merged while it signed: the index goes on top.
        self.assertIn("main moved (try 1): putting the index on top of it.", "\n".join(run.log))
        self.assertEqual(self.dry.git("--git-dir", str(self.world.bare), "rev-parse", "main^"), merged["sha"])
        self.assertEqual(self.world.show("README.md"), "Merged while it signed.\n")
        # Without the keys nothing is signed or pushed.
        main = self.world.main()
        run = self.resign(secrets={})
        self.assertJobs(run, prepare="success", sign="failure", pages="skipped")
        self.assertIn("needs the secrets CATALOG_KEY and CATALOG_KEY_PASSWORD", "\n".join(run.log))
        self.assertEqual(self.world.main(), main)


# The keyed steps of publish.yml's sign job, word for word: a change to them is
# a change to this test too.
CHECK_SCRIPT = """\
printf '%s  %s\\n' "$ASP_SHA256" "$ASP" "$INDEX_SHA256" unsigned/index.json \\
  "$MESSAGE_SHA256" unsigned/message.txt | sha256sum --check --strict -
chmod +x "$ASP"
mkdir -p previous catalog/site
if [ -f "$INDEX" ]; then
  cp "$INDEX" previous/index.json
fi
cp unsigned/index.json "$INDEX"
"""
SIGN_SCRIPT = """\
if [ -z "$CATALOG_KEY" ] || [ -z "$CATALOG_KEY_PASSWORD" ]; then
  echo "::error::The environment catalog-signing needs the secrets CATALOG_KEY and CATALOG_KEY_PASSWORD."
  exit 1
fi
"$ASP" catalog sign "$INDEX" --key-env CATALOG_KEY --password-env CATALOG_KEY_PASSWORD
"""
COMMIT_SCRIPT = """\
git -C catalog -c core.hooksPath=/dev/null add site/index.json site/index.json.minisig
git -C catalog -c core.hooksPath=/dev/null commit --quiet --file "$GITHUB_WORKSPACE/unsigned/message.txt"
git -C catalog log -1 --stat
"""
PUSH_SCRIPT = """\
if [ -z "$DEPLOY_KEY" ]; then
  echo "::error::The environment catalog-signing needs the secret CATALOG_DEPLOY_KEY."
  exit 1
fi
key="$RUNNER_TEMP/deploy-key"
home="$RUNNER_TEMP/push-home"
trap 'rm -f "$key"' EXIT
mkdir -p "$home"
(umask 077 && printf '%s\\n' "$DEPLOY_KEY" > "$key")
# GitHub's host key (api.github.com/meta, ssh_keys).
printf '%s\\n' "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl" \\
  > "$RUNNER_TEMP/known_hosts"
ssh="ssh -F /dev/null -i $key -o IdentitiesOnly=yes -o IdentityAgent=none -o HostKeyAlgorithms=ssh-ed25519 \
-o StrictHostKeyChecking=yes -o UserKnownHostsFile=$RUNNER_TEMP/known_hosts"
g() {
  env -i PATH=/usr/bin:/bin HOME="$home" GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \\
    GIT_COMMITTER_NAME="$GIT_COMMITTER_NAME" GIT_COMMITTER_EMAIL="$GIT_COMMITTER_EMAIL" \\
    GIT_SSH_COMMAND="$ssh" git -c core.hooksPath=/dev/null -C catalog "$@"
}
remote="git@github.com:$GITHUB_REPOSITORY.git"
for attempt in 1 2 3 4 5; do
  if g push --quiet "$remote" HEAD:refs/heads/main; then
    echo "commit=$(g rev-parse HEAD)" >> "$GITHUB_OUTPUT"
    exit 0
  fi
  g fetch --quiet "$remote" refs/heads/main
  if [ "$(g rev-parse FETCH_HEAD)" = "$(g rev-parse HEAD^)" ]; then
    echo "::error::main didn't move, but the push was refused: check the deploy key and the ruleset's bypass list."
    exit 1
  fi
  # Only CI writes site/, one run at a time: a main whose site/
  # changed since this run started isn't put under this index.
  if ! g diff --quiet HEAD^ FETCH_HEAD -- site/; then
    echo "::error::site/ changed on main while this run signed the index."
    exit 1
  fi
  echo "main moved (try $attempt): putting the index on top of it."
  if ! g rebase --quiet FETCH_HEAD; then
    g rebase --abort
    echo "::error::site/ changed on main while this run signed the index."
    exit 1
  fi
done
echo "::error::main kept moving: the index wasn't pushed."
exit 1
"""


if __name__ == "__main__":
    unittest.main()
