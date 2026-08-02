"""Per-instance browser generator (docs/architecture.md §13).

An instance is a themed Brave launcher with its OWN ``--user-data-dir`` and its
OWN copy of the extension bundle. The generator produces, per instance:

* a copy of the extension bundle (``extension/``) with a per-instance
  ``instance.json`` inside it — the FOUR fields ``{instanceId, title,
  serviceUrl, token}`` (+ ``allowExecuteJs``) that a fresh, empty profile has no
  other way to receive (§3/§6);
* a stamped ``manifest.json``: the ``<host>`` placeholders in ``host_permissions``
  filled from ``serviceUrl`` and the ``key`` pinned so the extension id is stable
  across path/rename and IDENTICAL across all instances (§13, arch row 21);
* an empty per-instance profile dir (the ``--user-data-dir``) — deliberately
  empty so the extension mints its own ``install_uuid`` there on first run (§6),
  which is what lets the service tell a legit reconnect from a cloned ``.app``
  (``duplicate_instance``);
* a ``.app`` wrapper (Info.plist + launcher script + icon) that execs the system
  Brave with the two flags above.

The module is split into a PURE core (`core`, all filesystem/text logic, runs on
Linux/CI), a `keys` helper (signing-key generation/persistence — the one place
that needs `cryptography`), and a thin macOS platform layer (`macos`, the real
``.icns``/``.app`` build that cannot run in CI).
"""
