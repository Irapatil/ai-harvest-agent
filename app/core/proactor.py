"""
ProactorEventLoop bridge for Windows + uvicorn --reload.

Problem
───────
When uvicorn starts with --reload on Windows, it calls:
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

SelectorEventLoop cannot spawn OS subprocesses, so Playwright's browser
launch raises NotImplementedError.

Solution
────────
needs_proactor() detects the bad loop.
run_in_proactor(coro_factory) runs the coroutine in a fresh thread that
owns its own ProactorEventLoop, then awaits it from the outer event loop
via run_in_executor — keeping the FastAPI response pipeline non-blocking.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any, Callable, Awaitable, TypeVar

T = TypeVar("T")


def needs_proactor() -> bool:
    """
    Return True when the running loop cannot spawn OS subprocesses.
    This happens on Windows when uvicorn --reload forces SelectorEventLoop.
    """
    if sys.platform != "win32":
        return False
    try:
        return isinstance(asyncio.get_event_loop(), asyncio.SelectorEventLoop)
    except RuntimeError:
        return False


def _run_sync(coro_factory: Callable[[], Awaitable[T]]) -> T:
    """
    Synchronous worker: create a ProactorEventLoop, run coro_factory() to
    completion, close the loop, and return the result.
    Called via run_in_executor so it runs on a thread-pool thread — NOT on
    the outer event loop thread.
    """
    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro_factory())
    finally:
        loop.close()
        asyncio.set_event_loop(None)


async def run_in_proactor(coro_factory: Callable[[], Awaitable[T]]) -> T:
    """
    Await coro_factory() inside a ProactorEventLoop on a worker thread.

    Usage
    ─────
    Instead of:
        result = await my_playwright_coro()

    Write:
        if needs_proactor():
            result = await run_in_proactor(my_playwright_coro)
        else:
            result = await my_playwright_coro()

    Or unconditionally (safe on all platforms / loop types):
        result = await run_in_proactor(my_playwright_coro)
    """
    outer_loop = asyncio.get_event_loop()
    return await outer_loop.run_in_executor(None, _run_sync, coro_factory)
