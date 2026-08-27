"""vexa_ext — an out-of-tree extension of the meetings control plane.

Nothing here lives under ``core/``. Every file in this package is one upstream does not own, so
``git pull`` from the Vexa remote is a fast-forward rather than a conflict negotiation.

Two strategies, and they cost very different amounts:

* **postwrap** (:mod:`vexa_ext.postwrap`) — call ``meeting_api.__main__.build_production_app()`` and
  add to the app it returns. Zero copying, zero private imports, all eight background loops intact.
  This is the default and the one to reach for.

* **root** (:mod:`vexa_ext.root`) — our own composition root. The ONLY way to swap a port, because
  ports are closure-captured into the routers at mount time (``bot_spawn.build_router``) and are NOT
  re-read from ``app.state`` per request. Costs a copy of the upstream wiring plus five private
  imports; see ``ext/README.md`` for the ledger.

Selected by ``VEXA_EXT_STRATEGY`` (``postwrap`` | ``root``); ``__main__`` dispatches.
"""
