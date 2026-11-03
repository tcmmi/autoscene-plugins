#!/usr/bin/env python3
"""Tests of plan.py: the rules a pull request and a plugin's folder are
held to (R28). Standard library only; check.yml's first job runs them as
`python3 -I scripts/test_plan.py`. They need git."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
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
LOCK = (
    'version = 1\nrevision = 3\nrequires-python = ">=3.10"\n\n'
    '[manifest]\nrequirements = [{ name = "truststore", specifier = "==0.10.5" }]\n\n'
    '[[package]]\nname = "truststore"\nversion = "0.10.5"\nsource = { registry = "https://pypi.org/simple" }\n'
)
KEY = "RWQf6LRCGA9i53mlYecO4IzT51TGPpvWucNSCh1CBM0QTaLn73Y7GFO3"

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


class PullRequests(unittest.TestCase):
    def test_a_new_version_is_planned_with_who_holds_its_id(self):
        repo = Repo(self)
        repo.add_plugin()
        repo.commit("add")
        versions, notes, index = plan.changes(repo.dir, repo.base)
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
        repo.add_plugin(version="1.0.1", text=version_toml(kind="wasm-rust"))
        repo.add_plugin(version="1.0.2", text=version_toml(kind="npm"))
        repo.commit("add")
        problems = problems_of(plan.changes, repo.dir, repo.base)
        self.assertIn("40 lowercase hex digits", problems)
        self.assertIn("wasm-rust comes later", problems)
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
        versions, _, index = plan.changes(repo.dir, repo.base, good)
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
        _, notes, index = plan.changes(repo.dir, repo.base)
        self.assertTrue(any("epoch-2.minisig" in n for n in notes))
        self.assertFalse(index)
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
        self.assertIn("days must be a whole number from 1 to 31", problems)

    def test_a_change_to_catalog_toml_names_the_old_and_new_asp_and_uv(self):
        repo = Repo(self)
        new_rev = "f" * 40
        repo.write("catalog.toml", f'uv = "0.14.0"\nasp = {{ rev = "{new_rev}" }}\ndays = 30\n')
        repo.commit("move asp")
        versions, notes, index = plan.changes(repo.dir, repo.base)
        self.assertEqual((versions, index), ([], False))
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
        _, _, index = plan.changes(repo.dir, base)
        self.assertTrue(index)

    def test_the_first_push_counts_every_version_as_new(self):
        repo = Repo(self)
        repo.add_plugin()
        repo.add_plugin(version="1.1.0")
        repo.write("site/index.json", "{}\n")
        head = repo.commit("all of it")
        versions, notes, index = plan.changes(repo.dir, plan.ZERO, head)
        self.assertEqual([v.version for v in versions], ["1.0.0", "1.1.0"])
        self.assertTrue(index)
        self.assertIn("first push", notes[0])
        # A base that isn't an ancestor (a push that rewrote main) counts the same.
        repo.git("checkout", "-q", "--orphan", "elsewhere")
        repo.git("rm", "-r", "-q", "--cached", ".")
        other = repo.commit("unrelated")
        repo.git("checkout", "-q", "-f", "main")
        versions, _, _ = plan.changes(repo.dir, other, head)
        self.assertEqual([v.version for v in versions], ["1.0.0", "1.1.0"])
        # And so does a base this clone doesn't have.
        versions, _, _ = plan.changes(repo.dir, "e" * 40, head)
        self.assertEqual(len(versions), 2)

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

    def check(self, folder, kind="uv-script", built=()):
        return plan.check_folder(kind, folder, list(built), ID, "1.0.0", REPO)

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
            ("files", {"type": "wasm", "component": "plugin.wasm"}, (), "comes later"),
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
                    files=None, lock=LOCK, bins=None, **manifest_fields):
        root = Repo(self)
        root.add_plugin(text=version_toml(kind=kind, path=path, bins=bins))
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

    def test_assemble_refuses_the_author_s_bin_and_a_missing_build(self):
        root, checkout = self.source_repo(files={"bin/README": b"x"})
        raw = Path(tempfile.mkdtemp(prefix="test-plan-raw-"))
        self.addCleanup(lambda: _remove(raw))
        folder = raw / "pkg"
        self.assertIn("has bin/", problems_of(plan.assemble, root, ID, "1.0.0", checkout, raw / "none", folder, {}))
        root, checkout = self.source_repo()
        problems = problems_of(plan.assemble, root, ID, "1.0.0", checkout, raw / "none", raw / "pkg2", {})
        self.assertEqual(problems.count("left no"), 3)



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
                ("uses: actions/attest@v4.2.2", "uses: actions/attest@main"),
                ("${{ steps.pack.outputs.sha256 }}", "${{ steps.pack.outputs.digest }}"),
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
        self.assertIn("actions/attest@main isn't pinned", problems)
        self.assertIn("steps.pack.outputs.digest isn't set", problems)


if __name__ == "__main__":
    unittest.main()
