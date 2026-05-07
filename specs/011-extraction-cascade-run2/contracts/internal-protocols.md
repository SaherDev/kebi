# Internal Protocol Contracts: Extraction Cascade Run 2

> Run 2 adds no new HTTP endpoints. Contracts here are Python Protocol interfaces
> between internal components. These define the seams where implementations can be
> swapped (ADR-038).

## `PlacesValidatorProtocol`

**Location**: `src/kebi/core/extraction/validator.py`  
**Concrete implementation**: `GooglePlacesValidator`

```python
class PlacesValidatorProtocol(Protocol):
    async def validate(
        self, candidates: list[CandidatePlace]
    ) -> list[ExtractionResult] | None: ...
```

**Contract rules**:
- Returns `None` when `candidates` is empty OR when all candidates fail validation.
- Returns a non-empty `list[ExtractionResult]` on success.
- Never raises — failures from the underlying places client are swallowed per-candidate.
- All candidates are validated concurrently.

---

## `GroqTranscriptionProtocol`

**Location**: `src/kebi/providers/groq_client.py`  
**Concrete implementation**: `GroqWhisperClient`

```python
class GroqTranscriptionProtocol(Protocol):
    async def transcribe_url(self, cdn_url: str) -> str: ...
    async def transcribe_bytes(self, audio_bytes: bytes, filename: str) -> str: ...
```

**Contract rules**:
- Both methods return the full transcript as a plain string.
- Both methods may raise on API error — callers are responsible for wrapping in try/except.
- `filename` for `transcribe_bytes` must include the correct extension (e.g. `"audio.opus"`).

---

## `Enricher` (from Run 1, repeated for reference)

**Location**: `src/kebi/core/extraction/protocols.py`

```python
class Enricher(Protocol):
    async def enrich(self, context: ExtractionContext) -> None: ...
```

**Contract rules**:
- Mutates `context.candidates`, `context.caption`, or `context.transcript`.
- Never returns a value — all output lives in context.
- Must skip gracefully if required input is absent (e.g., `context.url is None`).
- Must not raise on recoverable failures — log a warning instead.

---

## Event: `ExtractionPending`

**Location**: `src/kebi/core/extraction/types.py`  
**Dispatched by**: `ExtractionPipeline`  
**Handled by**: `ExtractionPendingHandler`

```python
@dataclass
class ExtractionPending:
    user_id: str
    url: str | None
    pending_levels: list[ExtractionLevel]
    context: ExtractionContext
    event_type: str = "extraction_pending"  # required for EventDispatcher registry lookup
```

**Registry key**: `"extraction_pending"`  
**Handler registration**: at API wiring layer (Run 3)  
**Context ownership**: The handler receives the full `ExtractionContext` — no state is reconstructed.
