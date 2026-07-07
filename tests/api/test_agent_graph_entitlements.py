"""get_agent_graph — per-request entitlement wiring.

Confirms the advanced-model role swap and the discovery tool gate are driven
by the GatewayIdentity, without building a real LangGraph.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from kebi.api.deps import GatewayIdentity, get_agent_graph


def _call(identity: GatewayIdentity) -> tuple[MagicMock, MagicMock]:
    """Invoke get_agent_graph with everything heavy patched out; return the
    (get_langchain_chat_model, build_tools) mocks for assertions."""
    llm_factory = MagicMock(return_value=MagicMock())
    tools_factory = MagicMock(return_value=[])
    with (
        patch("kebi.providers.llm.get_langchain_chat_model", llm_factory),
        patch("kebi.core.agent.tools.build_tools", tools_factory),
        patch("kebi.core.agent.graph.build_graph", MagicMock(return_value="GRAPH")),
        patch("kebi.api.deps.get_geocoding_client", MagicMock()),
    ):
        graph = get_agent_graph(
            identity=identity,
            checkpointer=MagicMock(),  # non-None so the build proceeds
            hybrid_search=MagicMock(),
            places_search_factory=MagicMock(),
            candidate_namer=MagicMock(),
        )
    assert graph == "GRAPH"
    return llm_factory, tools_factory


def _identity(**overrides: object) -> GatewayIdentity:
    return GatewayIdentity(user_id="user_test_dummy_123456789012345", **overrides)


def test_advanced_tier_selects_advanced_orchestrator_role() -> None:
    llm_factory, _ = _call(_identity(advanced_models_enabled=True))
    roles = {c.args[0] for c in llm_factory.call_args_list}
    assert "orchestrator_advanced" in roles
    assert "orchestrator" not in roles


def test_standard_tier_selects_base_orchestrator_role() -> None:
    llm_factory, _ = _call(_identity(advanced_models_enabled=False))
    roles = {c.args[0] for c in llm_factory.call_args_list}
    assert "orchestrator" in roles
    assert "orchestrator_advanced" not in roles


def test_discovery_flag_threads_into_build_tools() -> None:
    _, tools_factory = _call(_identity(discovery_enabled=False))
    assert tools_factory.call_args.kwargs["discovery_enabled"] is False

    _, tools_factory = _call(_identity(discovery_enabled=True))
    assert tools_factory.call_args.kwargs["discovery_enabled"] is True
