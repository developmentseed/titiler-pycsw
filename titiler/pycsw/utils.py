"""titiler.pycsw utilities.

The `retry` decorator is adapted from titiler.pgstac (MIT License).
"""

import time
from typing import Any, Sequence, Type, Union


def retry(
    tries: int,
    exceptions: Union[Type[Exception], Sequence[Type[Exception]]] = Exception,
    delay: float = 0.0,
):
    """Retry decorator for transient failures."""

    def _decorator(func: Any):
        def _newfn(*args: Any, **kwargs: Any):
            attempt = 0
            while attempt < tries:
                try:
                    return func(*args, **kwargs)
                except exceptions:  # type: ignore
                    attempt += 1
                    time.sleep(delay)

            return func(*args, **kwargs)

        return _newfn

    return _decorator
