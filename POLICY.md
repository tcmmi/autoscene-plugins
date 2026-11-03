# Catalog policy

Short rules for what gets into the AutoScene plugin catalog, and how.

## What is accepted

- Source must be public on GitHub (`https://github.com/`), at a fixed commit.
  Other hosts are not accepted.
- Builds come only from that source. No binaries are accepted. CI refuses
  any program, library or archive in your folder that it didn't build itself,
  and a uv lock that installs packages from files instead of a package index
  or a git repo.
- Build kinds for now: `uv-script`, `cargo` and `files`. The kind must fit
  the plugin:
  - `uv-script`: every program the plugin starts is a uv script with a lock.
  - `cargo`: every program the plugin starts is one CI builds.
  - `files`: the plugin runs no program (`runtime.type` is `none`).
- WebAssembly plugins (`wasm-rust`, `wasm-python`) come later.

## What review checks

A person reads, for each new version:

- the whole change since the last approved version (for a first version,
  the whole plugin),
- what it asks for: its permissions and the network hosts it names,
- its uv lock, for `uv-script` plugins,
- its `Cargo.lock` (new crates) and any `build.rs`, for `cargo` plugins.

A plugin that hides what it does, or asks for more than it needs, is
refused. Review is careful, but it is not a guarantee.

## A plugin may not

- run or load code that it downloads after install,
- ship obfuscated or minified code,
- send data to hosts it doesn't name, or for reasons it doesn't state,
- pretend to be AutoScene or another plugin.

A version that breaks one of these rules is withdrawn.

## Names

- A plugin id is a reverse-DNS name that you control: a domain you own, or
  `io.github.<your-user>`.
- `plugins/<id>/plugin.toml` says who holds the id. The owner merges new
  versions only from the GitHub accounts that `plugin.toml` lists.
- An author key in `plugin.toml` (optional) changes only in a pull request
  that carries a note signed with the old key (`minisign -S`). If the old key
  is lost, the owner first confirms the change with the GitHub accounts that
  `plugin.toml` lists.

## Withdrawal

- A version is withdrawn for malware, a data leak or broken security.
- A withdrawal names the versions and gives the reason in plain words.
- Apps that check daily (the default) learn of it within a day of it being
  published. **Check now** picks it up as soon as it is published.
- Approved version files are never changed or deleted. A withdrawal is added
  to `withdrawn.toml`.

## How fast

- One person reviews, in their own time. There is no fixed time for a
  review. Small changes to an approved plugin are usually quickest.
- Reports of harmful plugins come first. A withdrawal is signed into the
  index as soon as it is merged.
