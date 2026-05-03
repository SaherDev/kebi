# places_v2 — User Places API Contract

HTTP surface for `user_places` owned by this library. All endpoints share the prefix `/v1/users/{user_id}/places` — `user_id` is a path param. Each route lists its endpoint and the service call it maps to.

---

## Route 1 — List a user's saved places

**Endpoint:** `GET /v1/users/{user_id}/places`

**Service call:**
```python
service.list_user_places(user_id: str, query: UserPlacesListQuery) -> SavedPlacesPage
```

**`UserPlacesListQuery` (Pydantic, in `models.py`)** — bundles pagination, filters, ordering:

| Field | Type | Default |
|---|---|---|
| `limit` | `int` | `25` (`ge=1, le=100`) |
| `offset` | `int` | `0` (`ge=0`) |
| `visited` | `bool \| None` | `None` |
| `liked` | `bool \| None` | `None` (tri-state — omit for NULL) |
| `approved` | `bool \| None` | `None` |
| `source` | `PlaceSource \| None` | `None` |
| `category` | `PlaceCategory \| None` | `None` |
| `name` | `str \| None` | `None` (ILIKE) |
| `saved_after` | `datetime \| None` | `None` |
| `saved_before` | `datetime \| None` | `None` |
| `order_by` | `Literal["saved_at","visited_at","place_name"]` | `"saved_at"` |
| `order` | `Literal["asc","desc"]` | `"desc"` |

**Response — `SavedPlacesPage`:** `{ items: list[SavedPlaceView], total: int, limit: int, offset: int }`.

---

## Route 2 — Fetch one saved place

**Endpoint:** `GET /v1/users/{user_id}/places/{user_place_id}`

**Service call:**
```python
service.get_user_place(user_id: str, user_place_id: str) -> SavedPlaceView
```

**Response:** `SavedPlaceView` with **always-fresh** live fields. On cache miss the service refreshes from Google (one `places.get` call), writes back to cache, and returns the enriched object. Same refresh-on-miss path is used by Route 1 (list).

**Errors:** `404` if the row doesn't exist OR `row.user_id != user_id` (mask existence — don't leak ownership).

---

## Route 3 — Update a saved place

**Endpoint:** `PATCH /v1/users/{user_id}/places/{user_place_id}`

**Body — `UpdateUserPlace` (Pydantic, in `models.py`):**

| Field | Type | Notes |
|---|---|---|
| `visited` | `bool \| None` | Omit to leave unchanged |
| `liked` | `bool \| None` | Omit to leave unchanged |
| `approved` | `bool \| None` | Omit to leave unchanged |
| `note` | `str \| None` | Omit to leave; pass `""` to clear |

All fields optional; only set fields are written. Wraps the existing `UserPlacesService.update_status(...)` at `user_places_service.py:61`.

**Service call:**
```python
service.update_user_place(user_id: str, user_place_id: str, body: UpdateUserPlace) -> SavedPlaceView
```

**Response:** `200` + `SavedPlaceView` (overlaid).

**Errors:** `404` if not found OR `row.user_id != user_id`.

---

## Route 4 — Soft-delete a saved place

**Endpoint:** `DELETE /v1/users/{user_id}/places/{user_place_id}`

**Service call:**
```python
service.delete_user_place(user_id: str, user_place_id: str) -> None
```

Sets `user_places.deleted_at = now()` for the row. The underlying `places_v2` row is not touched. Idempotent — deleting an already-deleted row is a no-op (still returns `204`).

**New repo + service code:**
- `UserPlacesRepo.soft_delete_by_id(user_place_id)` — `UPDATE user_places SET deleted_at = now() WHERE user_place_id = ... AND deleted_at IS NULL`.
- `UserPlacesService.delete_user_place(user_id, user_place_id)` — verifies ownership, then calls the repo.

**Response:** `204 No Content`.

**Errors:** `404` if not found OR `row.user_id != user_id` (mask existence).
