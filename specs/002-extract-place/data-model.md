# Data Model: Place Extraction Endpoint (Phase 2)

**Branch**: `002-extract-place` | **Date**: 2026-03-24

---

## 1. Database: `places` table changes

The `Place` SQLAlchemy model and the `places` PostgreSQL table need three new nullable columns. A new Alembic migration is required.

### Updated `Place` model (additions only)

```python
# Additions to src/kebi/db/models.py — Place class

google_place_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
source: Mapped[str | None] = mapped_column(String, nullable=True)
```

### Migration

File: `alembic/versions/<hash>_add_extraction_metadata_to_places.py`

```python
def upgrade() -> None:
    op.add_column("places", sa.Column("google_place_id", sa.String(), nullable=True))
    op.add_column("places", sa.Column("confidence", sa.Float(), nullable=True))
    op.add_column("places", sa.Column("source", sa.String(), nullable=True))
    op.create_index("ix_places_google_place_id", "places", ["google_place_id"])

def downgrade() -> None:
    op.drop_index("ix_places_google_place_id", "places")
    op.drop_column("places", "source")
    op.drop_column("places", "confidence")
    op.drop_column("places", "google_place_id")
```

### Column semantics

| Column | Populated when | Notes |
|--------|---------------|-------|
| `google_place_id` | Google Places returns a match (EXACT or FUZZY) | Nullable — may be absent for CATEGORY_ONLY or NONE matches that still pass threshold |
| `confidence` | Record is written (confidence ≥ 0.70) | Always populated on write |
| `source` | Always on write | One of: `CAPTION`, `PLAIN_TEXT`, `SPEECH` (Phase 3), `OCR` (Phase 3) |

---

## 2. Enums

### `ExtractionSource`

```python
# src/kebi/core/extraction/confidence.py

from enum import Enum

class ExtractionSource(str, Enum):
    CAPTION = "CAPTION"       # TikTok oEmbed caption (Phase 2)
    PLAIN_TEXT = "PLAIN_TEXT" # Plain text input (Phase 2)
    SPEECH = "SPEECH"         # Whisper transcription (Phase 3 — defined now)
    OCR = "OCR"               # Frame OCR (Phase 3 — defined now)
```

### `PlacesMatchQuality`

```python
# src/kebi/core/extraction/places_client.py

from enum import Enum

class PlacesMatchQuality(str, Enum):
    EXACT = "EXACT"                 # Name similarity ≥ 0.95
    FUZZY = "FUZZY"                 # Name similarity ≥ 0.80
    CATEGORY_ONLY = "CATEGORY_ONLY" # Place found, name similarity < 0.80
    NONE = "NONE"                   # No match found
```

---

## 3. Pydantic models

### `PlaceExtraction` — LLM output schema

```python
# src/kebi/api/schemas/extract_place.py

class PlaceExtraction(BaseModel):
    """Structured output from LLM extraction step. Not persisted directly."""
    place_name: str = Field(description="Name of the place")
    address: str = Field(description="Full address including city")
    cuisine: str | None = Field(default=None, description="Cuisine type e.g. ramen, italian")
    price_range: Literal["low", "mid", "high"] | None = Field(
        default=None, description="low (<$15), mid ($15-40), high (>$40)"
    )
```

### `ExtractPlaceRequest` — API request

```python
class ExtractPlaceRequest(BaseModel):
    user_id: str
    raw_input: str
```

### `ExtractPlaceResponse` — API response

```python
class ExtractPlaceResponse(BaseModel):
    place_id: str | None          # UUID of saved record; None when requires_confirmation=True
    place: PlaceExtraction
    confidence: float
    requires_confirmation: bool   # True when 0.30 < confidence < 0.70
    source_url: str | None        # Populated for TikTok URLs, None for plain text
```

---

## 4. Internal result models

### `ExtractionResult`

```python
# src/kebi/core/extraction/result.py

from pydantic import BaseModel
from kebi.api.schemas.extract_place import PlaceExtraction
from kebi.core.extraction.confidence import ExtractionSource

class ExtractionResult(BaseModel):
    extraction: PlaceExtraction
    source: ExtractionSource   # set by the extractor — service never re-derives this
    source_url: str | None     # TikTok URL for TikTokExtractor; None for PlainTextExtractor
```

Each extractor constructs and returns this object. The `source` field is the extractor's declaration of what kind of input it processed. The service uses `result.source` directly in `compute_confidence()` — it never inspects `raw_input` again after `dispatch()` returns.

### `PlacesMatchResult`

```python
# src/kebi/core/extraction/places_client.py

class PlacesMatchResult(BaseModel):
    match_quality: PlacesMatchQuality
    validated_name: str | None    # Canonical name from Places API if matched
    google_place_id: str | None   # Google's place ID if matched
    lat: float | None
    lng: float | None
```

---

## 5. Error types

```python
# src/kebi/core/extraction/dispatcher.py
class UnsupportedInputError(Exception):
    """Raised when no extractor supports the given input."""

# src/kebi/core/extraction/service.py (or confidence.py)
class ExtractionFailedNoMatchError(Exception):
    """Raised when confidence ≤ 0.30 (no Places match). Maps to 422."""
```

---

## 6. Deduplication strategy

Before writing a new `Place` row, the service checks for an existing record by `google_place_id`:

```
if match.google_place_id is not None:
    existing = await db.query(Place).filter_by(google_place_id=match.google_place_id).first()
    if existing:
        return ExtractPlaceResponse(place_id=existing.id, ...)   # no write
```

This means two users saving the same restaurant share one `Place` row. The `user_id` column records who first added it; the taste model links users to places via a separate association (Phase 3). For Phase 2, the deduplication is purely on `google_place_id`. If `google_place_id` is `None` (CATEGORY_ONLY match that still passes threshold), a new row is always written.

## 7. Entity relationships

```
ExtractPlaceRequest (API boundary)
    └── raw_input → ExtractionDispatcher
                        └── TikTokExtractor | PlainTextExtractor
                                └── ExtractionResult(extraction: PlaceExtraction, source: ExtractionSource, source_url)
                                        └── GooglePlacesClient.validate_place()
                                                └── PlacesMatchResult
                                                        └── compute_confidence(result.source, match.match_quality, ...)
                                                                └── float (0.0–0.95)
                                                                        ├── ≥ 0.70 → dedup check by google_place_id
                                                                        │               ├── existing found → return existing place_id (no write)
                                                                        │               └── not found → Place (DB write) → ExtractPlaceResponse
                                                                        ├── 0.30–0.70 → ExtractPlaceResponse (requires_confirmation=True, no write)
                                                                        └── ≤ 0.30 → ExtractionFailedNoMatchError → 422
```
