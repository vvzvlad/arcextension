import os

# Provide the required tokens BEFORE any test module imports src.settings
# (Settings() is instantiated at import time and would otherwise fail because
# EXT_TOKEN / METRICS_TOKEN have no default). In CI the same variables are
# injected via the workflow's `env:` block.
os.environ.setdefault("EXT_TOKEN", "test-ext-token")
os.environ.setdefault("METRICS_TOKEN", "test-metrics-token")
