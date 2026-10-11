"""Error text that names the cause, not the wrapper."""


def exception_detail(exc: BaseException) -> str:
    """TaskGroups wrap the real failure in ExceptionGroups; report the innermost one."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{type(exc).__name__}: {exc}"
