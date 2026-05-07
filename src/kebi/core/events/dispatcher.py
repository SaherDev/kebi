"""Event dispatcher for domain-driven architecture"""

from collections.abc import Awaitable, Callable
from typing import Protocol

from fastapi import BackgroundTasks

from kebi.core.events.events import DomainEvent

# Type alias for event handler callables
EventHandler = Callable[[DomainEvent], Awaitable[None]]


class EventDispatcherProtocol(Protocol):
    """Protocol for event dispatch (ADR-043)"""

    async def dispatch(self, event: DomainEvent) -> None:
        """Dispatch a domain event to registered handlers

        Args:
            event: Domain event to dispatch

        Handlers are executed as background tasks after HTTP response is sent.
        Handler exceptions are logged and traced via Langfuse — never surfaced
        to caller.
        """
        ...


class EventDispatcher:
    """Concrete EventDispatcher implementation with FastAPI BackgroundTasks

    Registers event handlers per-request and dispatches events as background tasks.
    Handler registry is injected at construction time (per ADR-043 design).
    """

    def __init__(
        self,
        background_tasks: BackgroundTasks,
        handler_registry: dict[str, EventHandler] | None = None,
    ):
        """Initialize dispatcher

        Args:
            background_tasks: FastAPI BackgroundTasks for async execution
            handler_registry: Mapping of event_type → handler callable
        """
        self.background_tasks = background_tasks
        self.handler_registry = handler_registry or {}

    def register_handler(self, event_type: str, handler: EventHandler) -> None:
        """Register a handler for a specific event type

        Args:
            event_type: Event type name (e.g., "place_saved")
            handler: Async callable that handles the event
        """
        self.handler_registry[event_type] = handler

    async def dispatch(self, event: DomainEvent) -> None:
        """Dispatch a domain event to registered handlers

        Args:
            event: Domain event to dispatch

        The handler is queued as a background task. If no handler is registered
        for this event type, the event is silently dropped.
        """
        handler = self.handler_registry.get(event.event_type)
        if handler:
            self.background_tasks.add_task(handler, event)
