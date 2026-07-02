"""Capability layer: bind an agent's concrete tool names to canonical capabilities so the corpus
is universal across agents (spec §4). A probe targets canonical capabilities (its `required_tools`
and `tool_called`/`tool_arg` oracle args); a real agent's `tool_inventory` declares which
capabilities each of its tools fulfils. Selection and the oracle resolve through this layer.

Backward-compatible: a tool with no declared capabilities is its own capability ([name]); a run
with only bare `available_tools` names treats each name as a capability — identical to the
pre-capability behaviour."""

from probe_engine.domain.run import RunConfig


def provided_capabilities(run_config: RunConfig) -> set[str]:
    """Every capability the configured agent provides: the union of each declared tool's
    capabilities, plus bare `available_tools` names (each its own capability)."""
    caps: set[str] = set()
    for spec in run_config.tool_inventory:
        caps.update(spec.effective_capabilities())
    caps.update(run_config.available_tools)
    return caps


def capabilities_of(name: str, inventory: list) -> list[str]:
    """The capabilities of a real tool by name (for tagging recorded calls). Unknown name -> [name]."""
    for spec in inventory or []:
        if spec.name == name:
            return spec.effective_capabilities()
    return [name]
