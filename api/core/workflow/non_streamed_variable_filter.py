"""Drop empty final stream markers for selectors that never streamed content.

A tool node finalizes every variable it assigned with an empty ``is_final``
stream chunk, even when the variable was never streamed (``stream=False``).
The response stream filter buffers that marker as a stream event, so it
treats the variable as already streamed and never falls back to the value in
the variable pool. The Answer node then renders nothing for the segment while
the persisted run record holds the correct value
(https://github.com/langgenius/dify/issues/46034).

This filter runs before the response stream filter and drops an empty final
chunk for a selector that emitted no chunk event earlier in the run. With no
buffered event for the selector, the response stream filter takes its
existing pool fallback and emits the stored value.

Invariants:

- A dropped marker never discards content: only a chunk that is both empty
  and final is dropped, and only for a selector with no earlier chunk event
  in this run. Any chunk event for the selector, empty or not, marks it as
  streamed, so a genuinely streamed (including empty) variable keeps its
  final marker and behaves exactly as before.
- A dropped marker cannot stall a session: the only nodes that emit empty
  final markers (tool and LLM nodes) carry the same values in their
  completion outputs, so the variable pool holds the value by the time a
  downstream Answer node renders it. The pool fallback then completes the
  segment.
- Pause/resume is safe: a node's chunk events and its final marker are
  emitted synchronously within one node run, so no pause can interleave
  between them. The per-run seen set never needs to survive a resume.
- When in doubt the filter passes the event through; it never invents,
  reorders, or rewrites events, and ``flush`` emits nothing.
"""

from __future__ import annotations

from collections.abc import Iterable

from graphon.engine.filter import EngineEventFilter, EngineEventFilterContext
from graphon.engine_events import EngineEvent, NodeRunStreamChunkEvent


class NonStreamedVariableFilter(EngineEventFilter):
    """Drop content-free final markers for selectors that never streamed.

    Preconditions: events arrive in emission order within one engine run.
    Postconditions: every non-chunk event and every chunk event that carries
    content, is not final, or belongs to a selector that already streamed is
    passed through unchanged; an empty final chunk for a selector with no
    earlier chunk event is dropped. The filter keeps no state beyond the set
    of selectors that have streamed, and ``flush`` emits nothing.
    """

    def __init__(self) -> None:
        self._streamed_selectors: set[tuple[str, ...]] = set()

    def initialize(self, context: EngineEventFilterContext) -> None:
        """Reset per-run state. Preconditions: none. Postconditions: the set
        of selectors that have streamed is empty."""
        _ = context
        self._streamed_selectors = set()

    def on_event(self, event: EngineEvent) -> Iterable[EngineEvent]:
        """Filter one event.

        Preconditions: ``event`` is the next engine event in emission order.
        Postconditions: returns ``[event]`` unchanged except for an empty
        final chunk whose selector has no earlier chunk event, which returns
        ``[]``. The streamed-selector set gains the selector of every chunk
        event that is passed through.
        """
        if not isinstance(event, NodeRunStreamChunkEvent):
            return [event]
        selector = tuple(event.selector)
        if event.is_final and not event.chunk and selector not in self._streamed_selectors:
            return []
        self._streamed_selectors.add(selector)
        return [event]

    def flush(self) -> Iterable[EngineEvent]:
        """Emit nothing on stream end.

        Preconditions: the engine event stream has ended. Postconditions:
        returns no events; no session state is held past the run.
        """
        return []
