from __future__ import annotations

import pytest


# These tests deliberately specify production-hardening behavior that the current
# implementation does not yet provide. Keeping them as strict xfails means:
# - CI remains green only when all implemented behavior is stable;
# - an unexpected pass fails CI and forces this list to be removed/updated;
# - the validation PR remains an executable record of open product gaps.
KNOWN_RE