"""Structural graph-compilation tests (feature 027 M3, FR-025)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from langgraph.checkpoint.memory import InMemorySaver

from kebi.core.agent.graph import (
    NODE_AGENT,
    NODE_FALLBACK,
    NODE_RESOLVE_LOCATION,
    NODE_TOOLS,
    build_graph,
)


def test_build_graph_compiles_with_inmemorysaver(
    mock_llm: Any,
    no_tools: list[Any],
    checkpointer: InMemorySaver,
    mock_resolver_llm: MagicMock,
    mock_geocoding_client: MagicMock,
) -> None:
    app = build_graph(
        mock_llm, no_tools, checkpointer, mock_resolver_llm, mock_geocoding_client
    )
    assert app is not None


def test_build_graph_node_set(
    mock_llm: Any,
    no_tools: list[Any],
    checkpointer: InMemorySaver,
    mock_resolver_llm: MagicMock,
    mock_geocoding_client: MagicMock,
) -> None:
    """Compiled graph must expose the four documented nodes."""
    app = build_graph(
        mock_llm, no_tools, checkpointer, mock_resolver_llm, mock_geocoding_client
    )
    graph_repr = app.get_graph()
    node_names = set(graph_repr.nodes.keys())
    assert NODE_RESOLVE_LOCATION in node_names
    assert NODE_AGENT in node_names
    assert NODE_TOOLS in node_names
    assert NODE_FALLBACK in node_names
