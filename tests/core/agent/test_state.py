"""AgentState + add_messages reducer tests (feature 027 M3)."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph

from kebi.core.agent.state import AgentState


def test_agent_state_typed_dict_shape() -> None:
    """Smoke: AgentState accepts all documented fields."""
    state: AgentState = {
        "messages": [HumanMessage(content="hi")],
        "taste_profile_summary": "likes ramen",
        "memory_summary": "vegetarian",
        "user_id": "u1",
        "location": {"lat": 13.7, "lng": 100.5},
        "last_recall_results": None,
        "reasoning_steps": [],
        "steps_taken": 0,
        "error_count": 0,
    }
    assert state["user_id"] == "u1"
    assert state["last_recall_results"] is None


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
            "location": None,
            "last_recall_results": None,
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
            "last_recall_results": None,
            "reasoning_steps": [],
        },
        config=config,
    )
    # Both turns accumulate: Human+AI from turn 1 + Human+AI from turn 2 = 4
    assert len(state2["messages"]) == 4
    assert state2["messages"][0].content == "one"
    assert state2["messages"][2].content == "two"
