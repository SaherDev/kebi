"""DeltaBuffer — live narration, promotion, and answer streaming (ADR-158/159)."""

from __future__ import annotations

from kebi.core.chat.delta_buffer import DeltaBuffer, DeltaEvent

_NARRATION = "okay, nothing in your saves for canggu — checking what's on tonight."
# > 280 chars in total — an answer that proves itself mid-message.
_LONG_OPENING = (
    "tonight is Luigi's night, the counter seats are the move, and if you "
    "get there before seven you'll beat the queue that forms once the "
    "sunset crowd rolls off the beach and floods the whole lane, so aim "
    "for six thirty, order the tasting menu straight away, and keep the "
    "second half of the night open for the beach bars further down."
)


def _texts(events: list[DeltaEvent], kind: str) -> str:
    return "".join(e.text for e in events if e.kind == kind)


def test_narration_types_out_live() -> None:
    buf = DeltaBuffer(narration_flush_chars=12)
    events = buf.feed("m1", "okay, nothing in ", False)
    events += buf.feed("m1", "your saves for canggu", False)
    assert all(e.kind == "narration" for e in events)
    assert _texts(events, "narration").startswith("okay, nothing in your saves")


def test_tool_chunk_flushes_narration_tail_then_goes_quiet() -> None:
    buf = DeltaBuffer(narration_flush_chars=12)
    events = buf.feed("m1", _NARRATION, False)
    events += buf.feed("m1", "", True)
    # Everything said made it to the thinking row, nothing was promoted.
    assert _texts(events, "narration") == _NARRATION
    assert not [e for e in events if e.kind == "answer"]
    # The suppressed message stays quiet from here.
    assert buf.feed("m1", "tool arg text", False) == []


def test_long_answer_promotes_mid_message() -> None:
    buf = DeltaBuffer(threshold_chars=280, min_flush_chars=24)
    half = len(_LONG_OPENING) // 2
    events = buf.feed("m1", _LONG_OPENING[:half], False)
    events += buf.feed("m1", _LONG_OPENING[half:], False)
    answers = [e for e in events if e.kind == "answer"]
    assert answers and answers[0].promote
    # The promoted prefix carries everything said so far — the client
    # clears the typed narration and loses nothing.
    assert _texts(answers, "answer") == _LONG_OPENING
    # Subsequent chunks stream as ordinary answer deltas.
    more = buf.feed("m1", "Also the moon is out tonight.", False)
    assert [e.kind for e in more] == ["answer"]
    assert not more[0].promote


def test_short_terminal_answer_promotes_at_boundary() -> None:
    buf = DeltaBuffer(narration_flush_chars=12)
    typed = buf.feed("m1", "Saher. You told me earlier.", False)
    events = buf.boundary()
    assert [e.kind for e in events] == ["answer"]
    assert events[0].promote
    assert events[0].text == "Saher. You told me earlier."
    # Whatever typed into the row is superseded by the promote.
    assert all(e.kind == "narration" for e in typed)


def test_boundary_after_tool_call_promotes_nothing() -> None:
    buf = DeltaBuffer()
    buf.feed("m1", _NARRATION, False)
    buf.feed("m1", "", True)
    assert buf.boundary() == []


def test_new_message_id_resets_a_suppressed_verdict() -> None:
    buf = DeltaBuffer(threshold_chars=10, min_flush_chars=1, narration_flush_chars=1)
    buf.feed("m1", "checking", False)
    buf.feed("m1", "", True)
    events = buf.feed("m2", "the actual answer text", False)
    answers = [e for e in events if e.kind == "answer"]
    assert answers and answers[0].promote
    assert answers[0].text == "the actual answer text"


def test_answer_lane_normalizes_like_the_final_frame() -> None:
    # The final `message` frame passes through `normalize_voice` (dash →
    # comma); answer deltas must match it byte-for-byte or the swap jumps.
    # The dash is held back until the next chunk decides its fate.
    buf = DeltaBuffer(threshold_chars=1, min_flush_chars=1, narration_flush_chars=999)
    out = buf.feed("m1", "counter seats —", False)
    out += buf.feed("m1", " the move", False)
    assert _texts(out, "answer") == "counter seats, the move"


def test_narration_streams_raw() -> None:
    # Narration matches the step summary, which is the model's text
    # verbatim — no voice normalization on the thinking row.
    buf = DeltaBuffer(narration_flush_chars=1)
    events = buf.feed("m1", "hmm — let me look", False)
    assert _texts(events, "narration") == "hmm — let me look"


def test_answer_deltas_are_plain_prose() -> None:
    buf = DeltaBuffer(threshold_chars=1, min_flush_chars=1)
    out = buf.feed("m1", "tonight is Luigi's night", False)
    assert _texts(out, "answer") == "tonight is Luigi's night"
    assert "kebi://" not in _texts(out, "answer")
