"""secret-deals approval tools for Cawl (SEC-14).

Registers eleven tools into the ``deals`` toolset. Each runs the secret-deals worker's CLI with
``--json`` (contract: ``secret-deals/docs/cawl-contract.md``) and turns its one JSON object into
fixed-format text. Self-contained on purpose: relative imports only, so the same directory works
bundled (``plugins/deals``) or dropped into ``~/.hermes/plugins/deals``. Either way it loads only
when listed in ``plugins.enabled``.
"""

from __future__ import annotations

from . import tools as _t


def register(ctx) -> None:
    """Register the deals tools, bound to this profile's plugin settings."""
    for name, schema, handler, emoji in _t.build_tools(ctx.get_config):
        ctx.register_tool(name=name, toolset=_t.TOOLSET, schema=schema, handler=handler, emoji=emoji)
