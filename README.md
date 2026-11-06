# AutoScene plugin catalog

This repo is the list of plugins that AutoScene shows under
**Settings → Plugins → Browse**. Every plugin here is built by this repo's CI
from public source at a fixed commit, read by a person, and signed into one
index that the app checks.

The owner is [tcmmi](https://github.com/tcmmi). How to write a plugin is in
AutoScene's guide, `docs/PLUGINS.md` in
[tcmmi/AutoScene](https://github.com/tcmmi/AutoScene).

## For people who install plugins

### What "Reviewed by AutoScene" means

AutoScene calls a plugin **Reviewed** when this catalog's signed index lists
that exact version (its id, its version and the SHA-256 of its `files.lock`),
the index is current (its signature is not past its 30 days), and the index
doesn't withdraw that version. Change one file in the package and it is no
longer the version the catalog lists.

Before a version gets into the index:

- Its source is public on GitHub, at a fixed commit.
- CI built the package from that commit. No one uploads a finished build.
- A person read the whole change since the last approved version and what the
  plugin asks for (permissions and network hosts). For `uv-script` plugins
  that includes the uv lock; for `cargo` and `wasm-rust` plugins, `Cargo.lock`
  and any `build.rs`; for `wasm-python` plugins, the requirements file.

What it doesn't mean:

- It is not a promise that a plugin has no bugs.
- A plugin that runs a program runs it with your rights, as any program you
  start does. Review lowers the risk. It doesn't take it away. A WebAssembly
  plugin runs in AutoScene's sandbox instead, which enforces its permissions
  and hosts.

### Withdrawn versions

If a version turns out to be harmful (malware, a data leak, broken security),
it is withdrawn. The index says which versions and why, in plain words. A
withdrawn version stops running, the app offers to uninstall or update it, and
it can't be installed again.

Apps that check daily (the default) learn of a withdrawal within a day of it
being published. **Check now** picks it up as soon as it is published.

### How the index stays fresh

- The index lives at `https://tcmmi.github.io/autoscene-plugins/index.json`,
  with its signature beside it (`index.json.minisig`).
- CI signs it again every week. A signature counts for 30 days, so a missed
  week does no harm.
- Each package is attached to a GitHub release named `<id>@<version>` in this
  repo, with a build attestation. To check one yourself:

  ```
  gh attestation verify <file>.asp --repo tcmmi/autoscene-plugins \
    --signer-workflow tcmmi/autoscene-plugins/.github/workflows/build-plugin.yml \
    --source-ref refs/heads/main --deny-self-hosted-runners
  ```

  That proves the `.asp` came out of `build-plugin.yml` on this repo's `main`,
  built from the source commit its version file names.

## For plugin authors

### Before you submit

- Your plugin's source is in a public repo on `https://github.com/`.
- Its `plugin.json` names that repo as `repository`.
- Its id is a reverse-DNS name you control: your domain, or
  `io.github.<your-user>`. For example `io.github.alice.clip-renamer`.
- Read [POLICY.md](POLICY.md). It is short.

### What a build kind is

The build kind tells CI how to make your package:

| Kind | For | What CI does |
|---|---|---|
| `uv-script` | Python plugins run with uv, services too | Checks the script's uv lock (`uv lock --check`), then copies your plugin folder |
| `cargo` | Rust programs | Builds for `windows-x86_64`, `macos-aarch64` and `linux-x86_64` with `cargo build --locked --release`, run in your plugin folder. Your repo needs a `rust-toolchain.toml` with a dated channel (such as `nightly-2026-10-01`) or an exact release (such as `1.91.0`) |
| `files` | Plugins that run no program (`runtime.type` is `none`), such as page-only plugins | Copies your plugin folder, with no build step |
| `wasm-rust` | WebAssembly plugins in Rust (`runtime.type` is `wasm`) | Builds with `cargo build --locked --release --target wasm32-wasip2`, and puts the component where `runtime.component` says |
| `wasm-python` | WebAssembly plugins in Python | Builds with componentize-py, and puts the component where `runtime.component` says |

Your folder must not bring any binaries. CI builds every program itself, and
refuses executables, `.pyc`, `.so`, `.dylib`, `.dll`, `.node`, `.cwasm` and
WebAssembly files that it didn't build. A file that git marks executable counts
as a program too. Archives and installers (zip, wheel, jar, MSI, tar, gzip and
the like) are refused as well, whatever their names: no one can review what
is inside them.

A uv script's lock (`<script>.py.lock`, which AutoScene's `--locked` needs)
takes its packages from a package index or a git repo only. A wheel, folder or
file from your repo, or a file from a URL, is refused. A package index other
than PyPI is shown to the reviewer.

With `cargo`, CI puts each platform's program at `bin/<platform>/<name>` in
the package, `<name>` being the file name `build.bins` gives. `build.bins`
names where cargo builds the program, such as `target/release/<name>`: CI
checks that its own `cargo build` made that file, and refuses a path your repo
holds. So your folder has no `bin/` of its own, and `runtime.command` starts
the programs from there:

```json
"command": {
  "windows-x86_64": ["bin/windows-x86_64/clip-renamer.exe"],
  "macos-aarch64": ["bin/macos-aarch64/clip-renamer"],
  "linux-x86_64": ["bin/linux-x86_64/clip-renamer"]
}
```

A uv script may run beside them, under its own platform key or `*`.

### WebAssembly plugins

Keep the layout `asp init --wasm` makes: the plugin folder (`plugin/`, with
`plugin.json`) and your source beside it. `source.path` names the plugin
folder, and CI builds in the folder above it, on Ubuntu only, as a component
runs the same on every system. The package holds only the plugin folder. CI
copies the component it built to `runtime.component`, over anything you
committed there, so you can leave a `plugin.wasm` in place or ignore it in git.

- `wasm-rust`: the folder above the plugin folder holds your crate.
  Your repo needs a `Cargo.lock` and, as for `cargo`, a `rust-toolchain.toml`
  with a dated channel or an exact release. CI adds the `wasm32-wasip2` target.
  `build.artifact` names where cargo builds the `.wasm` file, from the top of
  your repo. CI checks that its own `cargo build` made that file, and refuses
  a path your repo holds.
- `wasm-python`: the folder above the plugin folder holds `app.py` and `wit/`,
  and CI runs
  `componentize-py -d wit -w plugin componentize app -o <file>` there.
  `build.componentize_py` names the version, which must be one CI builds with
  (0.25.1 for now). If the plugin needs packages, `build.requirements` names a
  requirements file, from the top of your repo: only `<name>==<version>` lines
  with their hashes, as `uv pip compile --generate-hashes` writes them, with
  no options, URLs or paths. CI installs their wheels; a package with no wheel
  can't be used.

A build runs your code: cargo runs any `build.rs`, and componentize-py runs
your `app.py`. It gets no secret and can't attest or sign anything: the job
that packs and attests the result runs none of your code.

A Rust build comes out the same, byte for byte, from the same source, toolchain
and lock. With `build.sha256` you name the hash of your own build, and CI
fails if its component differs. CI builds with

```
RUSTFLAGS="--remap-path-prefix=<top of your repo>=/build --remap-path-prefix=<cargo home>=/cargo"
```

on Linux, so build that way to compare. The job summary of a pull request's
run shows the hash CI built. A Python build differs on every run, so
`wasm-python` takes no `build.sha256`: the index signs the hash CI built.

### Submit a plugin

1. Fork this repo.
2. The first time, add `plugins/<id>/plugin.toml`. It says who holds the id:

   ```toml
   author = "Alice Example"
   github = ["alice"]
   # Optional: your minisign public key (the line from author.pub).
   # authorKey = "RW..."
   ```

3. For each version, add `plugins/<id>/<version>.toml`:

   ```toml
   [source]
   repo = "https://github.com/alice/clip-renamer"
   commit = "<the full 40-character commit id>"
   path = "."            # the plugin folder inside your repo

   [build]
   kind = "uv-script"    # or "cargo", "files", "wasm-rust" or "wasm-python"

   # wasm-rust only: where cargo builds the .wasm file, from the top of your
   # repo, and optionally your own build's hash
   # artifact = "target/wasm32-wasip2/release/clip_renamer.wasm"
   # sha256 = "<64 hex digits>"

   # wasm-python only: the componentize-py version, and optionally your
   # hash-locked requirements file, from the top of your repo
   # componentize_py = "0.25.1"
   # requirements = "requirements.txt"

   # cargo only: where each platform's program ends up, from the top of
   # your repo, one entry per platform CI builds
   # [build.bins]
   # "windows-x86_64" = "target/release/clip-renamer.exe"
   # "macos-aarch64"  = "target/release/clip-renamer"
   # "linux-x86_64"   = "target/release/clip-renamer"
   ```

   The platform keys are the ones `asp` uses: `windows-x86_64`,
   `windows-aarch64`, `macos-aarch64`, `macos-x86_64`, `linux-x86_64` and
   `linux-aarch64`. CI builds three of them: `windows-x86_64` on Windows,
   `macos-aarch64` on macOS (Apple silicon only) and `linux-x86_64` on
   Ubuntu 22.04 (so the binary needs glibc 2.35 or newer). Each of those
   three needs an entry.

4. Open a pull request. CI builds your plugin from that commit and writes a
   summary of what it asks for. The commit must be on a branch or tag of the
   repo `source.repo` names; a commit that is only in a fork doesn't count. A run for someone outside the repo waits
   until the owner lets it start.
5. The owner reviews it. Once merged, CI builds it again, attests it, releases
   it as `<id>@<version>` and signs it into the index.

### Rules for a pull request

- The folder name is the plugin id. The file name is the version.
- `plugin.json`'s id and version match the folder and the file.
- An approved version file never changes. To fix something, submit a new
  version.
- `site/` is written by CI only.

## What is in this repo

| Path | What it is |
|---|---|
| `README.md` | This page |
| `POLICY.md` | What is accepted, what review checks, names, withdrawal |
| `SECURITY.md` | How to report a harmful plugin |
| `catalog.toml` | The uv and `asp` versions CI builds with, and how long a signature counts (`scripts/plan.py` pins componentize-py and its Python) |
| `plugins/<id>/plugin.toml` | Who holds a plugin id |
| `plugins/<id>/<version>.toml` | Where one version's source is and how to build it |
| `withdrawn.toml` | Withdrawn versions and why |
| `epochs/` | Key certificates signed offline (empty at the start) |
| `site/` | The signed index, written by CI only |
| `scripts/`, `.github/workflows/` | The checks, builds and signing |
