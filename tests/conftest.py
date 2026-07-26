import os

# Provide the required credentials BEFORE any test module imports src.settings
# (Settings() is instantiated at import time and would otherwise fail). In CI the
# same variables are injected via the workflow's `env:` block.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
