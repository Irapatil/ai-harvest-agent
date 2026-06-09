"""
Development entry point — run with:

    python run.py            # port 8000
    python run.py 8010       # custom port

Why this file exists
────────────────────
Uvicorn's --reload mode calls `asyncio_setup(use_subprocess=True)` which
explicitly sets `WindowsSelectorEventLoopPolicy` on Windows.  That breaks
Playwright, which needs `ProactorEventLoop` to spawn browser subprocesses.

Passing `loop="none"` via the Python API tells uvicorn to skip its loop-policy
override entirely.  The Config object is pickled into the reload child
subprocess, so the child also inherits `loop="none"` and leaves the Windows
default (`ProactorEventLoop`) in place — Playwright works.

Note: `--loop none` is intentionally hidden from the CLI (uvicorn/main.py
excludes it from LOOP_CHOICES) but is fully supported via the Python API.
"""
from __future__ import annotations

import sys

import uvicorn

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=port,
        reload=True,
        loop="none",   # skip WindowsSelectorEventLoopPolicy — keeps ProactorEventLoop for Playwright
    )
