# Feature Specification: Taste Model

**Feature Branch**: `008-taste-model`
**Created**: 2026-03-31
**Status**: Draft

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Recommendations improve as a user saves more places (Priority: P1)

A user saves places over time. The system silently learns what they like — cuisine preferences, price comfort, distance tolerance, ambiance — and the recommendations they receive become progressively more aligned with those patterns. The user never fills in a preference form. Their behavior is the input.

**Why this priority**: This is the core product promise. Without a taste model that learns from saves, every recommendation is generic. This is what makes Kebi different from a generic search.

**Independent Test**: Save 5 places in the same cuisine and price range. Run a consultation. The primary recommendation should reflect that pattern without the user stating it explicitly.

**Acceptance Scenarios**:

1. **Given** a user has saved zero places, **When** they consult, **Then** they receive a generic population-level recommendation with no personalization applied
2. **Given** a user has saved places across 1–9 interactions, **When** they consult, **Then** recommendations blend their emerging taste with popular defaults (40% personal / 60% defaults)
3. **Given** a user has 10 or more interactions, **When** they consult, **Then** recommendations are driven by their full learned taste profile
4. **Given** a user saves a place, **When** the save is confirmed, **Then** the taste model updates without the user waiting — the save response is not delayed by the update

---

### User Story 2 — Onboarding actions immediately seed a taste profile (Priority: P2)

A brand new user with no saved places is shown a city starter pack and taste chips. Their confirmations and dismissals are treated as real behavioral signals — identical in kind to saves and recommendation feedback. When they receive their first recommendation, it already reflects what they chose during onboarding.

**Why this priority**: Cold start is the biggest drop-off risk. A user who gets a generic first recommendation has no reason to believe the system learns. Onboarding signals must produce an immediately visible effect.

**Independent Test**: A new user confirms 3 taste chips and dismisses 2. Their next consultation should reflect the confirmed preferences and not surface the dismissed ones.

**Acceptance Scenarios**:

1. **Given** a new user with zero prior interactions, **When** they confirm a taste chip, **Then** a behavioral signal is recorded with a positive weight and the taste profile updates
2. **Given** a new user with zero prior interactions, **When** they dismiss a taste chip, **Then** a behavioral signal is recorded with a negative weight and the taste profile updates
3. **Given** a new user who has confirmed taste chips, **When** they receive their first consultation, **Then** the recommendation reflects the confirmed preferences rather than generic defaults

---

### User Story 3 — Accepting or rejecting a recommendation shapes future ones (Priority: P3)

After receiving a recommendation, a user accepts the primary suggestion or picks an alternative. An acceptance means the recommendation was right. A rejection means it missed. Both signals are stored and used to move the taste profile in the correct direction for the next consultation.

**Why this priority**: Acceptance and rejection are the highest-quality signals the system can receive — they are deliberate, explicit choices. Without wiring them, the taste model can only learn from saves, which is a weaker and slower signal.

**Independent Test**: A user rejects 3 consecutive recommendations for a specific cuisine. Their next consultation should show reduced weighting for that cuisine without any explicit setting change.

**Acceptance Scenarios**:

1. **Given** a user receives a recommendation, **When** they accept the primary suggestion, **Then** a positive signal is recorded against the place's attributes with a weight of 2.0
2. **Given** a user receives a recommendation, **When** they reject it, **Then** a negative signal is recorded against the place's attributes with a weight of −1.5
3. **Given** either an acceptance or rejection signal, **When** the signal is recorded, **Then** the taste model confidence score increases to reflect the additional interaction

---

### User Story 4 — Taste profile confidence drives how much the system trusts the profile (Priority: P4)

The system tracks how confident it is in a user's taste profile. A user with 2 saves has a low-confidence profile; a user with 30 interactions has a high-confidence one. Confidence drives how much the system relies on the learned profile versus population defaults. It increases automatically with every interaction — users do not set it.

**Why this priority**: Without confidence, the system either over-trusts sparse data (poor early recommendations) or under-uses rich data (missed personalization for power users). Confidence is the mechanism that makes the blend adaptive.

**Independent Test**: Read confidence for a user with 0, 5, 10, and 20 interactions. Values must follow a smooth curve from 0.0 toward 1.0 and must match the defined formula at each checkpoint.

**Acceptance Scenarios**:

1. **Given** a user has zero interactions, **When** their confidence is read, **Then** it equals 0.0
2. **Given** a user accumulates interactions, **When** their confidence is read after each one, **Then** it increases monotonically and approaches but never reaches 1.0
3. **Given** a user's interaction count is 10, **When** their confidence is read, **Then** it equals approximately 0.63

---

### Edge Cases

- What happens when a place is saved but the embedding step fails? The save is confirmed, the taste model updates, and the missing embedding is logged as a known degraded state. The taste signal is not lost.
- What happens when the taste model background update fails? The failure is logged. The user-facing save response is not affected. The interaction count does not increment for that signal. If the interaction_log write itself fails, the taste_model cache is NOT updated — strict consistency: log is canonical.
- What happens when a user's taste profile has no stored record yet? The system returns zero-interaction defaults (all 8 dimensions at 0.5) rather than an error.
- What happens when a signal arrives for a place with incomplete metadata? The EMA update is skipped for dimensions that cannot be derived from the available metadata. The interaction is still logged and the count increments.
- What happens when the same place is saved twice (duplicate)? The deduplication path returns the existing place ID without creating a new save. No taste signal fires for the duplicate.

---

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST record every behavioral signal (save, accepted, rejected, onboarding confirmation, onboarding dismissal) in an append-only interaction log at the time the action occurs
- **FR-002**: The system MUST update the user's taste profile after each recorded interaction without blocking the user-facing response that triggered it
- **FR-003**: The system MUST apply different update rates per taste dimension — dimensions reflecting stable preferences update slowly; dimensions reflecting situational preferences update faster
- **FR-004**: The system MUST apply the correct EMA formula for positive and negative signals, deriving v_observation per dimension from a config-driven lookup table (`taste_model.observations` in app.yaml). Dimensions with no matching place metadata field default to 0.5 (neutral — no movement for that dimension).
- **FR-005**: The system MUST recompute the confidence score after every interaction using the formula: confidence = 1 − e^(−interaction_count / 10)
- **FR-006**: The system MUST route recommendations based on interaction count: zero-interaction users receive population defaults; users with 1–9 interactions receive a 40/60 personal-to-default blend; users with 10+ interactions receive their full stored taste profile
- **FR-007**: The system MUST return a taste vector for any user on any consultation request — never an error or empty response
- **FR-008**: The system MUST use the taste vector in consultation ranking so that recommendations reflect the user's learned preferences
- **FR-009**: The system MUST store gain values at write time in the interaction log so that changing gain configuration does not rewrite or invalidate historical data
- **FR-013**: The system MUST use atomic increments for interaction_count updates to prevent concurrent signal writes from producing incorrect counts
- **FR-014**: The system MUST abort the taste model cache update if the interaction_log write fails — partial writes (cache updated, log missing) are not permitted
- **FR-010**: The system MUST NOT make any AI model calls as part of taste model updates — the entire update path is deterministic
- **FR-011**: All gain values and dimension decay rates MUST be adjustable via configuration without a code deployment
- **FR-012**: A taste model update failure MUST NOT produce a user-facing error on the triggering action

### Key Entities

- **Taste Profile**: A user's learned preferences across 8 named dimensions (price comfort, cuisine frequency, distance tolerance, ambiance preference, crowd tolerance, cuisine adventurousness, time-of-day preference, dietary alignment), each a float in [0, 1]. Also carries a confidence score and a total interaction count. The stored profile is a derived cache — the interaction log is the source of truth and can reconstruct it.
- **Interaction Log**: An immutable, append-only record of every behavioral signal a user has generated. Each entry records the signal type, the place involved, the gain applied at write time, and the context (location, time of day, session). Never modified after creation.
- **Behavioral Signal**: A user action that carries taste information. Current scope: save (gain 1.0), accepted recommendation (gain 2.0), rejected recommendation (gain −1.5), onboarding confirmation (gain 1.2), onboarding dismissal (gain −0.8).

---

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Every save, acceptance, rejection, and onboarding signal produces an interaction log entry within the same request cycle — zero silent drops under normal operation
- **SC-002**: Taste profile confidence for a user with 10 interactions equals 0.63 ± 0.01
- **SC-003**: A user with zero interactions receives a taste vector where all 8 dimensions equal 0.5
- **SC-004**: A user with 10+ interactions receives their stored taste vector, not a blended or default value
- **SC-005**: The save response time is not measurably affected by the taste model update — the update runs after the response is sent
- **SC-006**: Consultation responses for users with 10+ interactions differ from responses for zero-interaction users on the same query — personalization is active and observable
- **SC-007**: Gain values and decay rates can be changed in configuration and take effect on the next interaction without a code deployment
- **SC-008**: A background taste model update failure produces a logged error and does not cause a failure response for the triggering user action

---

## Clarifications

### Session 2026-03-31

- Q: How is v_observation derived from place metadata for each EMA dimension? → A: Fixed lookup table in config — e.g. `price_range: {low: 1.0, mid: 0.5, high: 0.0}` maps to `price_comfort`. Dimensions with no matching place field default to 0.5 (neutral, no movement). All mappings live in `config/app.yaml` under `taste_model.observations`. No floats hardcoded in service code.
- Q: If interaction_log write fails, should the taste_model cache still update? → A: No — abort. If log_interaction() fails, do not update the cache. Log the error. The signal is lost for that request. Strict consistency: no log entry means no cache update, preserving log as the canonical replayable source of truth.
- Q: How should concurrent taste model updates for the same user be handled? → A: Atomic SQL increment — `UPDATE taste_model SET interaction_count = interaction_count + 1`. No SELECT FOR UPDATE needed. Avoids read-modify-write race without locking overhead.

---

## Assumptions

- Place metadata (cuisine, price range, ambiance indicators) is available at signal time for the EMA update. If metadata is partially missing, affected dimensions are skipped for that update only.
- The interaction log is append-only. Replaying the log from scratch produces the same taste profile as incremental updates.
- Gain values for deferred signal types (ignored, repeat visit, search accepted) are defined in configuration now but their triggers are not wired in this scope.
- Cluster bootstrapping for zero-interaction users (seeding from nearest-neighbor cluster centroid) is deferred. A stub returning 0.5 defaults covers the zero-interaction path for now.
- The feedback mechanism for recommendation acceptance/rejection (how the product app notifies this service) is in scope for wiring but its full contract with the product app is agreed separately.
