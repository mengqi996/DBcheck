# -*- coding: utf-8 -*-
from __future__ import annotations

"""Compatibility helpers for asyncio features missing on Python 3.8."""

import asyncio
from functools import partial
from typing import Any, Callable


async def to_thread(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    native = getattr(asyncio, "to_thread", None)
    if native is not None:
        return await native(func, *args, **kwargs)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))
