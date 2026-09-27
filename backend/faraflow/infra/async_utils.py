"""Thread offloading compatible with the API's Python 3.8 minimum."""

import asyncio
import contextvars
from functools import partial
from typing import Any, Callable, TypeVar

T = TypeVar("T")


async def run_sync(function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    context = contextvars.copy_context()
    call = partial(context.run, function, *args, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(None, call)
