"""DeltaBuffer — routes streamed agent tokens to the thinking row or the answer.

The `/v1/chat/stream` route subscribes to the orchestrator's token stream
(LangGraph `stream_mode="messages"`) so the turn reads like a person:
the agent talks a little while it decides, works, talks again, then the
answer types itself out (ADR-158/159). The hard problem is that which
kind of text a message is cannot be known while its first tokens arrive —
the model's text before a tool call is its thinking line (ADR-157), a
message with no tool call is the answer, and tool calls stream *last*.

So every message starts as narration and earns promotion:

- **narration events** — while the verdict is unknown, text streams into
  the active thinking row (`reasoning_delta` on the wire), so the talk
  types out live instead of landing when the step completes. If a
  tool-call chunk arrives, the text was indeed narration; it is already
  where it belongs and the step's `done` frame supersedes it.
- **answer events** — text that outgrows any plausible narration
  (`threshold_chars`), or a message that ends cleanly with no tool call
  (`boundary`), is the answer. The first answer event carries
  `promote=True`: the client clears what it typed into the thinking row
  and seeds the answer bubble with this event's text. Subsequent events
  append (`message_delta` on the wire).

Two invariants carry over from ADR-158. **Links never stream**: deltas
are plain prose; linkification runs once over the complete final text
and rides the terminal `message` frame, which the client swaps in as
authoritative — so no `kebi://` URI can be split or lost. **Answer text
byte-matches the final frame**: answer events pass through the same
`normalize_voice` as the final content, holding back trailing characters
that could be a partial pattern until the next chunk resolves them.
Narration events stream raw — they match the step summary, which is the
model's text verbatim, and are superseded by the step's `done` frame.

The buffer is transport-agnostic and knows nothing about LangChain chunk
types: the route feeds it `(message_id, text, has_tool_chunk)` plus a
`boundary()` call at every graph superstep, and renders whatever events
come back.
"""

from __future__ import annotations

from dataclasses import dataclass

from kebi.core.agent.entity_links import normalize_voice

_STATE_NARRATING = "narrating"
_STATE_STREAMING = "streaming"
_STATE_SUPPRESSED = "suppressed"

# Characters a `normalize_voice` pattern can start with or continue
# through. An answer flush never ends inside a run of these — the run is
# held back so the next chunk decides what it becomes (dash → comma vs
# dropped before punctuation, `**` stripped, doubled comma collapsed).
_HOLDBACK_CHARS = frozenset("—–*→,; \t")


def _split_flushable(pending: str) -> tuple[str, str]:
    """Split `pending` into (flush now, hold for the next chunk)."""
    cut = len(pending)
    while cut > 0 and pending[cut - 1] in _HOLDBACK_CHARS:
        cut -= 1
    return pending[:cut], pending[cut:]


@dataclass(frozen=True)
class DeltaEvent:
    """One emission decision: where `text` belongs on the wire.

    `kind` is "narration" (type into the active thinking row) or "answer"
    (append to the answer bubble). `promote` is set on the first answer
    event of a message whose text had been narrating: the client clears
    the thinking row's typed text and seeds the answer bubble with this
    event's text (which is the normalized full prefix, so nothing typed
    is lost — it moves).
    """

    kind: str
    text: str
    promote: bool = False


class DeltaBuffer:
    """Per-turn state machine turning agent token chunks into DeltaEvents.

    One instance per stream. `feed` returns the events for this chunk.
    `boundary` must be called at every graph superstep boundary: it both
    resolves a message that ended with no tool call (short answer →
    promote) and separates consecutive LLM calls when a provider does not
    stamp stable message ids on its chunks.
    """

    def __init__(
        self,
        threshold_chars: int = 280,
        min_flush_chars: int = 24,
        narration_flush_chars: int = 12,
    ) -> None:
        # Narration is prompted to a sentence or two (~150 chars); the
        # threshold sits above it with margin. It only sets how soon a
        # long answer switches mid-message from the thinking row to the
        # answer bubble — a short answer is promoted at `boundary` anyway.
        self._threshold = threshold_chars
        # Coalesce tiny provider chunks so mobile radios see fewer
        # frames; visually indistinguishable from per-token emission.
        self._min_flush = min_flush_chars
        self._narr_flush = narration_flush_chars
        self._state = _STATE_NARRATING
        self._message_id: str | None = None
        # Everything this message has said (for the promote event) and
        # the not-yet-emitted tails of each lane.
        self._total = ""
        self._narr_pending = ""
        self._answer_pending = ""

    def feed(
        self, message_id: str | None, text: str, has_tool_chunk: bool
    ) -> list[DeltaEvent]:
        """Consume one chunk's text; return the events to emit."""
        events: list[DeltaEvent] = []
        if message_id is not None and message_id != self._message_id:
            # A new LLM call inside the same superstep — the previous one
            # can only have been a tool-call message (a terminal answer
            # ends the node), so its leftovers are narration remnants the
            # step's `done` frame supersedes. Start fresh.
            self._reset()
            self._message_id = message_id

        if has_tool_chunk:
            # This message is (or has become) a tool call: its text is
            # narration. Flush the typed tail so the row reads complete,
            # then go quiet — the step's `done` frame takes over. If the
            # answer lane had already started (an over-long narration
            # crossed the threshold), the terminal `message` swap heals
            # what leaked.
            if self._state == _STATE_NARRATING and self._narr_pending:
                events.append(DeltaEvent("narration", self._narr_pending))
            self._state = _STATE_SUPPRESSED
            self._total = ""
            self._narr_pending = ""
            self._answer_pending = ""
            return events

        if not text or self._state == _STATE_SUPPRESSED:
            return events

        if self._state == _STATE_NARRATING:
            self._total += text
            self._narr_pending += text
            if len(self._total) >= self._threshold:
                # Outgrew narration: this is the answer. Promote the full
                # prefix (normalized) and stream from here.
                self._state = _STATE_STREAMING
                flushable, retained = _split_flushable(self._total)
                self._answer_pending = retained
                self._narr_pending = ""
                if flushable:
                    events.append(
                        DeltaEvent("answer", normalize_voice(flushable), promote=True)
                    )
                return events
            if len(self._narr_pending) >= self._narr_flush:
                events.append(DeltaEvent("narration", self._narr_pending))
                self._narr_pending = ""
            return events

        # Streaming the answer.
        self._answer_pending += text
        if len(self._answer_pending) < self._min_flush:
            return events
        flushable, retained = _split_flushable(self._answer_pending)
        self._answer_pending = retained
        if flushable:
            events.append(DeltaEvent("answer", normalize_voice(flushable)))
        return events

    def boundary(self) -> list[DeltaEvent]:
        """Superstep boundary — the current LLM call (if any) has ended.

        A message still narrating here ended with no tool call, which
        makes it the terminal answer (the graph only leaves the agent
        node on a terminal response or tool calls): promote everything it
        said. The events go out before the route emits the terminal
        `message` frame, so the promoted text is on screen first.
        """
        events: list[DeltaEvent] = []
        if self._state == _STATE_NARRATING and self._total:
            events.append(
                DeltaEvent("answer", normalize_voice(self._total), promote=True)
            )
        self._reset()
        return events

    def _reset(self) -> None:
        self._state = _STATE_NARRATING
        self._message_id = None
        self._total = ""
        self._narr_pending = ""
        self._answer_pending = ""


__all__ = ["DeltaBuffer", "DeltaEvent"]
