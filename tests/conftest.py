"""Global test configuration.

Disables auth for unit tests that don't test auth behavior.
"""

import os

# Disable auth for tests by default
os.environ.setdefault("AUTH_ENABLED", "false")
