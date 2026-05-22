"""AgentState + add_messages reducer tests (feature 027 M3)."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph

from kebi.core.agent.state import (
    LOCATION_INHERIT,
    AgentState,
    merge_working_location,
)


def test_agent_state_typed_dict_shape() -> None:
    """Smoke: AgentState accepts all documented fields."""
    state: AgentState = {
        "messages": [HumanMessage(content="hi")],
        "taste_profile_summary": "likes ramen",
        "memory_summary": "vegetarian",
        "user_id": "u1",
        "user_location": {"lat": 13.7, "lng": 100.5},
        "working_location": None,
        "location_clarification": None,
        "movement_profile": None,
        "reasoning_steps": [],
        "steps_taken": 0,
        "error_count": 0,
        "tool_calls_used": 0,
    }
    assert state["user_id"] == "u1"
    assert state["reasoning_steps"] == []


async def test_movement_profile_does_not_persist_across_turns() -> None:
    """`movement_profile` has no reducer — it is re-supplied every turn from
    the request. A turn that omits it must overwrite to None, never inherit a
    stale profile (contrast `working_location`, which carries on purpose).
    """
    checkpointer = InMemorySaver()

    async def passthrough(state: AgentState) -> dict:
        return {"messages": [AIMessage(content="ok")]}

    graph: StateGraph = StateGraph(AgentState)
    graph.add_node("passthrough", passthrough)
    graph.set_entry_point("passthrough")
    graph.add_edge("passthrough", END)
    app = graph.compile(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "u1"}}

    state1 = await app.ainvoke(
        {
            "messages": [HumanMessage(content="one")],
            "movement_profile": {"default_mode": "driving"},
        },
        config=config,
    )
    assert state1["movement_profile"] == {"default_mode": "driving"}

    # Turn 2 omits movement_profile (frontend stopped sending it).
    state2 = await app.ainvoke(
        {
            "messages": [HumanMessage(content="two")],
            "movement_profile": None,
        },
        config=config,
    )
    assert state2["movement_profile"] is None


def test_merge_working_location_inherit_keeps_carried_value() -> None:
    """The reducer is the explicit carry-forward contract: LOCATION_INHERIT
    preserves the prior turn's value; any other update replaces it.
    """
    carried = {"country": "Japan", "city": "Tokyo", "lat": 35.6, "lng": 139.7}
    # Sentinel → keep what was carried by the checkpointer.
    assert merge_working_location(carried, LOCATION_INHERIT) == carried
    # A resolved location → replace.
    resolved = {"country": "France", "city": "Lyon", "lat": 45.7, "lng": 4.8}
    assert merge_working_location(carried, resolved) == resolved
    # An explicit None → clear.
    assert merge_working_location(carried, None) is None


async def test_add_messages_reducer_appends_across_invocations() -> None:
    """The `messages` reducer accumulates, not overwrites, across turns."""
    checkpointer = InMemorySaver()

    async def echo(state: AgentState) -> dict:
        """Passthrough node that adds an AIMessage echoing the last human."""
        last = state["messages"][-1]
        return {"messages": [AIMessage(content=f"echo: {last.content}")]}

    graph: StateGraph = StateGraph(AgentState)
    graph.add_node("echo", echo)
    graph.set_entry_point("echo")
    graph.add_edge("echo", END)
    app = graph.compile(checkpointer=checkpointer)

    config = {"configurable": {"thread_id": "u1"}}

    state1 = await app.ainvoke(
        {
            "messages": [HumanMessage(content="one")],
            "taste_profile_summary": "",
            "memory_summary": "",
            "user_id": "u1",
            "user_location": None,
            "reasoning_steps": [],
            "steps_taken": 0,
            "error_count": 0,
        },
        config=config,
    )
    assert len(state1["messages"]) == 2  # HumanMessage + AIMessage

    state2 = await app.ainvoke(
        {
            "messages": [HumanMessage(content="two")],
            "reasoning_steps": [],
        },
        config=config,
    )
    # Both turns accumulate: Human+AI from turn 1 + Human+AI from turn 2 = 4
    assert len(state2["messages"]) == 4
    assert state2["messages"][0].content == "one"
    assert state2["messages"][2].content == "two"
