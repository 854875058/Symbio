"""Event bus for inter-component communication."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Coroutine
from uuid import uuid4

from symbio.utils.logger import get_logger

logger = get_logger("event_bus")


class EventType(str, Enum):
    """System event types."""

    # Task events
    TASK_CREATED = "task.created"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"

    # Agent events
    AGENT_SPAWNED = "agent.spawned"
    AGENT_COMPLETED = "agent.completed"
    AGENT_FAILED = "agent.failed"

    # Memory events
    MEMORY_STORED = "memory.stored"
    MEMORY_RECALLED = "memory.recalled"

    # Tool events
    TOOL_EXECUTED = "tool.executed"
    TOOL_FAILED = "tool.failed"

    # HITL events
    HITL_SUSPENDED = "hitl.suspended"
    HITL_APPROVED = "hitl.approved"
    HITL_REJECTED = "hitl.rejected"

    # System events
    SYSTEM_STARTUP = "system.startup"
    SYSTEM_SHUTDOWN = "system.shutdown"


@dataclass
class Event:
    """Event data structure."""

    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    event_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "data": self.data,
            "source": self.source,
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat(),
        }


# Type alias for event handlers
EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """Async event bus for pub/sub communication.

    Usage:
        bus = EventBus()

        @bus.on(EventType.TASK_COMPLETED)
        async def handle_task(event: Event):
            print(f"Task done: {event.data}")

        await bus.emit(Event(type=EventType.TASK_COMPLETED, data={"task_id": "123"}))
    """

    def __init__(self):
        self._handlers: dict[EventType, list[EventHandler]] = {}
        self._global_handlers: list[EventHandler] = []
        self._history: list[Event] = []
        self._max_history: int = 1000

    def on(self, event_type: EventType | None = None) -> Callable:
        """Register an event handler.

        Args:
            event_type: Event type to listen for. None for all events.

        Returns:
            Decorator function
        """

        def decorator(func: EventHandler) -> EventHandler:
            if event_type is None:
                self._global_handlers.append(func)
            else:
                if event_type not in self._handlers:
                    self._handlers[event_type] = []
                self._handlers[event_type].append(func)
            return func

        return decorator

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """Subscribe a handler to an event type.

        Args:
            event_type: Event type to listen for
            handler: Async handler function
        """
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    def subscribe_all(self, handler: EventHandler) -> None:
        """Subscribe a handler to all events.

        Args:
            handler: Async handler function
        """
        self._global_handlers.append(handler)

    async def emit(self, event: Event) -> None:
        """Emit an event to all subscribed handlers.

        Args:
            event: Event to emit
        """
        logger.debug(f"Emitting event: {event.type.value} from {event.source}")

        # Store in history
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history :]

        # Collect handlers
        handlers = list(self._global_handlers)
        if event.type in self._handlers:
            handlers.extend(self._handlers[event.type])

        # Execute handlers concurrently
        if handlers:
            tasks = [self._safe_call(handler, event) for handler in handlers]
            await asyncio.gather(*tasks)

    async def _safe_call(self, handler: EventHandler, event: Event) -> None:
        """Call handler with error catching."""
        try:
            await handler(event)
        except Exception as e:
            logger.error(f"Handler error for {event.type.value}: {e}")

    def get_history(
        self,
        event_type: EventType | None = None,
        limit: int = 100,
    ) -> list[Event]:
        """Get event history.

        Args:
            event_type: Filter by event type. None for all.
            limit: Max events to return

        Returns:
            List of events
        """
        events = self._history
        if event_type:
            events = [e for e in events if e.type == event_type]
        return events[-limit:]

    def clear_history(self) -> None:
        """Clear event history."""
        self._history.clear()
