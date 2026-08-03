"""Per-instance browser generator (docs/architecture.md §13).

Under enrollment (§6/§13, issue #35/#37) the fleet loads ONE universal extension
bundle and an instance is a THIN wrapper around it. Two commands, in order:

``bundle`` builds that single fleet-wide bundle ONCE:

* a copy of the repo's ``extension/`` (dev cruft skipped) and nothing else. Nothing is
  stamped into ``manifest.json``: issue #35 removed the ``<host>`` patterns (only
  ``<all_urls>`` is left) and the ``key`` field is gone too, so the extension id is
  Chromium's hash of the load path (arch row 21) — which nothing consumes anymore, since
  no origin is checked on ``/ext`` and ``/api/*`` CORS accepts any origin;
* NO ``instance.json``, no token, no service URL, no ``instanceId``: the bundle
  carries no credential and no address at all.

``generate`` then wraps that SHARED bundle per instance:

* an empty per-instance profile dir (the ``--user-data-dir``) — deliberately
  empty so the extension mints its own ``install_uuid`` there on first run (§6),
  which is what makes a cloned ``.app`` an un-enrolled install rather than a
  takeover of the original;
* a ``.app`` wrapper (Info.plist + launcher script + icon) whose launcher execs
  the system Brave with ``--user-data-dir`` (the profile above) and
  ``--load-extension`` pointing at the SHARED bundle — NO per-instance copy of
  the extension and NO ``instance.json``.

The service address and the per-install secret are not in the build at all: each
profile receives them through the extension's enrollment settings UI, and the
operator approves the request on ``/admin`` (§13). There is consequently no
``restamp`` command and no token rotation — the shared token they existed for is
gone, and a code update is a rebuild of the one shared bundle.

The module is split into a PURE core (`core`, all filesystem/text logic, runs on
Linux/CI) and a thin macOS platform layer (`macos`, the real ``.icns``/``.app``
build that cannot run in CI).
"""
