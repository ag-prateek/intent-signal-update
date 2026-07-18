"""Intent Signal Update application package.

Importing the package installs the external research router on the main FastAPI
application while preserving the existing API and lifecycle.
"""

from importlib import import_module

main = import_module("app.main")

from app.research import install  # noqa: E402

install(main.app, main.database, main.settings)
