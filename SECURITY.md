# Reporting a plugin

If a plugin in this catalog does harm (malware, a data leak, a way around
AutoScene's checks), please report it privately:

1. Open this repo's **Security** tab.
2. Choose **Report a vulnerability**.
3. Name the plugin id and version, and say what you saw.

The report goes only to the owner, [tcmmi](https://github.com/tcmmi). Please
don't open a public issue for it. If the **Report a vulnerability** button is
missing, open an issue that only says you have a private report, with no
details, and the owner will get in touch.

## What belongs here

- A plugin in the catalog that does something harmful or hidden.
- A problem with how this repo builds, attests or signs plugins.

A hole in AutoScene itself (the app, its page sandbox, its trust states)
belongs in [tcmmi/AutoScene](https://github.com/tcmmi/AutoScene), through
its own **Report a vulnerability**. If a catalog plugin uses such a hole,
report it in either place: one report is enough, and the owner links the two,
so the plugin is withdrawn here and the hole is fixed there. A plain bug in a
plugin belongs in that plugin's own repo.

## What happens next

The owner checks the report. If the plugin is harmful, the affected versions
are withdrawn (see [POLICY.md](POLICY.md)). Apps that check daily (the
default) learn of a withdrawal within a day of it being published. **Check
now** picks it up as soon as it is published.
