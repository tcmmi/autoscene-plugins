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
  that includes the uv lock; for `cargo` plugins, `Cargo.lock` and any
  `build.rs`.

What it doesn't mean:

- It is not a promise that a plugin has no bugs.
- A plugin that runs a program runs it with your rights, as any program you
  start does. Review lowers the risk. It doesn't take it away.

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
| `cargo` | Rust programs | Builds for `windows-x86_64`, `macos-aarch64` and `linux-x86_64` with `cargo build --locked --release`. Your repo needs a `rust-toolchain.toml` with a dated channel |
| `files` | Plugins that run no program (`runtime.type` is `none`), such as page-only plugins | Copies your plugin folder, with no build step |

WebAssembly plugins (`wasm-rust`, `wasm-python`) come later.

Your folder must not bring any binaries. CI builds every program itself, and
refuses executables, `.pyc`, `.so`, `.dylib`, `.dll`, `.node`, `.cwasm` and
WebAssembly files that it didn't build.

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
   kind = "uv-script"    # or "cargo" or "files"

   # cargo only: where each platform's binary ends up, one entry per
   # platform CI builds
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
   summary of what it asks for. A run for someone outside the repo waits
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
| `catalog.toml` | The uv and `asp` versions CI builds with, and how long a signature counts |
| `plugins/<id>/plugin.toml` | Who holds a plugin id |
| `plugins/<id>/<version>.toml` | Where one version's source is and how to build it |
| `withdrawn.toml` | Withdrawn versions and why |
| `epochs/` | Key certificates signed offline (empty at the start) |
| `site/` | The signed index, written by CI only |
| `scripts/`, `.github/workflows/` | The checks, builds and signing |
