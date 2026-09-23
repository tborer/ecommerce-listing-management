"""Vercel entrypoint for the web app API (see [tool.vercel] in pyproject.toml).

The package lives under src/; put it on the path so this works whether or
not the build installed the project itself."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
if os.environ.get("VERCEL"):
    # Only /tmp is writable on Vercel (category-tree cache etc.).
    os.environ.setdefault("ELM_DATA_DIR", "/tmp")

from ecommerce_listing_mgmt.webapp.main import app  # noqa: E402

__all__ = ["app"]
