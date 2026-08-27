"""Entrypoint — ``python -m vexa_ext``.

Mirrors upstream's own shape: the app is exposed LAZILY (PEP 562) so merely importing this module
never wires SQLAlchemy/asyncpg/boto3, and uvicorn constructs it only when it touches ``app``.

``VEXA_EXT_STRATEGY`` selects the composition:
  * ``postwrap`` (default) — upstream's root plus our routes. No private imports.
  * ``root``              — our own root. Required to swap a port; costs five private imports.
"""
from __future__ import annotations

import os


def _strategy() -> str:
    value = os.getenv("VEXA_EXT_STRATEGY", "postwrap").strip().lower()
    if value not in {"postwrap", "root"}:
        raise SystemExit(
            f"VEXA_EXT_STRATEGY={value!r} is not one of: postwrap, root. "
            "Refusing to boot rather than silently falling back — a deployment that thinks it "
            "swapped a port and did not is the failure this check exists to prevent."
        )
    return value


def build_app():
    strategy = _strategy()
    if strategy == "root":
        from .root import build_app as _build
    else:
        from .postwrap import build_app as _build
    return _build()


def __getattr__(name: str):
    if name == "app":
        return build_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main() -> None:
    import uvicorn

    uvicorn.run(
        build_app(),
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
