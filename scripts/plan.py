#!/usr/bin/env python3
"""The catalog's checks: what a pull request or a push adds, and whether a
plugin's folder is one CI may pack.

Standard library only (Python 3.11 or newer, for tomllib). The workflows run
it as `python3 -I scripts/plan.py <command>`; README.md and POLICY.md say the
same rules in words. Each command fails with exit code 1 and one `::error::`
line per broken rule, so a run shows all of them at once.

  changes --base <rev> [--head <rev>]
      What the commits base..head change, by the rules of a pull request:
      new version files, a new or changed plugin.toml, withdrawn.toml and
      new epoch certificates. An approved version file never changes, a
      withdrawal is never taken back or narrowed, and site/ is CI's. A
      changed catalog.toml is a note naming the old and new asp.rev and uv.
      A base of forty zeros, or one that isn't an ancestor of head, is a
      first push: every version file is new then. Outputs
      `versions` (JSON: [{"id", "version"}]), `any` (some version is new)
      and `index` (the index changes: new versions or withdrawn.toml).
  settings
      catalog.toml: outputs `uv`, `asp_rev` and `days`.
  show <id> <version>
      The version file and catalog.toml, as the build needs them: outputs
      `slug`, `repo`, `commit`, `path`, `kind`, `uv` and `matrix` (JSON).
  source <id> <version> <checkout>
      The author's source at the commit, before anything is built: the
      checks of check-folder that need no build; each uv script's lock, whose
      packages come from a package index or a git repo only; and for cargo a
      rust-toolchain.toml with a dated channel, and build.bins naming no file
      the repo holds. Outputs `scripts`, the uv scripts whose locks the build
      checks (space-separated); a package index other than PyPI goes to the
      job summary.
  assemble <id> <version> <checkout> <raw> <folder> <built> --digests <json>
      Makes the folder asp packs: the author's folder at the commit, without
      .git, plus each program CI built (from the `raw-*` artifacts in <raw>)
      at bin/<platform>/<name>, each checked against the SHA-256 its build
      job gave (--digests, {platform: SHA-256}). Writes the built files to
      <built>, one path per line.
  check-folder <kind> <folder> <built> --id <id> --version <v> --repo <url>
      Before `asp pack`: plugin.json names this id, version and repo; the
      build kind fits its launches; and no file CI didn't build is a binary
      or an archive.
      Writes the files CI built (path, size, SHA-256) to the job summary.
  check-package <pkg.asp> <built> --id <id> --version <v>
      After `asp pack`: the package is this id and version, and its
      files.lock marks no file to run that CI didn't build.
  block <title> <file>
      Adds the text a tool printed (asp verify, asp catalog diff) to the job
      summary in a code fence nothing in it can close, and to the log with
      workflow commands off.
  withdrawn <withdrawn.toml> <out.json>
      withdrawn.toml as the JSON `asp catalog withdrawn` reads.
  add <asp> <index.json> <packages> --catalog <owner/repo> --versions <json>
      Runs `asp catalog add` for each version, with its source and its
      holder from plugin.toml. Each package is
      <packages>/asp-<id>-<version>/<id>-<version>.asp.

`--root <dir>` (before the command) names the catalog repo; by default it
is the one this script is in.
"""

import base64
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tomllib
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent

ZERO = "0" * 40
KINDS = ("uv-script", "cargo", "files")
LATER_KINDS = ("wasm-rust", "wasm-python")
# The platforms CI builds a cargo plugin for, and the runner of each.
CI_PLATFORMS = {
    "windows-x86_64": "windows-latest",
    "macos-aarch64": "macos-latest",
    "linux-x86_64": "ubuntu-22.04",
}
# Where uv locks are checked, and where a kind with no build per platform runs.
LINUX_BUILD = "ubuntu-22.04"
UV_FORM = "uv run --script --locked <script>.py"
# What asp pack leaves out of a folder at any depth, and beside the lock at
# its top (crates/as-plugin/src/package.rs, LEFT_OUT and BESIDE).
LEFT_OUT = (".git", ".hg", ".svn", ".ds_store", "thumbs.db")
BESIDE = ("files.lock", "files.lock.minisig", "author.pub")
# How the programs and libraries of each system start (R28).
MAGICS = (
    (b"MZ", "a Windows program or library (it starts with MZ)"),
    (b"\x7fELF", "a Linux program or library (ELF)"),
    (b"\xfe\xed\xfa\xce", "a macOS program or library (Mach-O)"),
    (b"\xfe\xed\xfa\xcf", "a macOS program or library (Mach-O)"),
    (b"\xce\xfa\xed\xfe", "a macOS program or library (Mach-O)"),
    (b"\xcf\xfa\xed\xfe", "a macOS program or library (Mach-O)"),
    (b"\xca\xfe\xba\xbe", "a macOS universal program (Mach-O)"),
    (b"\0asm", "WebAssembly"),
)
BINARY_ENDINGS = (".pyc", ".so", ".dylib", ".dll", ".node", ".cwasm")
# Archives and installers: what is inside can't be read in a review, and
# tools open them whatever they are called (a zip runs as a Python app, uv
# installs a wheel from a path source, msiexec takes any name).
ARCHIVE_MAGICS = (
    (b"PK\x03\x04", "a zip archive (wheels, jars and zip apps are zips)"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "an OLE file (MSI installers are OLE files)"),
    (b"\x1f\x8b", "a gzip archive"),
    (b"\xfd7zXZ\x00", "an xz archive"),
    (b"\x28\xb5\x2f\xfd", "a zstd archive"),
    (b"7z\xbc\xaf\x27\x1c", "a 7z archive"),
    (b"Rar!\x1a\x07", "a RAR archive"),
)
ARCHIVE_ENDINGS = (
    ".whl", ".egg", ".zip", ".jar", ".pyz", ".msi", ".cab", ".7z", ".rar",
    ".tar", ".tgz", ".gz", ".bz2", ".xz", ".zst",
)
# Where a uv lock may take a package from. path, directory and editable
# install files from the author's folder, url a file from anywhere: each
# would bring code that isn't in the reviewed source or a package index.
UV_SOURCES = ("registry", "git")
PYPI = "https://pypi.org/simple"
MAX_MANIFEST_BYTES = 256 << 10
MAX_LOCK_BYTES = 1 << 20
MAX_UV_LOCK_BYTES = 4 << 20
MAX_TOML_BYTES = 64 << 10
MAX_BLOCK_CHARS = 200_000
# A package path (check_path in crates/as-plugin/src/package.rs).
MAX_PATH_BYTES = 200
MAX_DEPTH = 12
MAX_SHOWN = 200

LABEL = r"[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?"
ID_RE = re.compile(rf"{LABEL}(?:\.{LABEL}){{1,4}}")
NUM = r"(?:0|[1-9][0-9]*)"
PRE = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER_RE = re.compile(rf"{NUM}\.{NUM}\.{NUM}(?:-{PRE}(?:\.{PRE})*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?")
HANDLE = r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}"
HANDLE_RE = re.compile(HANDLE)
REPO_RE = re.compile(rf"https://github\.com/({HANDLE})/([A-Za-z0-9._-]{{1,100}})")
SLUG_RE = re.compile(rf"{HANDLE}/[A-Za-z0-9._-]{{1,100}}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
UV_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
TOOLCHAIN_RE = re.compile(r"(?:stable|beta|nightly)-[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]+\.[0-9]+\.[0-9]+")
VERSION_FILE_RE = re.compile(r"plugins/([^/]+)/([^/]+)\.toml")
EPOCH_RE = re.compile(r"epochs/epoch-([1-9][0-9]*)\.minisig")


class Refused(Exception):
    """A check failed; `problems` says each rule it broke."""

    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


def shown(value, limit=MAX_SHOWN):
    """`value` as text that is safe to print: control and invisible
    characters as escapes, and at most `limit` characters."""
    text = value if isinstance(value, str) else repr(value)
    out = "".join(c if c.isprintable() else f"\\u{ord(c):04x}" for c in text)
    return out if len(out) <= limit else out[: limit - 1] + "…"


def markdown(value):
    """`value` for a Markdown table cell of the job summary."""
    return re.sub(r"([\\`*_{}\[\]<>()#+!|~])", r"\\\1", shown(value))


def annotation(message):
    """`message` as the data of a workflow command (`::error::…`)."""
    return message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def output(name, value):
    """Sets the step output `name` (GITHUB_OUTPUT), or prints it."""
    value = str(value)
    assert "\n" not in value and "\r" not in value, name
    line = f"{name}={value}\n"
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line)
    else:
        sys.stdout.write(line)


def summary(text):
    """Adds Markdown to the job summary (GITHUB_STEP_SUMMARY), or prints it."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(text)
    else:
        sys.stdout.write(text)


# Names and paths --------------------------------------------------------------


def is_id(s):
    """A plugin id as AutoScene reads one (PluginId::parse): 2 to 5 parts of
    lowercase letters, digits and dashes, the first no Windows device name."""
    if not isinstance(s, str) or not ID_RE.fullmatch(s):
        return False
    first = s.split(".")[0]
    return first not in ("con", "prn", "aux", "nul") and not re.fullmatch(r"(?:com|lpt)[0-9]", first)


def is_version(s):
    """A strict semver version, as AutoScene reads one (at most 64 characters)."""
    return isinstance(s, str) and len(s) <= 64 and SEMVER_RE.fullmatch(s) is not None


def path_problem(p):
    """Why `p` can't be a package path (check_path in as-plugin), or None."""
    if not isinstance(p, str) or not p:
        return "it's empty"
    if len(p.encode()) > MAX_PATH_BYTES:
        return "the path is too long"
    parts = p.split("/")
    if len(parts) > MAX_DEPTH:
        return "too many folders deep"
    for part in parts:
        if not part:
            return "it must be a relative path without empty parts"
        if part in (".", ".."):
            return "it can't step into another folder"
        if not re.fullmatch(r"[A-Za-z0-9._@+-]+", part):
            return "use only letters, digits and . _ @ + -"
        if part.endswith("."):
            return "a name can't end with a dot"
        stem = part.split(".")[0].upper()
        if stem in ("CON", "PRN", "AUX", "NUL") or re.fullmatch(r"(?:COM|LPT)[0-9]", stem):
            return "Windows reserves that name"
    return None


def repo_problem(repo):
    """Why `repo` can't be a plugin's source repo, or None."""
    m = REPO_RE.fullmatch(repo) if isinstance(repo, str) else None
    if not m or m.group(2) in (".", "..") or m.group(2).lower().endswith(".git"):
        return (
            f"source.repo is {shown(repo)}. It must be a repo on GitHub, written as "
            "https://github.com/<owner>/<repo>, with no .git or / at the end."
        )
    return None


def author_binary(name, head):
    """Why the file `name`, which starts with the bytes `head`, is a program
    or library that its author may not bring (R28), or None."""
    lower = name.lower()
    for ending in BINARY_ENDINGS + ARCHIVE_ENDINGS:
        if lower.endswith(ending):
            return f"a {ending} file"
    for magic, what in MAGICS + ARCHIVE_MAGICS:
        if head.startswith(magic):
            return what
    return None


def fenced(title, text):
    """`text` from a tool as a block of the job summary: a code fence longer
    than any run of backticks in it, so nothing in it renders as Markdown or
    HTML, whatever an author put into the names it prints."""
    if len(text) > MAX_BLOCK_CHARS:
        text = text[:MAX_BLOCK_CHARS] + "\n… (cut here: the full text is in the job's log)"
    body = "\n".join(shown(line, 4000) for line in text.splitlines())
    longest = max((len(run) for run in re.findall(r"`+", body)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"### {markdown(title)}\n\n{fence}text\n{body}\n{fence}\n\n", body


# TOML files -------------------------------------------------------------------


def read_toml(text, where, problems):
    """`text` as a TOML table, or None with a problem."""
    try:
        return tomllib.loads(text)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        problems.append(f"{where} isn't TOML: {shown(str(e))}")
        return None


def known(table, keys, where, problems):
    for key in table:
        if key not in keys:
            problems.append(f"{where} has the key {shown(key)}, which the catalog doesn't know.")


def subtable(table, key, where, problems):
    value = table.get(key)
    if not isinstance(value, dict):
        problems.append(f"{where} needs a [{key}] table.")
        return None
    return value


@dataclass(frozen=True)
class Version:
    """One version file: plugins/<id>/<version>.toml."""

    id: str
    version: str
    repo: str
    commit: str
    path: str
    kind: str
    bins: dict

    @property
    def slug(self):
        return self.repo.removeprefix("https://github.com/")

    @property
    def file(self):
        return f"plugins/{self.id}/{self.version}.toml"

    def built_paths(self):
        """Where `assemble` puts the programs CI builds: bin/<platform>/<name>."""
        return {f"bin/{p}/{PurePosixPath(b).name}" for p, b in self.bins.items()}

    def matrix(self):
        """The build jobs: one per platform for cargo, else one on Linux."""
        if self.kind == "cargo":
            return [
                {"platform": p, "os": os_, "bin": self.bins[p], "locks": p == "linux-x86_64"}
                for p, os_ in CI_PLATFORMS.items()
            ]
        return [{"platform": "any", "os": LINUX_BUILD, "bin": "", "locks": self.kind == "uv-script"}]


def parse_version_file(rel, text):
    """The version file at `rel` (plugins/<id>/<version>.toml) with `text`."""
    problems = []
    where = shown(rel)
    m = VERSION_FILE_RE.fullmatch(rel)
    if not m or m.group(2) == "plugin":
        raise Refused([f"{where} isn't a version file: plugins/<id>/<version>.toml."])
    id_, version = m.groups()
    if not is_id(id_):
        problems.append(
            f"plugins/{shown(id_)}/ must be named after a plugin id such as io.github.alice.clip-renamer: "
            "2 to 5 parts of lowercase letters, digits and dashes, joined by dots."
        )
    if not is_version(version):
        problems.append(f"{where} must be named after the version, such as 1.2.0.toml.")
    data = read_toml(text, where, problems)
    if data is None:
        raise Refused(problems)
    known(data, ("source", "build"), where, problems)
    source = subtable(data, "source", where, problems) or {}
    build = subtable(data, "build", where, problems) or {}
    known(source, ("repo", "commit", "path"), f"{where} [source]", problems)
    known(build, ("kind", "bins"), f"{where} [build]", problems)

    repo = source.get("repo")
    if why := repo_problem(repo):
        problems.append(f"{where}: {why}")
    commit = source.get("commit")
    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
        problems.append(f"{where}: source.commit must be the full commit id: 40 lowercase hex digits.")
    path = source.get("path", ".")
    if path != "." and (why := path_problem(path)):
        problems.append(f"{where}: source.path {shown(path)} can't be used ({why}). Use . for the repo's top.")

    kind = build.get("kind")
    if kind in LATER_KINDS:
        problems.append(f"{where}: build.kind {kind} comes later. For now the kinds are {', '.join(KINDS)}.")
    elif kind not in KINDS:
        problems.append(f"{where}: build.kind must be one of {', '.join(KINDS)}, not {shown(kind)}.")
    bins = build.get("bins")
    if kind == "cargo":
        if not isinstance(bins, dict):
            problems.append(f"{where}: build kind cargo needs [build.bins], one entry for each of {', '.join(CI_PLATFORMS)}.")
            bins = {}
        for platform in CI_PLATFORMS:
            if platform not in bins:
                problems.append(f"{where}: [build.bins] needs an entry for {platform}.")
        for platform, binary in bins.items():
            entry = f'{where}: build.bins["{shown(platform)}"]'
            if platform not in CI_PLATFORMS:
                problems.append(f"{entry}: CI builds only {', '.join(CI_PLATFORMS)}.")
            elif not isinstance(binary, str) or (why := path_problem(binary)):
                problems.append(f"{entry} can't be used ({why if isinstance(binary, str) else 'it must be a path'}).")
            elif left := [part for part in binary.split("/") if part.lower() in LEFT_OUT]:
                problems.append(
                    f"{entry} is in {shown(left[0])}/, which the checks and asp pack leave out. "
                    "Name the program where cargo builds it, such as target/release/<name>."
                )
            elif platform.startswith("windows-") and not binary.lower().endswith(".exe"):
                problems.append(f"{entry} must be an .exe file.")
    elif bins is not None:
        problems.append(f"{where}: [build.bins] is for build kind cargo only.")
    if problems:
        raise Refused(problems)
    return Version(id_, version, repo, commit, path, kind, dict(bins or {}))


def check_holder(rel, text):
    """plugins/<id>/plugin.toml: who holds the id. Returns the table."""
    problems = []
    data = read_toml(text, rel, problems)
    if data is None:
        raise Refused(problems)
    known(data, ("author", "github", "authorKey"), rel, problems)
    author = data.get("author")
    if not isinstance(author, str) or not author.strip() or len(author) > 60 or not author.isprintable():
        problems.append(f"{rel}: author must be a name of 1 to 60 characters.")
    github = data.get("github")
    if (
        not isinstance(github, list)
        or not 1 <= len(github) <= 10
        or not all(isinstance(h, str) and HANDLE_RE.fullmatch(h) for h in github)
    ):
        problems.append(f"{rel}: github must list 1 to 10 GitHub user names, such as [\"alice\"].")
    key = data.get("authorKey")
    if key is not None and not is_public_key(key):
        problems.append(f"{rel}: authorKey must be the minisign public key line from author.pub (it starts with RW).")
    if problems:
        raise Refused(problems)
    return data


def is_public_key(key):
    """A minisign public key: base64 of "Ed", an 8-byte key id and 32 bytes."""
    if not isinstance(key, str) or len(key) != 56:
        return False
    try:
        raw = base64.b64decode(key, validate=True)
    except ValueError:
        return False
    return len(raw) == 42 and raw[:2] == b"Ed"


def check_settings(text):
    """catalog.toml: the uv version, the AutoScene commit asp is built from,
    and how many days a signature counts."""
    problems = []
    data = read_toml(text, "catalog.toml", problems)
    if data is None:
        raise Refused(problems)
    known(data, ("uv", "asp", "days"), "catalog.toml", problems)
    uv = data.get("uv")
    if not isinstance(uv, str) or not UV_RE.fullmatch(uv):
        problems.append("catalog.toml: uv must be a uv version such as 0.13.7.")
    asp = data.get("asp")
    rev = asp.get("rev") if isinstance(asp, dict) else None
    if isinstance(asp, dict):
        known(asp, ("rev",), "catalog.toml asp", problems)
    if not isinstance(rev, str) or not COMMIT_RE.fullmatch(rev):
        problems.append(
            "catalog.toml: asp.rev must be the full id of the tcmmi/AutoScene commit CI builds asp from "
            f"(40 lowercase hex digits), not {shown(rev)}."
        )
    days = data.get("days")
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 31:
        problems.append("catalog.toml: days must be a whole number from 1 to 31.")
    if problems:
        raise Refused(problems)
    return {"uv": uv, "asp_rev": rev, "days": days}


def check_withdrawn(text):
    """withdrawn.toml, as the list `asp catalog withdrawn` reads:
    [{id, versions, reason, since}], since in unix seconds."""
    problems = []
    data = read_toml(text, "withdrawn.toml", problems)
    if data is None:
        raise Refused(problems)
    known(data, ("withdrawn",), "withdrawn.toml", problems)
    items = data.get("withdrawn", [])
    if not isinstance(items, list):
        raise Refused(problems + ["withdrawn.toml: withdrawn must be a list of [[withdrawn]] tables."])
    out = []
    for i, item in enumerate(items):
        where = f"withdrawn.toml: [[withdrawn]] number {i + 1}"
        if not isinstance(item, dict):
            problems.append(f"{where} must be a table.")
            continue
        known(item, ("id", "versions", "reason", "since"), where, problems)
        if not is_id(item.get("id")):
            problems.append(f"{where}: id must be a plugin id.")
        versions = item.get("versions")
        if not isinstance(versions, str) or not versions.strip() or len(versions) > 100 or not versions.isprintable():
            problems.append(f"{where}: versions must name the versions, such as \"<=1.0.4\".")
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 300 or not reason.isprintable():
            problems.append(f"{where}: reason must say why in 1 to 300 characters, on one line.")
        since = unix_time(item.get("since"))
        if since is None:
            problems.append(f"{where}: since must be a date (2026-11-03), a time with its zone, or unix seconds.")
        out.append({"id": item.get("id"), "versions": versions, "reason": reason, "since": since})
    if problems:
        raise Refused(problems)
    return out


def unix_time(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, datetime.datetime):
        return int(value.timestamp()) if value.tzinfo is not None else None
    if isinstance(value, datetime.date):
        return int(datetime.datetime(value.year, value.month, value.day, tzinfo=datetime.UTC).timestamp())
    return None


def read_text(path, where):
    if path.is_symlink() or not path.is_file():
        raise Refused([f"{where} is missing."])
    data = path.read_bytes()
    if len(data) > MAX_TOML_BYTES:
        raise Refused([f"{where} is larger than {MAX_TOML_BYTES // 1024} KiB."])
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused([f"{where} isn't UTF-8 text."]) from None


def load_version(root, id_, version):
    if not is_id(id_) or not is_version(version):
        raise Refused([f"{shown(id_)} {shown(version)} isn't a plugin id and a version."])
    rel = f"plugins/{id_}/{version}.toml"
    return parse_version_file(rel, read_text(root / rel, rel))


def load_settings(root):
    return check_settings(read_text(root / "catalog.toml", "catalog.toml"))


# What a change adds -------------------------------------------------------------


def git(root, *args):
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
    if done.returncode != 0:
        raise Refused([f"git {' '.join(args)} failed: {shown(done.stderr.decode(errors='replace').strip())}"])
    return done.stdout


def is_ancestor(root, base, head):
    done = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", base, head], capture_output=True, check=False
    )
    return done.returncode == 0


def blob(root, rev, path):
    """The file `path` at `rev` as text, or None when it isn't there."""
    done = subprocess.run(
        ["git", "-C", str(root), "cat-file", "blob", f"{rev}:{path}"], capture_output=True, check=False
    )
    if done.returncode != 0:
        return None
    if len(done.stdout) > MAX_TOML_BYTES:
        raise Refused([f"{path} is larger than {MAX_TOML_BYTES // 1024} KiB."])
    try:
        return done.stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused([f"{path} isn't UTF-8 text."]) from None


def changes(root, base, head="HEAD"):
    """What base..head changes, checked by the rules of a pull request.
    Returns (versions, notes, index): the new versions, sorted; lines for
    the job summary; and whether the index changes."""
    head = git(root, "rev-parse", "--verify", f"{head}^{{commit}}").decode().strip()
    first = base == ZERO or not is_ancestor(root, base, head)
    if first:
        listing = git(root, "ls-tree", "-r", "-z", "--name-only", head).decode("utf-8", "replace")
        changed = [("A", p) for p in listing.split("\0") if p]
    else:
        raw = git(root, "diff", "--no-renames", "--name-status", "-z", base, head).decode("utf-8", "replace")
        fields = [f for f in raw.split("\0") if f]
        changed = list(zip(fields[0::2], fields[1::2]))

    problems, notes, versions = [], [], []
    holders, withdrawn = set(), False
    if first:
        notes.append("This is a first push, or its base isn't an ancestor: every version file counts as new.")

    for status, path in changed:
        status = status[:1]
        p = shown(path)
        if path.startswith("site/"):
            if not first:
                problems.append(f"{p}: site/ is written by CI only.")
            continue
        if path == "withdrawn.toml":
            if status == "D":
                problems.append("withdrawn.toml can't be removed: a withdrawal is never taken back.")
            else:
                withdrawn = True
            continue
        if path.startswith("epochs/"):
            m = EPOCH_RE.fullmatch(path)
            if not m or int(m.group(1)) < 2:
                problems.append(f"{p}: epochs/ holds only certificates named epoch-<n>.minisig, n from 2 up.")
            elif status != "A" and not first:
                problems.append(f"{p}: an epoch certificate never changes.")
            else:
                notes.append(f"New epoch certificate: `{path}`.")
            continue
        if path == "catalog.toml":
            if status == "D":
                problems.append("catalog.toml can't be removed: CI reads its settings.")
                continue
            try:
                new = check_settings(blob(root, head, path) or "")
            except Refused as e:
                problems += e.problems
                continue
            notes.append(settings_note(None if first else old_settings(root, base), new))
            continue
        if not path.startswith("plugins/"):
            continue
        parts = path.split("/")
        if len(parts) != 3:
            problems.append(f"{p}: plugins/ holds only plugins/<id>/plugin.toml and plugins/<id>/<version>.toml.")
            continue
        if parts[2] == "plugin.toml":
            if not is_id(parts[1]):
                problems.append(f"plugins/{shown(parts[1])}/ must be named after a plugin id.")
            elif status == "D":
                problems.append(f"{p} can't be removed: it says who holds the id.")
            else:
                holders.add(parts[1])
                if not first:
                    notes.append(
                        f"`{path}` is new or changed: it says who holds the id. "
                        "A changed authorKey needs the old key's owner (POLICY.md, Names)."
                    )
            continue
        if not parts[2].endswith(".toml"):
            problems.append(f"{p}: plugins/ holds only plugins/<id>/plugin.toml and plugins/<id>/<version>.toml.")
            continue
        if status != "A":
            problems.append(f"{p}: an approved version file never changes. Submit a new version instead.")
            continue
        try:
            v = parse_version_file(path, blob(root, head, path) or "")
        except Refused as e:
            problems += e.problems
            continue
        versions.append(v)
        holders.add(v.id)

    for id_ in sorted(holders):
        rel = f"plugins/{id_}/plugin.toml"
        text = blob(root, head, rel)
        if text is None:
            problems.append(f"{shown(rel)} is missing: a plugin's first pull request adds it, to say who holds the id.")
            continue
        try:
            check_holder(rel, text)
        except Refused as e:
            problems += e.problems
    if withdrawn:
        try:
            now = check_withdrawn(blob(root, head, "withdrawn.toml") or "")
            notes.append("`withdrawn.toml` changes: the index withdraws what it lists.")
            if not first:
                problems += taken_back(old_withdrawn(root, base), now)
        except Refused as e:
            problems += e.problems
    if problems:
        raise Refused(problems)
    versions.sort(key=lambda v: (v.id, v.version))
    return versions, notes, bool(versions) or withdrawn


def old_settings(root, base):
    """catalog.toml at `base`, or None when it isn't there or can't be read."""
    try:
        text = blob(root, base, "catalog.toml")
        return None if text is None else check_settings(text)
    except Refused:
        return None


def settings_note(old, new):
    """The summary line for a changed catalog.toml. The asp it names runs
    beside the signing key in publish.yml, so the reviewer is told."""
    def value(name):
        if old is None or old[name] == new[name]:
            return f"`{new[name]}`"
        return f"`{new[name]}` (was `{old[name]}`)"

    return (
        f"**`catalog.toml` changes:** asp.rev {value('asp_rev')}, uv {value('uv')}, days {value('days')}. "
        "CI builds `asp` from that tcmmi/AutoScene commit, and after the merge that `asp` runs beside "
        "the catalog's signing key: it must be a commit on AutoScene's `main` (CI checks that too)."
    )


def old_withdrawn(root, base):
    """withdrawn.toml at `base` as check_withdrawn reads it ([] when missing).
    It passed its checks when it was merged."""
    try:
        return check_withdrawn(blob(root, base, "withdrawn.toml") or "")
    except Refused:
        return []


def taken_back(before, now):
    """Withdrawals of `before` that `now` no longer makes, each as a problem.
    A withdrawal is never taken back or narrowed: its id and versions stay."""
    kept = {(item["id"], item["versions"]) for item in now}
    return [
        f"withdrawn.toml no longer withdraws {shown(item['id'])} {shown(item['versions'])}. "
        "A withdrawal is never taken back or narrowed: add a new entry instead of changing one."
        for item in before
        if (item["id"], item["versions"]) not in kept
    ]


def source_link(v):
    tree = f"{v.repo}/tree/{v.commit}"
    return tree if v.path == "." else f"{tree}/{v.path}"


# A plugin's folder ----------------------------------------------------------------


def read_manifest(folder):
    """The folder's plugin.json, or a problem."""
    path = folder / "plugin.json"
    if path.is_symlink() or not path.is_file():
        raise Refused(["The plugin folder has no plugin.json."])
    data = path.read_bytes()
    if len(data) > MAX_MANIFEST_BYTES:
        raise Refused(["plugin.json is larger than 256 KiB."])
    return parse_manifest(data)


def parse_manifest(data):
    def no_twice(pairs):
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError("an object names a key twice")
        return dict(pairs)

    try:
        manifest = json.loads(data.decode("utf-8-sig"), object_pairs_hook=no_twice)
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise Refused([f"plugin.json isn't JSON that AutoScene reads: {shown(str(e))}"]) from None
    if not isinstance(manifest, dict):
        raise Refused(["plugin.json must be a JSON object."])
    return manifest


def manifest_problems(manifest, id_, version, repo=None):
    """plugin.json must name the version file's id, version and repo."""
    problems = []
    if manifest.get("id") != id_:
        problems.append(
            f"plugin.json's id is {shown(manifest.get('id'))}, but its folder here is plugins/{id_}/. "
            "The folder is named after the id."
        )
    if manifest.get("version") != version:
        problems.append(
            f"plugin.json's version is {shown(manifest.get('version'))}, but its version file is {version}.toml. "
            "The file is named after the version."
        )
    if repo is not None and manifest.get("repository") != repo:
        problems.append(
            f"plugin.json's repository is {shown(manifest.get('repository'))}, but the version file's "
            f"source.repo is {repo}. They must name the same repo."
        )
    return problems


def launches(manifest):
    """Each runtime.command entry as (key, how, what): how is "uv" (what is
    the script), "program" (what is the program) or "bad"."""
    runtime = manifest.get("runtime")
    command = runtime.get("command") if isinstance(runtime, dict) else None
    if not isinstance(command, dict):
        return []
    out = []
    for key in sorted(command):
        parts = command[key]
        if not isinstance(parts, list) or not parts or not all(isinstance(x, str) for x in parts):
            out.append((key, "bad", None))
            continue
        name = parts[0].lower().removesuffix(".exe")
        if name == "uv":
            uv = parts[0] == "uv" and parts[1:4] == ["run", "--script", "--locked"]
            if uv and len(parts) == 5 and parts[4].endswith(".py"):
                out.append((key, "uv", parts[4]))
            else:
                out.append((key, "bad", None))
        else:
            out.append((key, "program", parts[0]))
    return out


def uv_scripts(manifest):
    return sorted({what for _, how, what in launches(manifest) if how == "uv"})


def kind_problems(kind, manifest, built):
    """Whether the build kind fits the manifest's launches (R28). `built` is
    the set of package paths CI builds."""
    runtime = manifest.get("runtime")
    rtype = runtime.get("type") if isinstance(runtime, dict) else None
    named = "missing" if rtype is None else shown(rtype)
    if rtype == "wasm":
        return ['runtime.type "wasm" needs a WebAssembly build kind, which comes later.']
    if kind == "files":
        if rtype != "none":
            return [
                'Build kind files is for a plugin that runs no program (runtime.type "none"), '
                f"but this one's runtime.type is {named}. A uv script needs build kind uv-script, "
                "a Rust program build kind cargo."
            ]
        return []
    if rtype not in ("process", "service"):
        hint = " Use build kind files." if rtype == "none" else ""
        return [
            f"Build kind {kind} is for a plugin that runs a program "
            f'(runtime.type "process" or "service"), but this one\'s runtime.type is {named}.{hint}'
        ]
    found = launches(manifest)
    problems = [] if found else ["runtime.command names no command."]
    for key, how, what in found:
        where = f'runtime.command["{shown(key)}"]'
        if how == "bad":
            problems.append(f"{where} is no command AutoScene runs. A uv script runs as {UV_FORM}.")
        elif how == "uv":
            if why := path_problem(what):
                problems.append(f"{where} names the script {shown(what)}, which can't be used ({why}).")
        elif kind == "uv-script":
            problems.append(
                f"{where} starts {shown(what)}, but build kind uv-script runs only uv scripts ({UV_FORM}). "
                "A Rust program needs build kind cargo."
            )
        elif not (key in CI_PLATFORMS and what.startswith(f"bin/{key}/") and what in built):
            problems.append(
                f"{where} starts {shown(what)}, which CI doesn't build. With build kind cargo, each "
                "platform's program is bin/<platform>/<the file name build.bins gives>, built for "
                f"{', '.join(CI_PLATFORMS)}."
            )
    if kind == "cargo" and not any(how == "program" for _, how, _ in found):
        problems.append(
            "Build kind cargo builds programs, but runtime.command starts none. "
            "A plugin that only runs uv scripts uses build kind uv-script."
        )
    return problems


def folder_files(folder):
    """Every file of `folder` that asp pack would take, as (package path,
    file). Links are problems: a package can't hold one."""
    files, problems = [], []
    for here, dirs, names in os.walk(folder):
        base = Path(here)
        rel = base.relative_to(folder).as_posix()
        prefix = "" if rel == "." else rel + "/"
        dirs[:] = sorted(d for d in dirs if d.lower() not in LEFT_OUT)
        for d in list(dirs):
            if (base / d).is_symlink():
                problems.append(f"{shown(prefix + d)} is a link, which a package can't hold.")
                dirs.remove(d)
        for name in sorted(names):
            path = prefix + name
            if name.lower() in LEFT_OUT or path in BESIDE:
                continue
            if (base / name).is_symlink():
                problems.append(f"{shown(path)} is a link, which a package can't hold.")
                continue
            files.append((path, base / name))
    return files, problems


def binary_problems(folder, built, cargo):
    """The author's files that are programs or libraries (R28), and for
    cargo any file in bin/ that CI didn't build there."""
    files, problems = folder_files(folder)
    for path, file in files:
        if path in built:
            continue
        with open(file, "rb") as f:
            head = f.read(8)
        if why := author_binary(PurePosixPath(path).name, head):
            problems.append(
                f"{shown(path)} is {why}. A plugin's folder brings no programs, libraries or archives: "
                "CI builds every program itself, and no one can review what is inside an archive."
            )
        elif cargo and path.startswith("bin/"):
            problems.append(f"{shown(path)} is in bin/, where CI puts the programs it builds. Move it elsewhere.")
    return problems


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def read_built(path):
    text = Path(path).read_text(encoding="utf-8")
    return sorted({line for line in text.splitlines() if line})


def write_built(path, built):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(f"{p}\n" for p in sorted(built))


def check_folder(kind, folder, built, id_, version, repo):
    """The checks before `asp pack` (R28). Returns the summary Markdown."""
    folder = Path(folder)
    if kind not in KINDS:
        raise Refused([f"Build kind {shown(kind)} isn't one CI builds."])
    if not is_id(id_) or not is_version(version) or repo_problem(repo):
        raise Refused([f"{shown(id_)} {shown(version)} {shown(repo)} isn't a plugin id, a version and a repo."])
    manifest = read_manifest(folder)
    problems = manifest_problems(manifest, id_, version, repo)
    problems += kind_problems(kind, manifest, set(built))
    for path in built:
        file = folder / path
        if path_problem(path) or file.is_symlink() or not file.is_file():
            problems.append(f"{shown(path)}, which CI built, isn't in the plugin folder.")
    problems += binary_problems(folder, set(built), kind == "cargo")
    if problems:
        raise Refused(problems)
    lines = ["### Files CI built\n\n"]
    if built:
        lines.append("| File | Size (bytes) | SHA-256 |\n|---|---:|---|\n")
        for path in built:
            file = folder / path
            lines.append(f"| {markdown(path)} | {file.stat().st_size} | `{sha256_file(file)}` |\n")
    else:
        lines.append("None: the package holds the author's folder as it is at the commit.\n")
    return "".join(lines) + "\n"


def built_path_problems(checkout, v):
    """build.bins must name where cargo puts a program, not a file the repo
    holds: the build uploads that path, and a committed file there would
    ship as if CI had built it."""
    problems = []
    for binary in sorted(set(v.bins.values())):
        step = checkout
        for part in binary.split("/"):
            step = step / part
            if step.is_symlink():
                problems.append(f"build.bins names {shown(binary)}, which goes through a link in the repo.")
                break
        else:
            if step.exists():
                problems.append(
                    f"build.bins names {shown(binary)}, which the repo holds at the commit. It must be where "
                    "cargo builds the program (target/release/<name>), and nothing there may be committed."
                )
    return problems


def read_uv_lock(path, where):
    if path.is_symlink() or not path.is_file():
        raise Refused([f"{where} is missing. AutoScene runs the script with --locked, which needs it: "
                       "make it with uv lock --script <script>.py."])
    data = path.read_bytes()
    if len(data) > MAX_UV_LOCK_BYTES:
        raise Refused([f"{where} is larger than {MAX_UV_LOCK_BYTES >> 20} MiB."])
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused([f"{where} isn't UTF-8 text."]) from None


def uv_lock_problems(folder, scripts):
    """Each uv script's lock takes its packages from a package index or a git
    repo only (UV_SOURCES). Returns (problems, notes): a package index other
    than PyPI is a note for the reviewer."""
    problems, notes = [], []
    for script in scripts:
        if path_problem(script):
            continue  # kind_problems says why the script can't be used
        where = f"{shown(script)}.lock"
        try:
            text = read_uv_lock(folder / f"{script}.lock", where)
        except Refused as e:
            problems += e.problems
            continue
        data = read_toml(text, where, problems)
        if data is None:
            continue
        packages = data.get("package", [])
        if not isinstance(packages, list):
            problems.append(f"{where}: package must be a list of [[package]] tables.")
            continue
        indexes = set()
        for package in packages:
            name = shown(package.get("name")) if isinstance(package, dict) else "?"
            source = package.get("source") if isinstance(package, dict) else None
            kinds = list(source) if isinstance(source, dict) else []
            if len(kinds) != 1 or kinds[0] not in UV_SOURCES:
                what = shown(", ".join(f"{k} {source[k]}" for k in kinds)) if kinds else "nowhere it names"
                problems.append(
                    f"{where} installs {name} from {what}. A plugin's packages come from a package index "
                    "or a git repo; a wheel, folder or file from the plugin or from a URL is a binary CI "
                    "didn't build."
                )
            elif kinds[0] == "registry" and source["registry"] != PYPI:
                indexes.add(source["registry"])
        for index in sorted(indexes, key=str):
            notes.append(f"`{markdown(script)}` takes packages from the index {markdown(index)}, not PyPI.")
    return problems, notes


def check_source(root, id_, version, checkout):
    """The checks on the author's source before it is built. Returns the uv
    scripts whose locks the build checks, and notes for the reviewer."""
    v = load_version(root, id_, version)
    checkout = Path(checkout)
    folder = checkout if v.path == "." else checkout / v.path
    problems = []
    step = checkout
    for part in [] if v.path == "." else v.path.split("/"):
        step = step / part
        if step.is_symlink():
            raise Refused([f"source.path {v.path} goes through a link. It must be a folder in the repo."])
    if not folder.is_dir():
        raise Refused([f"The repo has no folder {v.path} at {v.commit}."])
    manifest = read_manifest(folder)
    problems += manifest_problems(manifest, v.id, v.version, v.repo)
    problems += kind_problems(v.kind, manifest, v.built_paths())
    problems += binary_problems(folder, set(), False)
    if v.kind == "cargo":
        if (folder / "bin").exists() or (folder / "bin").is_symlink():
            problems.append("The plugin folder has bin/, where CI puts the programs it builds. Move it elsewhere.")
        problems += toolchain_problems(checkout, folder)
        problems += built_path_problems(checkout, v)
    scripts = [] if v.kind == "files" else uv_scripts(manifest)
    lock_problems, notes = uv_lock_problems(folder, scripts)
    problems += lock_problems
    if problems:
        raise Refused(problems)
    return scripts, notes


def toolchain_problems(checkout, folder):
    """cargo builds with the toolchain rustup finds from the plugin folder
    up to the repo's top; it must be fixed."""
    hint = "a dated channel such as nightly-2026-10-01, or a release such as 1.91.0"
    top = checkout.resolve()
    here = folder.resolve()
    while True:
        legacy, toml = here / "rust-toolchain", here / "rust-toolchain.toml"
        if legacy.exists() or legacy.is_symlink():
            return [f"The repo names its toolchain in rust-toolchain. CI needs rust-toolchain.toml with {hint}."]
        if toml.is_symlink():
            return ["rust-toolchain.toml is a link. CI needs the file itself."]
        if toml.exists():
            problems = []
            try:
                data = read_toml(read_text(toml, "rust-toolchain.toml"), "rust-toolchain.toml", problems)
            except Refused as e:
                return e.problems
            if data is None:
                return problems
            table = data.get("toolchain")
            channel = table.get("channel") if isinstance(table, dict) else None
            if not isinstance(channel, str) or not TOOLCHAIN_RE.fullmatch(channel):
                return [f"rust-toolchain.toml's channel is {shown(channel)}. CI needs {hint}."]
            return []
        if here == top or top not in here.parents:
            break
        here = here.parent
    return [f"The repo has no rust-toolchain.toml. CI builds cargo plugins with the toolchain it names: {hint}."]


def parse_digests(text):
    """--digests: the build job's outputs as JSON, {platform: SHA-256}."""
    try:
        digests = json.loads(text)
    except ValueError:
        raise Refused(["--digests isn't JSON."]) from None
    if not isinstance(digests, dict) or not all(isinstance(k, str) and isinstance(d, str) for k, d in digests.items()):
        raise Refused(['--digests must be a JSON object of {"<platform>": "<SHA-256>"}.'])
    return digests


def assemble(root, id_, version, checkout, raw, folder, digests):
    """Makes the folder asp packs. Returns the files CI built in it.
    `digests` holds the SHA-256 each build job gave for what it uploaded:
    every job of a run can write artifacts, so a raw output counts only if
    it is still what its own build made."""
    v = load_version(root, id_, version)
    checkout, raw, folder = Path(checkout), Path(raw), Path(folder)
    source = checkout if v.path == "." else checkout / v.path
    step = checkout
    for part in [] if v.path == "." else v.path.split("/"):
        step = step / part
        if step.is_symlink():
            raise Refused([f"source.path {v.path} goes through a link. It must be a folder in the repo."])
    if not source.is_dir():
        raise Refused([f"The repo has no folder {v.path} at {v.commit}."])
    if folder.exists():
        raise Refused([f"{folder} is there already."])
    shutil.copytree(source, folder, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    if v.kind != "cargo":
        return []
    if (folder / "bin").exists() or (folder / "bin").is_symlink():
        raise Refused(["The plugin folder has bin/, where CI puts the programs it builds. Move it elsewhere."])
    built, problems = [], []
    for platform in CI_PLATFORMS:
        name = PurePosixPath(v.bins[platform]).name
        made = raw / f"raw-{v.id}-{v.version}-{platform}" / name
        if made.is_symlink() or not made.is_file():
            problems.append(f"The build for {platform} left no {shown(name)}.")
            continue
        want = digests.get(platform, "")
        if not re.fullmatch(r"[0-9a-f]{64}", want):
            problems.append(f"The build for {platform} gave no SHA-256 of what it made.")
            continue
        if sha256_file(made) != want:
            problems.append(
                f"The {shown(name)} downloaded for {platform} isn't what its build made (SHA-256 {want}). "
                "Another job of the run may have replaced it."
            )
            continue
        target = folder / "bin" / platform / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(made, target)
        target.chmod(0o755)
        built.append(f"bin/{platform}/{name}")
    if problems:
        raise Refused(problems)
    return sorted(built)


def read_member(z, name, cap):
    try:
        with z.open(name) as f:
            data = f.read(cap + 1)
    except KeyError:
        raise Refused([f"The package has no {name}."]) from None
    if len(data) > cap:
        raise Refused([f"The package's {name} is too large."])
    return data


def check_package(pkg, built, id_, version):
    """The checks after `asp pack` (R28)."""
    if not is_id(id_) or not is_version(version):
        raise Refused([f"{shown(id_)} {shown(version)} isn't a plugin id and a version."])
    try:
        with zipfile.ZipFile(pkg) as z:
            lock_bytes = read_member(z, "files.lock", MAX_LOCK_BYTES)
            manifest = parse_manifest(read_member(z, "plugin.json", MAX_MANIFEST_BYTES))
    except (zipfile.BadZipFile, OSError) as e:
        raise Refused([f"The package can't be read: {shown(str(e))}"]) from None
    try:
        lock = json.loads(lock_bytes.decode("utf-8"))
        files = lock["files"]
        marked = [f["path"] for f in files if f.get("exec")]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as e:
        raise Refused([f"The package's files.lock can't be read: {shown(str(e))}"]) from None
    problems = manifest_problems(manifest, id_, version)
    for path in marked:
        if path not in built:
            problems.append(
                f"files.lock marks {shown(path)} to run, but CI didn't build it. A file git marks executable "
                "counts too: drop that mark (git update-index --chmod=-x <file>)."
            )
    if problems:
        raise Refused(problems)


# The index ----------------------------------------------------------------------


def release_url(catalog, id_, version):
    """Where publish.yml puts the package: a release <id>@<version>."""
    tag = urllib.parse.quote(f"{id_}@{version}", safe="")
    asset = urllib.parse.quote(f"{id_}-{version}.asp", safe="")
    return f"https://github.com/{catalog}/releases/download/{tag}/{asset}"


def add_args(root, asp, index, pkg, catalog, v):
    """The `asp catalog add` command for the version `v`."""
    rel = f"plugins/{v.id}/plugin.toml"
    holder = check_holder(rel, read_text(root / rel, rel))
    args = [
        str(asp), "catalog", "add", str(index), str(pkg),
        "--url", release_url(catalog, v.id, v.version),
        "--repo", v.repo, "--commit", v.commit, "--path", v.path,
        "--author", holder["author"],
    ]
    if "authorKey" in holder:
        args += ["--author-key", holder["authorKey"]]
    return args


def parse_versions(text):
    try:
        items = json.loads(text)
    except ValueError:
        raise Refused(["--versions isn't JSON."]) from None
    if not isinstance(items, list) or not all(
        isinstance(i, dict) and is_id(i.get("id")) and is_version(i.get("version")) for i in items
    ):
        raise Refused(['--versions must be a JSON list of {"id", "version"}.'])
    return [(i["id"], i["version"]) for i in items]


# The command line -----------------------------------------------------------------


def take(args, name):
    """Removes `name <value>` from `args` and returns the value."""
    if name not in args:
        raise Refused([f"plan.py needs {name}."])
    i = args.index(name)
    if i + 1 >= len(args):
        raise Refused([f"plan.py needs a value after {name}."])
    value = args[i + 1]
    del args[i : i + 2]
    return value


def positional(args, count, usage):
    if len(args) != count or any(a.startswith("--") for a in args):
        raise Refused([f"usage: plan.py {usage}"])
    return args


def run(argv):
    args = list(argv)
    root = Path(take(args, "--root")).resolve() if "--root" in args else ROOT
    if not args:
        raise Refused(["usage: plan.py [--root <dir>] <command> …; see the top of plan.py."])
    command, args = args[0], args[1:]
    if command == "changes":
        base = take(args, "--base")
        head = take(args, "--head") if "--head" in args else "HEAD"
        positional(args, 0, "changes --base <rev> [--head <rev>]")
        versions, notes, index = changes(root, base, head)
        output("versions", json.dumps([{"id": v.id, "version": v.version} for v in versions], separators=(",", ":")))
        output("any", "true" if versions else "false")
        output("index", "true" if index else "false")
        text = ["## What this change adds to the catalog\n\n"]
        if versions:
            text.append("| Plugin | Version | Source | Build kind |\n|---|---|---|---|\n")
            for v in versions:
                text.append(f"| {markdown(v.id)} | {markdown(v.version)} | {source_link(v)} | {v.kind} |\n")
            text.append("\n")
        else:
            text.append("No new plugin versions.\n\n")
        text += [f"- {n}\n" for n in notes]
        summary("".join(text) + "\n")
    elif command == "settings":
        positional(args, 0, "settings")
        for name, value in load_settings(root).items():
            output(name, value)
    elif command == "show":
        id_, version = positional(args, 2, "show <id> <version>")
        v = load_version(root, id_, version)
        settings = load_settings(root)
        for name, value in (
            ("slug", v.slug), ("repo", v.repo), ("commit", v.commit), ("path", v.path),
            ("kind", v.kind), ("uv", settings["uv"]),
            ("matrix", json.dumps(v.matrix(), separators=(",", ":"))),
        ):
            output(name, value)
    elif command == "source":
        id_, version, checkout = positional(args, 3, "source <id> <version> <checkout>")
        scripts, notes = check_source(root, id_, version, checkout)
        output("scripts", " ".join(scripts))
        if notes:
            summary("### Package indexes\n\n" + "".join(f"- {n}\n" for n in notes) + "\n")
    elif command == "assemble":
        digests = parse_digests(take(args, "--digests"))
        id_, version, checkout, raw, folder, built = positional(
            args, 6, "assemble <id> <version> <checkout> <raw> <folder> <built> --digests <json>"
        )
        write_built(built, assemble(root, id_, version, checkout, raw, folder, digests))
    elif command == "block":
        title, source = positional(args, 2, "block <title> <file>")
        text = Path(source).read_bytes().decode("utf-8", "replace")
        block, body = fenced(title, text)
        summary(block)
        # The log too, with workflow commands off: the text is the author's.
        token = secrets.token_hex(16)
        print(f"::stop-commands::{token}\n{body}\n::{token}::", flush=True)
    elif command == "check-folder":
        id_, version, repo = take(args, "--id"), take(args, "--version"), take(args, "--repo")
        kind, folder, built = positional(args, 3, "check-folder <kind> <folder> <built> --id --version --repo")
        summary(check_folder(kind, folder, read_built(built), id_, version, repo))
    elif command == "check-package":
        id_, version = take(args, "--id"), take(args, "--version")
        pkg, built = positional(args, 2, "check-package <pkg.asp> <built> --id --version")
        check_package(pkg, set(read_built(built)), id_, version)
    elif command == "withdrawn":
        source, out = positional(args, 2, "withdrawn <withdrawn.toml> <out.json>")
        path = Path(source)
        items = check_withdrawn(read_text(path, "withdrawn.toml")) if path.exists() else []
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
            f.write("\n")
    elif command == "add":
        catalog = take(args, "--catalog")
        pairs = parse_versions(take(args, "--versions"))
        asp, index, packages = positional(args, 3, "add <asp> <index.json> <packages> --catalog --versions")
        if not SLUG_RE.fullmatch(catalog):
            raise Refused([f"--catalog must be <owner>/<repo>, not {shown(catalog)}."])
        for id_, version in pairs:
            v = load_version(root, id_, version)
            pkg = Path(packages) / f"asp-{id_}-{version}" / f"{id_}-{version}.asp"
            if not pkg.is_file():
                raise Refused([f"There is no package for {id_} {version} at {pkg}."])
            done = subprocess.run(add_args(root, asp, index, pkg, catalog, v), check=False)
            if done.returncode != 0:
                raise Refused([f"asp catalog add refused {id_} {version}."])
    else:
        raise Refused([f"plan.py has no command {shown(command)}; see the top of plan.py."])


def main(argv=None):
    try:
        run(sys.argv[1:] if argv is None else argv)
    except Refused as e:
        for problem in e.problems:
            print(f"::error::{annotation(problem)}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
