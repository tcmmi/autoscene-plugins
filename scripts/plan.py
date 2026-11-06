#!/usr/bin/env python3
"""The catalog's checks: what a pull request or a push adds, and whether a
plugin's folder is one CI may pack.

Standard library only (Python 3.11 or newer, for tomllib). The workflows run
it as `python3 -I scripts/plan.py <command>`; README.md and POLICY.md say the
same rules in words. Each command fails with exit code 1 and one `::error::`
line per broken rule, so a run shows all of them at once.

  changes --base <rev> [--head <rev>] [--listed <index.json>] [--unlisted]
      What the commits base..head change, by the rules of a pull request:
      new version files, a new or changed plugin.toml, withdrawn.toml and
      new epoch certificates. An approved version file never changes, a
      withdrawal is never taken back or narrowed, and site/ is CI's. A
      changed catalog.toml is a note naming the old and new asp.rev and uv.
      A base of forty zeros, or one that isn't an ancestor of head, is a
      first push: every version file is new then. With --listed, versions
      that index already lists are left out (publish.yml: a run again, or
      a first push over a catalog that has an index). With --unlisted
      (publish.yml), every version file at head that --listed doesn't list
      is planned too, changed in base..head or not: a version whose run
      stopped before its index was pushed is picked up by the next. Outputs
      `versions` (JSON: [{"id", "version"}]), `any` (some version is new),
      `index` (the index changes: new versions or withdrawn.toml) and
      `epoch` (a new epoch certificate).
  settings
      catalog.toml: outputs `uv`, `asp_rev` and `days` (15 to 31).
  show <id> <version>
      The version file and catalog.toml, as the build needs them: outputs
      `slug`, `repo`, `commit`, `path`, `kind`, `uv`, `matrix` (JSON),
      `folder` (where the build runs), `builds` (it makes a file), `wasm`,
      and for wasm-python `tool` (componentize-py as a hash-locked
      requirement), `python` and `requirements`.
  source <id> <version> <checkout>
      The author's source at the commit, before anything is built: the
      checks of check-folder that need no build and no download (resources
      too); each uv script's lock, whose
      packages come from a package index or a git repo only; for cargo a
      rust-toolchain.toml with a dated channel, and build.bins naming no file
      the repo holds; for wasm-rust the same with build.artifact, and a
      Cargo.lock; for wasm-python app.py, wit/ and a hash-locked requirements
      file. Outputs `scripts`, the uv scripts whose locks the build checks
      (space-separated); a package index other than PyPI goes to the job
      summary.
  assemble <id> <version> <checkout> <raw> <folder> <built> --digests <json>
      Makes the folder asp packs: the author's folder at the commit, without
      .git, plus each program CI built (from the `raw-*` artifacts in <raw>)
      at bin/<platform>/<name>, or the component CI built at the manifest's
      runtime.component, over anything the author committed there. Each is
      checked against the SHA-256 its build job gave (--digests, {platform:
      SHA-256}), and a component against build.sha256 when the version file
      has one. Writes the built files to <built>, one path per line.
  check-folder <kind> <folder> <built> --id <id> --version <v> --repo <url>
      Before `asp pack`: plugin.json names this id, version and repo; the
      build kind fits its launches; no file CI didn't build is a binary
      or an archive; and the folder holds at most 256 MiB. Each resource
      (a large file plugin.json names by URL, size and SHA-256) is fetched
      from its first URL with curl into hash-stream's check, never to disk,
      all within 20 minutes, and held to the same binary rule; a download
      that ends at a host or address that isn't public is refused without
      its size or hash.
      Writes to the job summary the files CI built (path, size, SHA-256),
      the resources (name, size, kind, hosts) and the author's data files
      (path, size, kind), each kind read from the file's first bytes.
  hash-stream <size> <sha256> [--name <name>]
      Reads stdin to its end, but never more than <size> + 1 bytes, and
      checks its size, its SHA-256 and the binary rule, the endings too with
      --name (curl … | plan.py hash-stream …). Outputs `kind`. The URL rules
      and where curl's redirects led are check-folder's.
  check-package <pkg.asp> <built> --id <id> --version <v>
      After `asp pack`: the package is this id and version, and its
      files.lock marks no file to run that CI didn't build.
  block <title> <file>
      Adds the text a tool printed (asp verify, asp catalog diff) to the job
      summary in a code fence nothing in it can close, and to the log with
      workflow commands off.
  withdrawn <withdrawn.toml> <out.json>
      withdrawn.toml as the JSON `asp catalog withdrawn` reads.
  add <asp> <index.json> <packages> --catalog <owner/repo>
      (--versions <json> | --released <json>)
      Runs `asp catalog add` for each version, with its release URL, its
      source and its holder from plugin.toml, after checking that the
      package is that id and version. A version the index already lists
      is left as it is. Each package is
      <packages>/asp-<id>-<version>/<id>-<version>.asp: with --versions
      ([{"id", "version"}], check.yml) it is the build's artifact; with
      --released ([{"id", "version", "sha256"}], publish.yml) it is
      downloaded from its release first, within 5 minutes and all of them
      within 40, and must have that SHA-256. asp's output goes to the log
      with workflow commands off.
  release-info <pkg.asp> <id> <version> <out.json> --run <url>
      Before a package is released: it is this id and version from the
      version file's repo. Writes JSON to <out.json>: tag, asset, title,
      notes and sha256.
  epoch <index.json>
      Outputs `certificate`: the highest epochs/epoch-<n>.minisig above the
      index's epoch, or nothing.
  message <previous.json | -> <index.json> <out.txt> --run <url>
      The commit message for a signed index: what it approves, withdraws
      or starts against the index before it (- for none), and its seq.

`--root <dir>` (before the command) names the catalog repo; by default it
is the one this script is in.
"""

import base64
import datetime
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent

ZERO = "0" * 40
WASM_KINDS = ("wasm-rust", "wasm-python")
KINDS = ("uv-script", "cargo", "files") + WASM_KINDS
# The platforms CI builds a cargo plugin for, and the runner of each.
CI_PLATFORMS = {
    "windows-x86_64": "windows-latest",
    "macos-aarch64": "macos-latest",
    "linux-x86_64": "ubuntu-22.04",
}
# Where uv locks are checked, and where a kind with no build per platform runs.
LINUX_BUILD = "ubuntu-22.04"
# Where a build job leaves a component (build-plugin.yml) and the name it has
# among the raw outputs.
COMPONENT_FILE = "plugin.wasm"
BUILT_COMPONENT = f"built/{COMPONENT_FILE}"
# The componentize-py versions CI builds with, each with the SHA-256 of its
# manylinux x86-64 wheel on PyPI. AutoScene's ci.yml and its templates pin the
# same version: a newer one can import more of WASI than `asp check` and the
# app accept, so it comes here after it passes there.
COMPONENTIZE_PY = {
    "0.25.1": "8623cd8cab8453cb0cd8bf03f3c7103420d06f2f3d360186a280a70a1ca5e89f",
}
# The Python that runs componentize-py and picks the plugin's wheels, as uv
# installs it. The Python inside the component is componentize-py's own
# (3.14.0 in 0.25.1), so this stays on its minor version.
BUILD_PYTHON = "3.14.10"
UV_FORM = "uv run --script --locked <script>.py"
# What asp pack leaves out of a folder at any depth, and beside the lock at
# its top (crates/as-plugin/src/package.rs, LEFT_OUT and BESIDE).
LEFT_OUT = (".git", ".hg", ".svn", ".ds_store", "thumbs.db")
# What else asp pack leaves out. The checks still read these: a binary there
# is refused, whatever asp does with it. A component can't be in one.
LEFT_OUT_BY_ASP = LEFT_OUT + (".gitignore", ".autoscene", ".vscode")
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
    (b"\x04\x22\x4d\x18", "an lz4 archive"),
    (b"\x02\x21\x4c\x18", "an lz4 archive"),
)
# Archives whose mark isn't at the start, is two letters that text may
# start with too, or is one byte (archive_magic).
BZIP2_RE = re.compile(rb"BZh[1-9](?:1AY&SY|\x17rE8P\x90)")
TAR_AT = 257
ZLIB_STARTS = (b"x\x01", b"x^", b"x\x9c", b"x\xda")
ARCHIVE_ENDINGS = (
    ".whl", ".egg", ".zip", ".jar", ".pyz", ".msi", ".cab", ".7z", ".rar",
    ".tar", ".tgz", ".gz", ".bz2", ".xz", ".zst", ".lz4", ".lzma",
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
# What a catalog package may hold (the app takes up to 1 GiB): more comes
# as resources.
MAX_PACKAGE_BYTES = 256 << 20
# The largest .asp the app takes, and so the most publish.yml downloads of
# a released package.
MAX_ASP_BYTES = 1 << 30
# The largest index the app reads.
MAX_INDEX_BYTES = 4 << 20
# How long publish.yml's prepare job waits for one released package, and
# for all of them (its timeout-minutes is 60).
PACKAGE_SECONDS = 5 * 60
ADD_SECONDS = 40 * 60
# How many bytes of a file its kind and the binary rule read (a tar's mark
# is at byte 257).
HEAD_BYTES = 512
# Resources: large files plugin.json names by URL, size and SHA-256, which
# AutoScene downloads at the first run (#23a, crates/as-plugin/src/manifest.rs).
MAX_RESOURCES = 16
MAX_RESOURCE_BYTES = 4 << 30
MAX_RESOURCES_BYTES = 8 << 30
MAX_RESOURCE_NAME_BYTES = 100
MAX_RESOURCE_URLS = 3
MAX_URL_CHARS = 2000
RESOURCE_KEYS = ("name", "urls", "sha256", "size")
# How long all of a plugin's downloads may take: the pack job has 30
# minutes, and checkouts, asp pack, verify and diff need the rest. 8 GiB
# arrive in time at about 7 MB/s.
DOWNLOAD_SECONDS = 20 * 60
# Endings as-models gives the files it keeps beside a download.
RESOURCE_SIDECARS = (".part", ".verified", ".lock", ".unpacked")
HUGGING_FACE = ("huggingface.co", "www.huggingface.co", "hf.co")
# Names that never reach the internet (LOCAL_ENDINGS in as-plugin's net.rs).
LOCAL_ENDINGS = ("localhost", "local", "lan", "internal", "localdomain", "test", "invalid", "example")
# The field tags that follow ir_version at the start of an ONNX ModelProto.
ONNX_NEXT = (0x12, 0x1A, 0x22, 0x28, 0x32, 0x3A, 0x42, 0x72)

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
SHA256_RE = re.compile(r"[0-9a-f]{64}")
# The parts of a line of a hash-locked requirements file (is_locked_pin).
PIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9._,-]+\])?==[A-Za-z0-9._+!-]+")
MARKER_RE = re.compile(r"[\w.<>=!~'\" ()]+")
HASH_RE = re.compile(r"--hash=sha256:[0-9a-f]{64}")
HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


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
    if len(p.encode("utf-8", "replace")) > MAX_PATH_BYTES:
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
    for magic, what in MAGICS:
        if head.startswith(magic):
            return what
    return archive_magic(head)


def archive_magic(head):
    """What archive `head`, a file's first HEAD_BYTES, starts, or None."""
    for magic, what in ARCHIVE_MAGICS:
        if head.startswith(magic):
            return what
    if head[TAR_AT : TAR_AT + 5] == b"ustar":
        return "a tar archive"
    # lzma-alone: 0x5d, then a dictionary size of 2^n or 2^n + 2^(n-1), as
    # liblzma takes it, so raw numbers that start with 93 aren't refused.
    if head[:1] == b"\x5d" and len(head) >= 5:
        dict_size = int.from_bytes(head[1:5], "little")
        top = dict_size.bit_length() - 1
        if dict_size >= 1 << 12 and dict_size in (1 << top, (1 << top) | (1 << (top - 1))):
            return "an lzma archive"
    if BZIP2_RE.match(head):
        return "a bzip2 archive"
    if head[:2] in ZLIB_STARTS and not is_text(head):
        return "zlib-compressed data (joblib writes it, around a pickle)"
    return None


def is_text(head):
    """Whether `head` reads as UTF-8 text (the last character may be cut)."""
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError as e:
        if e.end != len(head) or e.reason != "unexpected end of data":
            return False
        text = head[: e.start].decode("utf-8")
    return all(c.isprintable() or c in "\t\n\r\f" for c in text)


def data_kind(head):
    """What a data file is, by its first bytes, for the reviewer: zip,
    pickle, gzip, ONNX, safetensors, GGUF or other. author_binary refuses
    zip and gzip, so an accepted file is never one of those."""
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    if len(head) >= 2 and head[0] == 0x80 and 2 <= head[1] <= 5:
        return "pickle"  # protocol 2 and later start with PROTO
    if head.startswith(b"GGUF"):
        return "GGUF"
    if len(head) >= 9 and head[8:9] == b"{" and int.from_bytes(head[:8], "little") <= 100 << 20:
        return "safetensors"  # the header's length, then its JSON
    if len(head) >= 3 and head[0] == 0x08 and 1 <= head[1] <= 0x40 and head[2] in ONNX_NEXT:
        return "ONNX"  # ir_version, then another ModelProto field
    return "other"


def head_of(file):
    with open(file, "rb") as f:
        return f.read(HEAD_BYTES)


# Resources --------------------------------------------------------------------


@dataclass(frozen=True)
class Resource:
    """One entry of plugin.json's resources."""

    name: str
    urls: tuple
    sha256: str
    size: int

    def hosts(self):
        return sorted({url_host(url) for url in self.urls})


def url_host(url):
    """The host of an https:// URL that url_problem accepts."""
    return re.split(r"[/?#]", url[len("https://"):], maxsplit=1)[0].split(":")[0].lower()


def public_name(host):
    """Whether `host` is a public host name, never an address or a local
    name (net::public_name in as-plugin)."""
    labels = host.split(".")
    last = labels[-1]
    return (
        len(host) <= 253
        and len(labels) >= 2
        and all(HOST_LABEL_RE.fullmatch(label) for label in labels)
        and (last.isalpha() or re.fullmatch(r"xn--[a-z0-9-]+", last) is not None)
        and last not in LOCAL_ENDINGS
        and host != "home.arpa"
        and not host.endswith(".home.arpa")
    )


def url_problem(url):
    """Why `url` can't be a resource's URL, or None: a plain https:// address
    on port 443 with no user name, at a public host name, never an address
    (plain_link and net::public_name in as-plugin, design-i91 5.1 rule 5),
    and no . or .. in its path. A Hugging Face URL must pin a revision."""
    plain = (
        isinstance(url, str)
        and len(url) <= MAX_URL_CHARS
        and url.isascii()
        and not any(c.isspace() or not c.isprintable() or c == "\\" for c in url)
    )
    if not plain or not url.startswith("https://"):
        return "it must be a plain https:// address"
    address = re.split(r"[/?#]", url[len("https://"):], maxsplit=1)[0]
    host, colon, port = address.partition(":")
    host = host.lower()
    if not public_name(host):
        return "it must name a public host, with no user name, address or local name"
    if colon and port != "443":
        return "it may use port 443 only"
    # curl and AutoScene drop . and .. from a path (RFC 3986), so
    # /resolve/<commit>/../main/ asks for a branch.
    parts = re.split(r"[?#]", url, maxsplit=1)[0].split("/")[3:]
    if any(urllib.parse.unquote(part) in (".", "..") for part in parts):
        return "its path may not hold a . or .. part, written out or as %2e"
    if host in HUGGING_FACE:
        # <repo>/resolve/<revision>/<file>, the repo being [datasets/]<org>/<name>.
        at = parts.index("resolve") if "resolve" in parts else -1
        pinned = 1 <= at < len(parts) - 2 and COMMIT_RE.fullmatch(parts[at + 1]) and parts[-1]
        if not pinned:
            return (
                "it follows a branch of Hugging Face, so the file stops matching its hash when the branch "
                "moves. Pin a revision: https://huggingface.co/<repo>/resolve/<the 40-character commit id>/<file>"
            )
    return None


def plugin_resources(manifest):
    """plugin.json's resources, checked as AutoScene checks them (design-i91
    5.1), before anything is downloaded. Returns (resources, problems)."""
    if "resources" not in manifest:
        return [], []
    items = manifest["resources"]
    problems = []
    if manifest.get("manifestVersion") != 2:
        problems.append('plugin.json names resources, which need "manifestVersion": 2. '
                        "AutoScene skips them in a version 1 manifest.")
    runtime = manifest.get("runtime")
    if isinstance(runtime, dict) and runtime.get("type") == "none":
        problems.append("A plugin that runs no program has no use for resources.")
    if not isinstance(items, list):
        return [], problems + ["plugin.json's resources must be a list."]
    if len(items) > MAX_RESOURCES:
        problems.append(f"plugin.json names {len(items)} resources. A plugin has at most {MAX_RESOURCES}.")
    found, names = [], set()
    for i, item in enumerate(items[:MAX_RESOURCES]):
        if not isinstance(item, dict):
            problems.append(f"resources[{i}] must be an object with name, urls, sha256 and size.")
            continue
        name, urls, sha256, size = (item.get(k) for k in RESOURCE_KEYS)
        bad = len(problems)
        # The app only warns about another key; here it fails, so a typo
        # or a key a later AutoScene reads never passes unseen.
        for key in item:
            if key not in RESOURCE_KEYS:
                problems.append(
                    f"resources[{i}] has the key {shown(key)}, which AutoScene doesn't know. "
                    "A resource has name, urls, sha256 and size only."
                )
        if not isinstance(name, str) or "/" in name or path_problem(name):
            why = path_problem(name) if isinstance(name, str) and "/" not in name else "it must be one file name"
            problems.append(f"resources[{i}]'s name {shown(name)} can't be used ({why}).")
            name = f"resources[{i}]"
        elif len(name.encode()) > MAX_RESOURCE_NAME_BYTES:
            problems.append(f"The resource name {shown(name)} is longer than {MAX_RESOURCE_NAME_BYTES} bytes.")
        elif name.lower() in names:
            problems.append(f"Two resources are named {shown(name)}, ignoring case.")
        elif ending := next((e for e in RESOURCE_SIDECARS if name.lower().endswith(e)), None):
            problems.append(
                f"The resource name {shown(name)} ends in {ending}, which AutoScene keeps beside its downloads."
            )
        names.add(name.lower())
        if not isinstance(urls, list) or not 1 <= len(urls) <= MAX_RESOURCE_URLS:
            problems.append(f"The resource {shown(name)} needs 1 to {MAX_RESOURCE_URLS} urls.")
        else:
            for url in urls:
                if why := url_problem(url):
                    problems.append(f"The resource {shown(name)} has the URL {shown(url)}: {why}.")
        if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
            problems.append(f"The resource {shown(name)}'s sha256 must be 64 lowercase hex digits.")
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_RESOURCE_BYTES:
            problems.append(
                f"The resource {shown(name)}'s size must be a whole number of bytes, "
                f"1 to {MAX_RESOURCE_BYTES} ({MAX_RESOURCE_BYTES >> 30} GiB)."
            )
        if len(problems) == bad:
            found.append(Resource(name, tuple(urls), sha256, size))
    total = sum(r.size for r in found)
    if total > MAX_RESOURCES_BYTES:
        problems.append(
            f"The resources come to {total} bytes. A plugin's come to at most {MAX_RESOURCES_BYTES} "
            f"({MAX_RESOURCES_BYTES >> 30} GiB)."
        )
    return found, problems


def read_stream(stream, size):
    """Hashes `stream` to its end, but never more than size + 1 bytes.
    Returns (bytes read, SHA-256, the first HEAD_BYTES)."""
    h, head, got = hashlib.sha256(), b"", 0
    while got <= size:
        chunk = stream.read(min(1 << 20, size + 1 - got))
        if not chunk:
            break
        if len(head) < HEAD_BYTES:
            head += chunk[: HEAD_BYTES - len(head)]
        h.update(chunk)
        got += len(chunk)
    return got, h.hexdigest(), head


def stream_problems(what, got, digest, size, sha256):
    """Whether what read_stream read is `size` bytes with the SHA-256 `sha256`."""
    if got > size:
        return [f"{what} is larger than the {size} bytes plugin.json gives."]
    if got != size:
        return [f"{what} is {got} bytes, but plugin.json gives {size}."]
    if digest != sha256:
        return [f"{what} has the SHA-256 {digest}, but plugin.json gives {sha256}."]
    return []


def curl_args(url, size, seconds, mark):
    """curl as the pack job runs it for a resource: https on every hop, at
    most 10 redirects, at most `size` bytes, to stdout, for at most
    `seconds`. -q comes first, so no .curlrc applies; --globoff, so [] and
    {} are just characters. At the end it writes `mark`, the URL it ended
    at and that server's address to stderr (where_it_led)."""
    return [
        "curl", "-q", "--silent", "--show-error", "--fail", "--globoff",
        "--proto", "=https", "--proto-redir", "=https", "--location", "--max-redirs", "10",
        "--max-filesize", str(size), "--connect-timeout", "30", "--speed-limit", "102400", "--speed-time", "60",
        "--max-time", str(seconds),
        "--write-out", f"%{{stderr}}\\n{mark} %{{url_effective}} %{{remote_ip}}\\n",
        "--url", url,
    ]


def hop_problem(url, ip):
    """Why the URL a download ended at, after its redirects, and the address
    curl got it from aren't public, or None. curl follows a redirect to any
    https host; this keeps what a private server sent out of the messages."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return "which isn't a URL"
    if parts.scheme != "https" or port not in (None, 443) or not public_name(parts.hostname or ""):
        return "which isn't a public host name on https port 443"
    if ip:
        try:
            public = ipaddress.ip_address(ip).is_global
        except ValueError:
            public = False
        if not public:
            return "whose server isn't at a public address"
    return None


def where_it_led(text, mark):
    """curl's stderr split into (the URL it ended at, its address, curl's
    own words); the URL is None when curl wrote no line with `mark`."""
    url, ip, said = None, "", []
    for line in text.splitlines():
        if line.startswith(mark + " "):
            url, _, ip = line[len(mark) + 1 :].partition(" ")
        elif line.strip():
            said.append(line.strip())
    return url, ip.strip(), " ".join(said)


def curl_fetch(url, size, seconds):
    """Downloads `url` with curl straight into read_stream: nothing is
    written to disk. Returns what read_stream returns."""
    mark = secrets.token_hex(16)
    with tempfile.TemporaryFile() as err:
        try:
            curl = subprocess.Popen(
                curl_args(url, size, seconds, mark), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err
            )
        except OSError as e:
            raise Refused([f"curl can't be started ({shown(str(e))})"]) from None
        with curl:
            got, digest, head = read_stream(curl.stdout, size)
            if got > size:
                curl.kill()
            code = curl.wait()
        if got > size:
            return got, digest, head
        err.seek(0)
        led, ip, said = where_it_led(err.read(64 << 10).decode("utf-8", "replace"), mark)
    # Where it led comes first: nothing a private server sent, not even its
    # error, reaches the messages.
    if led is not None and (why := hop_problem(led, ip)):
        raise Refused([f"it led to {shown(led)}, {why}"])
    if code == 63:
        raise Refused([f"the server says it is larger than the {size} bytes plugin.json gives"])
    if code == 28:
        raise Refused([
            f"it didn't arrive in time. CI gives all of a plugin's downloads {DOWNLOAD_SECONDS // 60} minutes "
            "and stops one that sends less than 100 kB a second for a minute: use a faster host"
        ])
    if code != 0:
        raise Refused([f"{shown(said[:2000] or 'no reason given')} (curl's exit code {code})"])
    if led is None:
        raise Refused(["curl didn't say where the download led"])
    return got, digest, head


def fetch_resources(found, fetch):
    """Downloads each resource from its first URL with `fetch(url, size,
    seconds)`, all within DOWNLOAD_SECONDS, and checks its size, its SHA-256
    and the binary rule. Returns (rows, problems)."""
    rows, problems = [], []
    end = time.monotonic() + DOWNLOAD_SECONDS
    for r in found:
        what = f"The resource {shown(r.name)} from {shown(r.urls[0])}"
        seconds = int(end - time.monotonic())
        if seconds < 1:
            problems.append(
                f"{what} wasn't downloaded: the downloads before it took the {DOWNLOAD_SECONDS // 60} minutes "
                "CI gives all of a plugin's downloads. Use a faster host."
            )
            continue
        try:
            got, digest, head = fetch(r.urls[0], r.size, seconds)
        except Refused as e:
            problems += [f"{what} couldn't be downloaded: {p}." for p in e.problems]
            continue
        wrong = stream_problems(what, got, digest, r.size, r.sha256)
        if not wrong and (why := author_binary(r.name, head)):
            wrong.append(
                f"The resource {shown(r.name)} is {why}. A resource is a data file: like the files in the "
                "folder, it brings no programs, libraries or archives."
            )
        problems += wrong
        rows.append((r, data_kind(head)))
    return rows, problems


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
    artifact: str = ""
    sha256: str = ""
    componentize_py: str = ""
    requirements: str = ""

    @property
    def slug(self):
        return self.repo.removeprefix("https://github.com/")

    @property
    def file(self):
        return f"plugins/{self.id}/{self.version}.toml"

    @property
    def folder(self):
        """Where the build runs, from the repo's top: the plugin folder, and
        for a WebAssembly plugin the folder above it, where `asp init --wasm`
        puts Cargo.toml or app.py beside plugin/."""
        return PurePosixPath(self.path).parent.as_posix() if self.kind in WASM_KINDS else self.path

    def tool(self):
        """componentize-py as a line of a hash-locked requirements file."""
        if not self.componentize_py:
            return ""
        return f"componentize-py=={self.componentize_py} --hash=sha256:{COMPONENTIZE_PY[self.componentize_py]}"

    def built_paths(self):
        """Where `assemble` puts the programs CI builds: bin/<platform>/<name>."""
        return {f"bin/{p}/{PurePosixPath(b).name}" for p, b in self.bins.items()}

    def matrix(self):
        """The build jobs: one per platform for cargo, else one on Linux."""
        if self.kind == "cargo":
            return [
                {"platform": p, "os": os_, "bin": self.bins[p], "made": f"src/{self.bins[p]}",
                 "locks": p == "linux-x86_64"}
                for p, os_ in CI_PLATFORMS.items()
            ]
        made = BUILT_COMPONENT if self.kind in WASM_KINDS else ""
        return [{"platform": "any", "os": LINUX_BUILD, "bin": self.artifact, "made": made,
                 "locks": self.kind == "uv-script"}]


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
    keys = ("kind", "bins", "artifact", "sha256", "componentize_py", "requirements")
    known(build, keys, f"{where} [build]", problems)

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
    if kind not in KINDS:
        problems.append(f"{where}: build.kind must be one of {', '.join(KINDS)}, not {shown(kind)}.")
    bins = build.get("bins")
    if kind == "cargo":
        if not isinstance(bins, dict):
            problems.append(
                f"{where}: build kind cargo needs [build.bins], one entry for each of {', '.join(CI_PLATFORMS)}."
            )
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
    problems += wasm_build_problems(where, kind, build)
    if problems:
        raise Refused(problems)
    return Version(
        id_, version, repo, commit, path, kind, dict(bins or {}), build.get("artifact", ""), build.get("sha256", ""),
        build.get("componentize_py", ""), build.get("requirements", ""),
    )


def wasm_build_problems(where, kind, build):
    """The [build] keys of the WebAssembly kinds, and that no other kind has
    them. build.sha256 is the author's hash of a build that comes out the
    same every time, which a Python build doesn't."""
    problems = []
    artifact, sha256 = build.get("artifact"), build.get("sha256")
    tool, requirements = build.get("componentize_py"), build.get("requirements")
    if kind == "wasm-rust":
        why = "it must be a path" if not isinstance(artifact, str) else path_problem(artifact)
        if artifact is None:
            problems.append(f"{where}: build kind wasm-rust needs build.artifact: where cargo builds the .wasm file.")
        elif why:
            problems.append(f"{where}: build.artifact {shown(artifact)} can't be used ({why}).")
        elif not artifact.endswith(".wasm"):
            problems.append(f"{where}: build.artifact must be a .wasm file.")
        elif left := [part for part in artifact.split("/") if part.lower() in LEFT_OUT]:
            problems.append(
                f"{where}: build.artifact is in {shown(left[0])}/, which the checks and asp pack leave out. "
                "Name the file where cargo builds it, such as target/wasm32-wasip2/release/<name>.wasm."
            )
        if sha256 is not None and (not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256)):
            problems.append(f"{where}: build.sha256 must be 64 lowercase hex digits.")
    else:
        for key, value in (("artifact", artifact), ("sha256", sha256)):
            if value is not None:
                problems.append(f"{where}: build.{key} is for build kind wasm-rust only.")
    if kind == "wasm-python":
        if not isinstance(tool, str) or tool not in COMPONENTIZE_PY:
            problems.append(
                f"{where}: build.componentize_py must name a version CI builds with "
                f"({', '.join(COMPONENTIZE_PY)}), not {shown(tool)}."
            )
        if requirements is not None and (not isinstance(requirements, str) or path_problem(requirements)):
            why = path_problem(requirements) if isinstance(requirements, str) else "it must be a path"
            problems.append(f"{where}: build.requirements {shown(requirements)} can't be used ({why}).")
    else:
        for key, value in (("componentize_py", tool), ("requirements", requirements)):
            if value is not None:
                problems.append(f"{where}: build.{key} is for build kind wasm-python only.")
    return problems


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
    # resign.yml signs the index again once a week, and GitHub can start a
    # scheduled run late or drop it: with 15 days or more, a missed week
    # still leaves a week. AutoScene refuses an index that lives longer
    # than 31 days.
    days = data.get("days")
    if isinstance(days, bool) or not isinstance(days, int) or not 15 <= days <= 31:
        problems.append(
            "catalog.toml: days must be a whole number from 15 to 31: the index is signed again once a week, "
            "and a missed week must not let it run out."
        )
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


def changes(root, base, head="HEAD", listed=frozenset(), unlisted=False):
    """What base..head changes, checked by the rules of a pull request.
    Returns (versions, notes, index, epoch): the new versions, sorted,
    without those `listed` ((id, version) pairs an index already lists);
    lines for the job summary; whether the index changes; and whether an
    epoch certificate is new. With `unlisted`, every version file at head
    that `listed` doesn't name is a new version too."""
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
    holders, withdrawn, epoch = set(), False, False
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
                epoch = True
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

    if unlisted and not first:
        planned = {(v.id, v.version) for v in versions}
        again = []
        listing = git(root, "ls-tree", "-r", "-z", "--name-only", head, "--", "plugins").decode("utf-8", "replace")
        for path in listing.split("\0"):
            m = VERSION_FILE_RE.fullmatch(path)
            if not m or m.group(2) == "plugin" or (m.group(1), m.group(2)) in planned | set(listed):
                continue
            try:
                v = parse_version_file(path, blob(root, head, path) or "")
            except Refused as e:
                problems += e.problems
                continue
            versions.append(v)
            holders.add(v.id)
            again.append(v)
        if again:
            notes.append(
                "The index doesn't list " + ", ".join(f"{v.id} {v.version}" for v in again)
                + " yet: planned again, after a run that stopped."
            )

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
    if left := [v for v in versions if (v.id, v.version) in listed]:
        notes.append(
            "The index already lists " + ", ".join(f"{v.id} {v.version}" for v in left)
            + ": left as it is, not built again."
        )
        versions = [v for v in versions if (v.id, v.version) not in listed]
    return versions, notes, bool(versions) or withdrawn, epoch


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


def component_of(manifest):
    """The manifest's runtime.component, as it stands."""
    runtime = manifest.get("runtime")
    return runtime.get("component") if isinstance(runtime, dict) else None


def component_problem(component):
    """Why `component` can't be where CI puts a component, or None
    (wasm_component in as-plugin's manifest.rs, and what asp pack leaves out)."""
    if not isinstance(component, str):
        return "it must be a path"
    if why := path_problem(component):
        return why
    name = PurePosixPath(component).name
    if len(name) <= len(".wasm") or not name.endswith(".wasm"):
        return "it must be a .wasm file"
    if any(part.lower() in LEFT_OUT_BY_ASP for part in component.split("/")):
        return "asp pack leaves that folder out"
    return None


def kind_problems(kind, manifest, built):
    """Whether the build kind fits the manifest's launches (R28). `built` is
    the set of package paths CI builds."""
    runtime = manifest.get("runtime")
    rtype = runtime.get("type") if isinstance(runtime, dict) else None
    named = "missing" if rtype is None else shown(rtype)
    if kind in WASM_KINDS:
        if rtype != "wasm":
            return [f'Build kind {kind} is for a WebAssembly plugin (runtime.type "wasm"), '
                    f"but this one's runtime.type is {named}."]
        component = component_of(manifest)
        if why := component_problem(component):
            return [f"runtime.component {shown(component)} can't be used ({why}). "
                    "It names the file CI puts in the plugin folder, such as plugin.wasm."]
        if component not in built:
            return [f"runtime.component is {shown(component)}, which CI didn't build."]
        return []
    if rtype == "wasm":
        return [f'runtime.type "wasm" needs build kind {" or ".join(WASM_KINDS)}, not {kind}.']
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
        if why := author_binary(PurePosixPath(path).name, head_of(file)):
            problems.append(
                f"{shown(path)} is {why}. A plugin's folder brings no programs, libraries or archives: "
                "CI builds every program itself, and no one can review what is inside an archive."
            )
        elif cargo and path.startswith("bin/"):
            problems.append(f"{shown(path)} is in bin/, where CI puts the programs it builds. Move it elsewhere.")
    return problems


def size_problems(folder):
    """A catalog package holds at most MAX_PACKAGE_BYTES."""
    files, _ = folder_files(folder)
    total = sum(file.stat().st_size for _, file in files)
    if total <= MAX_PACKAGE_BYTES:
        return []
    return [
        f"The plugin folder holds {total} bytes, more than the {MAX_PACKAGE_BYTES >> 20} MiB a catalog package "
        "may hold. Move large files to resources: plugin.json names each by URL, size and SHA-256, and "
        "AutoScene downloads it at the first run."
    ]


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


def check_folder(kind, folder, built, id_, version, repo, fetch=curl_fetch):
    """The checks before `asp pack` (R28, design-i91 5.6). Returns the
    summary Markdown. `fetch(url, size)` downloads a resource into
    read_stream; tests pass their own."""
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
    problems += size_problems(folder)
    found, resource_problems = plugin_resources(manifest)
    problems += resource_problems
    if problems:
        raise Refused(problems)  # before downloading anything
    rows, problems = fetch_resources(found, fetch)
    if problems:
        raise Refused(problems)
    lines = sandbox_summary(manifest) if kind in WASM_KINDS else []
    lines.append("### Files CI built\n\n")
    if built:
        lines.append("| File | Size (bytes) | SHA-256 |\n|---|---:|---|\n")
        for path in built:
            file = folder / path
            lines.append(f"| {markdown(path)} | {file.stat().st_size} | `{sha256_file(file)}` |\n")
    else:
        lines.append("None: the package holds the author's folder as it is at the commit.\n")
    kinds = [data for _, data in rows]
    if rows:
        lines.append(
            "\n### Files it downloads (resources)\n\n"
            "CI downloaded each from its first URL in this run, and each has the size and SHA-256 that "
            "plugin.json gives. The kind is read from the file's first bytes.\n\n"
            "| Name | Size (bytes) | Kind | Hosts |\n|---|---:|---|---|\n"
        )
        for r, data in rows:
            lines.append(f"| {markdown(r.name)} | {r.size} | {data} | {', '.join(markdown(h) for h in r.hosts())} |\n")
    files, _ = folder_files(folder)
    data_files = []
    for path, file in files:
        if path not in built and (data := data_kind(head_of(file))) != "other":
            data_files.append(f"| {markdown(path)} | {file.stat().st_size} | {data} |\n")
            kinds.append(data)
    lines.append("\n### The author's data files\n\n")
    if data_files:
        lines.append("| File | Size (bytes) | Kind |\n|---|---:|---|\n")
        lines += data_files
    else:
        lines.append("None of the author's files starts like a pickle, ONNX, safetensors or GGUF file.\n")
    if "pickle" in kinds:
        lines.append(
            "\nA pickle runs code when it is loaded. The pull request must say why it is one, and the plugin's "
            "code must load it without running it (`torch.load(..., weights_only=True)`, never `pickle.load`).\n"
        )
    return "".join(lines) + "\n"


def sandbox_summary(manifest):
    """What the job summary says of a WebAssembly plugin: AutoScene enforces
    what it asks for. The text is the author's."""
    def listed_in(value):
        names = [markdown(v) for v in value if isinstance(v, str)] if isinstance(value, list) else []
        return ", ".join(names) or "none"

    network = manifest.get("network")
    return [
        "### Runs in a sandbox\n\n",
        "AutoScene enforces what this plugin asks for: it reaches nothing else.\n\n",
        f"- Permissions: {listed_in(manifest.get('permissions'))}\n",
        f"- Hosts: {listed_in(network.get('hosts') if isinstance(network, dict) else None)}\n\n",
    ]


def built_path_problems(checkout, key, paths, example):
    """`key` (build.bins, build.artifact) must name where cargo puts a file,
    not one the repo holds: the build takes that path, and a committed file
    there would ship as if CI had built it."""
    problems = []
    for path in sorted(set(paths)):
        step = checkout
        for part in path.split("/"):
            step = step / part
            if step.is_symlink():
                problems.append(f"{key} names {shown(path)}, which goes through a link in the repo.")
                break
        else:
            if step.exists():
                problems.append(
                    f"{key} names {shown(path)}, which the repo holds at the commit. It must be where "
                    f"cargo builds it ({example}), and nothing there may be committed."
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
    # CI's component goes over anything committed at the manifest's path.
    component = component_of(manifest)
    replaced = {component} if v.kind in WASM_KINDS and isinstance(component, str) else set()
    problems += kind_problems(v.kind, manifest, replaced or v.built_paths())
    problems += binary_problems(folder, replaced, False)
    problems += size_problems(folder)
    problems += plugin_resources(manifest)[1]
    if v.kind == "cargo":
        if (folder / "bin").exists() or (folder / "bin").is_symlink():
            problems.append("The plugin folder has bin/, where CI puts the programs it builds. Move it elsewhere.")
        problems += toolchain_problems(checkout, folder)
        problems += built_path_problems(checkout, "build.bins", v.bins.values(), "target/release/<name>")
    if v.kind in WASM_KINDS:
        problems += wasm_source_problems(checkout, v, component)
    scripts = [] if v.kind == "files" else uv_scripts(manifest)
    lock_problems, notes = uv_lock_problems(folder, scripts)
    problems += lock_problems
    if problems:
        raise Refused(problems)
    return scripts, notes


def wasm_source_problems(checkout, v, component):
    """What a WebAssembly build needs from the repo, before anything of the
    author's runs. `component` is the manifest's runtime.component."""
    problems = [] if component_problem(component) else component_path_problems(checkout / v.path, component)
    folder = checkout / v.folder
    if v.kind == "wasm-rust":
        if not (folder / "Cargo.toml").is_file():
            problems.append(
                f"{v.folder}/Cargo.toml is missing. CI builds in the folder above the plugin folder, "
                "where asp init --wasm rust puts the crate."
            )
        here, top = folder.resolve(), checkout.resolve()
        while not (here / "Cargo.lock").is_file():
            if here == top or top not in here.parents:
                problems.append("The repo has no Cargo.lock for the crate. CI builds with --locked: commit it.")
                break
            here = here.parent
        problems += toolchain_problems(checkout, folder)
        problems += built_path_problems(checkout, "build.artifact", [v.artifact],
                                        "target/wasm32-wasip2/release/<name>.wasm")
    else:
        if not (folder / "app.py").is_file() or not (folder / "wit").is_dir():
            problems.append(
                f"{v.folder} needs app.py and wit/. CI builds in the folder above the plugin folder, "
                "where asp init --wasm python puts them."
            )
        if v.requirements:
            problems += requirements_problems(checkout, v.requirements)
    return problems


def component_path_problems(folder, component):
    """Whether CI's component can go to `component` in `folder`: no link on
    the way, nor a file where a folder must be. A link or a file at the path
    itself is replaced, never written through."""
    step = folder
    for part in component.split("/")[:-1]:
        step = step / part
        if step.is_symlink() or (step.exists() and not step.is_dir()):
            return [f"runtime.component {shown(component)} goes through {shown(part)}, which isn't a folder."]
    target = folder / component
    if target.is_dir() and not target.is_symlink():
        return [f"runtime.component {shown(component)} is a folder in the repo. It must be free for CI's file."]
    return []


def is_locked_pin(line):
    """Whether `line` is one requirement as `uv pip compile --generate-hashes`
    writes it: a pinned name from the package index, an optional marker, and
    its hashes. No options, URLs or paths. In plain steps, so a long line
    costs no backtracking."""
    words = line.split()
    hashes = 0
    while hashes < len(words) and HASH_RE.fullmatch(words[-1 - hashes]):
        hashes += 1
    if not hashes:
        return False
    pin, semicolon, marker = " ".join(words[:-hashes]).partition(";")
    return PIN_RE.fullmatch(pin.strip()) is not None and (
        not semicolon or MARKER_RE.fullmatch(marker.strip()) is not None
    )


def requirements_problems(checkout, requirements):
    """build.requirements must be a file of the repo with only hash-locked
    pins (is_locked_pin): `uv pip install --require-hashes` alone would still
    take options, URLs and paths. Whatever a reader might take for the end of
    a line or a comment is refused, so what passes here is what uv reads."""
    where = shown(requirements)
    step = checkout
    for part in requirements.split("/"):
        step = step / part
        if step.is_symlink():
            return [f"build.requirements names {where}, which goes through a link in the repo."]
    if not step.is_file():
        return [f"build.requirements names {where}, which the repo doesn't hold at the commit."]
    data = step.read_bytes()
    if len(data) > MAX_UV_LOCK_BYTES:
        return [f"{where} is larger than {MAX_UV_LOCK_BYTES >> 20} MiB."]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [f"{where} isn't UTF-8 text."]
    problems, pending = [], ""
    for raw in text.split("\n"):
        raw = raw.removesuffix("\r")
        if not re.fullmatch(r"[\t\x20-\x7e]*", raw):
            # uv ends a comment at a bare \r too, and what came after would count.
            problems.append(f"{where} holds a character that isn't plain ASCII text: {shown(raw, 100)}.")
            continue
        comment = re.search(r"(?:^|[ \t])#", raw)
        code = raw[: comment.start()] if comment else raw
        if comment and raw.endswith("\\"):
            # Readers differ on whether such a line goes on: the next one
            # could pass here as a comment and count as an option there.
            problems.append(f"{where} has a comment that ends in a backslash: {shown(raw, 100)}.")
        elif code.endswith("\\"):
            pending += code[:-1] + " "
            continue
        line = (pending + code).strip(" \t")
        pending = ""
        if line and not is_locked_pin(line):
            problems.append(
                f"{where} holds {shown(line, 100)}. Each line must be <name>==<version> with its "
                "--hash=sha256: values, as uv pip compile --generate-hashes writes them: no options, URLs or paths."
            )
    if pending.strip():
        problems.append(f"{where} ends in the middle of a line.")
    return problems


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
    if v.kind in WASM_KINDS:
        return [assemble_component(v, raw, folder, digests)]
    if v.kind != "cargo":
        return []
    if (folder / "bin").exists() or (folder / "bin").is_symlink():
        raise Refused(["The plugin folder has bin/, where CI puts the programs it builds. Move it elsewhere."])
    built, problems = [], []
    for platform in CI_PLATFORMS:
        name = PurePosixPath(v.bins[platform]).name
        made, why = raw_output(v, raw, platform, name, digests)
        if why:
            problems.append(why)
            continue
        target = folder / "bin" / platform / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(made, target)
        target.chmod(0o755)
        built.append(f"bin/{platform}/{name}")
    if problems:
        raise Refused(problems)
    return sorted(built)


def raw_output(v, raw, platform, name, digests):
    """The file `name` a build job left among the raw outputs, or why it
    doesn't count. Returns (path, problem)."""
    build = "The build" if platform == "any" else f"The build for {platform}"
    made = raw / f"raw-{v.id}-{v.version}-{platform}" / name
    if made.is_symlink() or not made.is_file():
        return None, f"{build} left no {shown(name)}."
    want = digests.get(platform, "")
    if not re.fullmatch(r"[0-9a-f]{64}", want):
        return None, f"{build} gave no SHA-256 of what it made."
    if sha256_file(made) != want:
        downloaded = "downloaded" if platform == "any" else f"downloaded for {platform}"
        return None, (
            f"The {shown(name)} {downloaded} isn't what its build made (SHA-256 {want}). "
            "Another job of the run may have replaced it."
        )
    return made, None


def assemble_component(v, raw, folder, digests):
    """Copies the component CI built to the manifest's runtime.component,
    over anything the author committed there (R28), and never through a
    link: the folder is the author's, and this job goes on to run plan.py.
    Returns its path in the folder."""
    manifest = read_manifest(folder)
    component = component_of(manifest)
    if why := component_problem(component):
        raise Refused([f"runtime.component {shown(component)} can't be used ({why})."])
    made, why = raw_output(v, raw, "any", COMPONENT_FILE, digests)
    if why:
        raise Refused([why])
    if v.sha256 and (got := sha256_file(made)) != v.sha256:
        raise Refused([
            f"CI built a component with the SHA-256 {got}, but build.sha256 is {v.sha256}. "
            "A Rust build comes out the same with the same source, toolchain and lock: see README.md."
        ])
    if problems := component_path_problems(folder, component):
        raise Refused(problems)
    target = folder / component
    if target.is_symlink() or target.exists():
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(made, target)
    target.chmod(0o644)
    return component


def read_member(z, name, cap):
    try:
        with z.open(name) as f:
            data = f.read(cap + 1)
    except KeyError:
        raise Refused([f"The package has no {name}."]) from None
    if len(data) > cap:
        raise Refused([f"The package's {name} is too large."])
    return data


def read_package(pkg, id_, version):
    """The package's files.lock (bytes) and plugin.json (a dict), after
    checking that `id_` and `version` are a plugin id and a version."""
    if not is_id(id_) or not is_version(version):
        raise Refused([f"{shown(id_)} {shown(version)} isn't a plugin id and a version."])
    try:
        with zipfile.ZipFile(pkg) as z:
            lock_bytes = read_member(z, "files.lock", MAX_LOCK_BYTES)
            manifest = parse_manifest(read_member(z, "plugin.json", MAX_MANIFEST_BYTES))
    except (zipfile.BadZipFile, OSError) as e:
        raise Refused([f"The package can't be read: {shown(str(e))}"]) from None
    return lock_bytes, manifest


def check_package(pkg, built, id_, version):
    """The checks after `asp pack` (R28)."""
    lock_bytes, manifest = read_package(pkg, id_, version)
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


def parse_versions(text, flag="--versions"):
    """[(id, version, sha256)] from --versions ([{"id", "version"}], sha256
    None) or --released (each with its "sha256")."""
    try:
        items = json.loads(text)
    except ValueError:
        raise Refused([f"{flag} isn't JSON."]) from None
    released = flag == "--released"
    if not isinstance(items, list) or not all(
        isinstance(i, dict) and is_id(i.get("id")) and is_version(i.get("version"))
        and (not released or (isinstance(i.get("sha256"), str) and SHA256_RE.fullmatch(i["sha256"])))
        for i in items
    ):
        shape = '{"id", "version", "sha256"}' if released else '{"id", "version"}'
        raise Refused([f"{flag} must be a JSON list of {shape}."])
    return [(i["id"], i["version"], i["sha256"] if released else None) for i in items]


def read_index(path):
    """An index.json, with what this script reads from it checked: epoch and
    seq are whole numbers, plugins a list of entries with an id and a
    version, withdrawn a list."""
    where = shown(str(path))
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise Refused([f"{where} is missing."])
    data = path.read_bytes()
    if len(data) > MAX_INDEX_BYTES:
        raise Refused([f"{where} is larger than {MAX_INDEX_BYTES >> 20} MiB."])
    try:
        index = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise Refused([f"{where} isn't JSON: {shown(str(e))}"]) from None
    if not isinstance(index, dict):
        raise Refused([f"{where} isn't an index: it must be a JSON object."])
    problems = []
    for key in ("epoch", "seq"):
        value = index.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            problems.append(f"{where}: {key} must be a whole number.")
    plugins = index.get("plugins", [])
    if not isinstance(plugins, list) or not all(
        isinstance(p, dict) and isinstance(p.get("id"), str) and isinstance(p.get("version"), str) for p in plugins
    ):
        problems.append(f"{where}: plugins must be a list of entries, each with an id and a version.")
    if not isinstance(index.get("withdrawn", []), list):
        problems.append(f"{where}: withdrawn must be a list.")
    if problems:
        raise Refused(problems)
    return index


def listed(index):
    """The (id, version) pairs an index lists."""
    return {(p["id"], p["version"]) for p in index.get("plugins", [])}


def epoch_certificate(root, index):
    """The highest epochs/epoch-<n>.minisig above the index's epoch, as a
    path from the catalog's top, or "" when there is none."""
    folder = Path(root) / "epochs"
    found = []
    if folder.is_dir() and not folder.is_symlink():
        for file in folder.iterdir():
            m = EPOCH_RE.fullmatch(f"epochs/{file.name}")
            if m and not file.is_symlink() and file.is_file() and int(m.group(1)) > index["epoch"]:
                found.append(int(m.group(1)))
    return f"epochs/epoch-{max(found)}.minisig" if found else ""


def run_link(run):
    if not isinstance(run, str) or not re.fullmatch(r"https://[!-~]{1,500}", run):
        raise Refused([f"--run must be the run's https URL, not {shown(run)}."])
    return run


def release_info(root, pkg, id_, version, run):
    """What publish.yml needs to release a package, after checking that it
    is this id and version, from the version file's repo."""
    run = run_link(run)
    v = load_version(root, id_, version)
    _, manifest = read_package(pkg, id_, version)
    if problems := manifest_problems(manifest, id_, version, v.repo):
        raise Refused(problems)
    name = manifest.get("name")
    name = shown(name.strip(), 100) if isinstance(name, str) and name.strip() else id_
    return {
        "tag": f"{id_}@{version}",
        "asset": f"{id_}-{version}.asp",
        "title": f"{name} {version}",
        "notes": f"Built from {v.repo}@{v.commit} by {run}.",
        "sha256": sha256_file(pkg),
    }


def download_package(url, dest, seconds=PACKAGE_SECONDS):
    """A released package, with curl: https only, redirects included, at
    most the largest .asp the app takes, and within `seconds`, retries
    included (curl is stopped 30 seconds after)."""
    seconds = max(1, int(seconds))
    try:
        done = subprocess.run(
            [
                "curl", "-q", "--silent", "--show-error", "--fail", "--location",
                "--proto", "=https", "--proto-redir", "=https", "--max-redirs", "10",
                "--max-filesize", str(MAX_ASP_BYTES), "--max-time", str(seconds),
                "--retry", "2", "--retry-all-errors", "--retry-max-time", str(seconds),
                "--output", str(dest), url,
            ],
            capture_output=True,
            check=False,
            timeout=seconds + 30,
        )
    except subprocess.TimeoutExpired:
        raise Refused([f"{url} wasn't downloaded within {seconds} seconds."]) from None
    if done.returncode != 0:
        said = shown(done.stderr.decode("utf-8", "replace").strip())
        raise Refused([f"{url} can't be downloaded (curl exit {done.returncode}): {said}"])


def tool(args, refusal):
    """Runs a tool whose output may hold an author's texts: into the log
    with workflow commands off, so no name it prints can start one."""
    done = subprocess.run(args, capture_output=True, check=False)
    text = (done.stdout + done.stderr).decode("utf-8", "replace")
    token = secrets.token_hex(16)
    print(f"::stop-commands::{token}\n{text.rstrip()}\n::{token}::", flush=True)
    if done.returncode != 0:
        raise Refused([refusal])


def add_packages(root, asp, index, packages, catalog, items):
    """`asp catalog add` for each of `items`, (id, version, sha256 or None).
    With a SHA-256 the package is downloaded from its release first and must
    have it; without, it is the build's artifact in `packages`. Either way it
    must be that id and version, from the version file's repo. A version the
    index already lists is left as it is (a run again). All the downloads
    together take at most ADD_SECONDS."""
    if not SLUG_RE.fullmatch(catalog):
        raise Refused([f"--catalog must be <owner>/<repo>, not {shown(catalog)}."])
    already = listed(read_index(index))
    deadline = time.monotonic() + ADD_SECONDS
    for id_, version, sha256 in items:
        if (id_, version) in already:
            print(f"The index already lists {id_} {version}: left as it is.", flush=True)
            continue
        v = load_version(root, id_, version)
        url = release_url(catalog, id_, version)
        pkg = Path(packages) / f"asp-{id_}-{version}" / f"{id_}-{version}.asp"
        if sha256 is not None:
            pkg.parent.mkdir(parents=True, exist_ok=True)
            left = deadline - time.monotonic()
            if left < 60:
                raise Refused([f"The downloads took longer than {ADD_SECONDS // 60} minutes; {id_} {version} wasn't."])
            download_package(url, pkg, min(PACKAGE_SECONDS, left - 30))
            if (got := sha256_file(pkg)) != sha256:
                raise Refused([f"{url} has the SHA-256 {got}, not {sha256}, which the release job checked."])
        elif not pkg.is_file():
            raise Refused([f"There is no package for {id_} {version} at {pkg}."])
        _, manifest = read_package(pkg, id_, version)
        if problems := manifest_problems(manifest, id_, version, v.repo):
            raise Refused(problems)
        tool(add_args(root, asp, index, pkg, catalog, v), f"asp catalog add refused {id_} {version}.")


def message(previous, index, run):
    """The commit message for `index`, signed after `previous` (None for the
    first index): what it approves, withdraws or starts, and its seq."""
    run = run_link(run)
    added = sorted(listed(index) - (listed(previous) if previous else set()))
    seq, epoch = index["seq"], index["epoch"]
    new_epoch = epoch != (previous["epoch"] if previous else 1)
    withdrawn = index.get("withdrawn", [])
    new_withdrawn = withdrawn != (previous.get("withdrawn", []) if previous else [])
    if len(added) == 1:
        title = f"catalog: {shown(added[0][0])} {shown(added[0][1])} approved (seq {seq})"
    elif added:
        title = f"catalog: {len(added)} versions approved (seq {seq})"
    elif new_epoch:
        title = f"catalog: epoch {epoch} starts (seq {seq})"
    elif new_withdrawn:
        title = f"catalog: withdrawals signed (seq {seq})"
    else:
        title = f"catalog: signed again (seq {seq})"
    body = []
    if added:
        body += ["Approved:"] + [f"- {shown(i)} {shown(v)}" for i, v in added] + [""]
    if new_withdrawn:
        count = f"{len(withdrawn)} withdrawal" + ("" if len(withdrawn) == 1 else "s")
        body += [f"withdrawn.toml changed: the index lists {count} now.", ""]
    if new_epoch:
        body += [f"Epoch {epoch} starts, with epochs/epoch-{epoch}.minisig.", ""]
    return "\n".join([title, ""] + body + [f"Signed by {run}"]) + "\n"


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
        known_ones = listed(read_index(take(args, "--listed"))) if "--listed" in args else frozenset()
        unlisted = "--unlisted" in args
        if unlisted:
            args.remove("--unlisted")
        positional(args, 0, "changes --base <rev> [--head <rev>] [--listed <index.json>] [--unlisted]")
        versions, notes, index, epoch = changes(root, base, head, known_ones, unlisted)
        output("versions", json.dumps([{"id": v.id, "version": v.version} for v in versions], separators=(",", ":")))
        output("any", "true" if versions else "false")
        output("index", "true" if index else "false")
        output("epoch", "true" if epoch else "false")
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
            ("folder", v.folder), ("builds", "true" if v.kind == "cargo" or v.kind in WASM_KINDS else "false"),
            ("wasm", "true" if v.kind in WASM_KINDS else "false"), ("tool", v.tool()),
            ("python", BUILD_PYTHON if v.kind == "wasm-python" else ""), ("requirements", v.requirements),
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
    elif command == "hash-stream":
        name = take(args, "--name") if "--name" in args else ""
        size, sha256 = positional(args, 2, "hash-stream <size> <sha256> [--name <name>]")
        if not re.fullmatch(r"[1-9][0-9]{0,11}", size) or int(size) > MAX_RESOURCE_BYTES:
            raise Refused([f"The size must be 1 to {MAX_RESOURCE_BYTES} bytes, not {shown(size)}."])
        if not SHA256_RE.fullmatch(sha256):
            raise Refused([f"The SHA-256 must be 64 lowercase hex digits, not {shown(sha256)}."])
        got, digest, head = read_stream(sys.stdin.buffer, int(size))
        if problems := stream_problems("The file read from stdin", got, digest, int(size), sha256):
            raise Refused(problems)
        if why := author_binary(name, head):
            raise Refused([
                f"The file read from stdin is {why}. A resource is a data file: like the files in the "
                "folder, it brings no programs, libraries or archives."
            ])
        output("kind", data_kind(head))
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
        if ("--versions" in args) == ("--released" in args):
            raise Refused(["plan.py add needs one of --versions and --released."])
        flag = "--versions" if "--versions" in args else "--released"
        items = parse_versions(take(args, flag), flag)
        asp, index, packages = positional(
            args, 3, "add <asp> <index.json> <packages> --catalog (--versions | --released)"
        )
        add_packages(root, asp, index, packages, catalog, items)
    elif command == "release-info":
        run = take(args, "--run")
        pkg, id_, version, out = positional(args, 4, "release-info <pkg.asp> <id> <version> <out.json> --run <url>")
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(release_info(root, pkg, id_, version, run), f, ensure_ascii=False)
            f.write("\n")
    elif command == "epoch":
        (index,) = positional(args, 1, "epoch <index.json>")
        output("certificate", epoch_certificate(root, read_index(index)))
    elif command == "message":
        run = take(args, "--run")
        previous, index, out = positional(args, 3, "message <previous.json | -> <index.json> <out.txt> --run <url>")
        text = message(None if previous == "-" else read_index(previous), read_index(index), run)
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
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
