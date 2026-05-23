"""Smoke-test `find_saved` + `suggest_places` end-to-end through `POST /v1/chat`.

Hits a running local kebi server with a curated set of consult prompts
spanning both tools:

- `find_saved` — geofence, named neighborhood, named city, alternate
  location, movement profile, limit, multi-category OR, category-only
  browse, feature/atmosphere tags, empty-result, no-location.
- `suggest_places` — public-knowledge picks (famous spots, "I've never
  been here"), both-tools open-ended intents, constraint-aware suggest
  (the agent must pass dietary `tags` to both tools).

For each scenario:
- POSTs `/v1/chat`.
- Prints scenario name, HTTP status, response type, every tool
  reasoning step (multi-step user-fluent narration for suggest_places),
  per-source candidate counts, and a truncated prose preview.
- Tags PASS / WARN / FAIL based on response shape and which tools the
  agent actually called vs the scenario's expectations.

This is a manual debugging tool, not a CI test. The agent's behaviour
is non-deterministic; the assertions only check structural sanity
(`type=="agent"`, 200 response, expected tool(s) called). Read the
prose to judge whether the agent picked the right tool args.

Usage:
    poetry run python scripts/consult_smoke.py            # all scenarios
    poetry run python scripts/consult_smoke.py rooftop    # filter by name
    poetry run python scripts/consult_smoke.py suggest    # only suggest_places
    poetry run python scripts/consult_smoke.py --list     # list scenario names
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

BASE_URL = "http://localhost:8000"
USER_ID = "user_3AhqBhtLzKKlbKrjVNGTHro1o76"
TIMEOUT_S = 60

# ANSI color codes for readable output.
RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"


@dataclass
class Scenario:
    name: str
    dimension: str
    message: str
    location: dict[str, float] | None = field(
        default_factory=lambda: {"lat": 13.7563, "lng": 100.5018}
    )
    movement_profile: dict[str, Any] | None = field(
        default_factory=lambda: {
            "available_modes": ["walking", "transit"],
            "reach": "normal",
        }
    )
    # Which tool(s) the agent should call this turn. {"find_saved"} is
    # the default — preserves existing find_saved-only scenarios.
    # {"suggest_places"} for new-name discovery; {"find_saved",
    # "suggest_places"} for open-ended turns. Empty set means "no tool"
    # (clarification path).
    expects_tools: set[str] = field(default_factory=lambda: {"find_saved"})


# Geographic anchors used by tests:
BANGKOK = {"lat": 13.7563, "lng": 100.5018}
KOH_SAMUI = {"lat": 9.5018, "lng": 100.0136}
AMSTERDAM = {"lat": 52.3676, "lng": 4.9041}

WALKING_NORMAL = {
    "available_modes": ["walking", "transit"],
    "reach": "normal",
}
DRIVING_FAR = {
    "available_modes": ["driving"],
    "reach": "far",
}
DRIVING_NORMAL = {
    "available_modes": ["driving", "walking"],
    "reach": "normal",
}
TRANSIT_NORMAL = {
    "available_modes": ["transit", "walking"],
    "reach": "normal",
}
WALKING_COMPACT = {
    "available_modes": ["walking"],
    "reach": "compact",
}


SCENARIOS: list[Scenario] = [
    # ----- Geofence-driven (Bangkok, walking) -----
    Scenario(
        "rooftop-bar-nearby",
        "geofence + bar category",
        "any good rooftop bar nearby for tonight?",
    ),
    Scenario(
        "park-around-here",
        "geofence + park category + atmosphere",
        "what's a chill park to spend the afternoon around here?",
    ),
    Scenario(
        "temple-walkable",
        "geofence + temple category",
        "any temples worth visiting around here?",
    ),
    Scenario(
        "market-evening",
        "geofence + market category + time",
        "good night market for dinner and a wander?",
    ),
    Scenario(
        "viewpoint-sunset",
        "geofence + viewpoint/scenic_view tag",
        "somewhere with a great view for sunset?",
    ),
    Scenario(
        "shopping-anywhere",
        "geofence + shopping_mall category",
        "where should I go shopping?",
        movement_profile=TRANSIT_NORMAL,
    ),
    Scenario(
        "museum-browse",
        "category-only browse",
        "any museums worth visiting?",
        movement_profile=TRANSIT_NORMAL,
    ),
    # ----- Multi-category / OR semantics -----
    Scenario(
        "food-or-drink",
        "multi-category OR",
        "somewhere good to eat or grab a drink tonight, I'm flexible",
    ),
    Scenario(
        "outdoor-afternoon",
        "multi-category OR (park / beach / garden)",
        "somewhere outdoors for the afternoon, I don't care if it's a park or garden",
    ),
    # ----- Named neighborhood -----
    Scenario(
        "chinatown",
        "named neighborhood",
        "anything cool in Chinatown I should check out?",
    ),
    Scenario(
        "sathon",
        "named neighborhood (smaller)",
        "any parks in Sathon you'd recommend?",
    ),
    # ----- Named city, agent should drop geofence -----
    Scenario(
        "amsterdam-from-bangkok",
        "named city, foreign country, agent drops Bangkok geofence",
        "what should I see when I'm in Amsterdam?",
    ),
    Scenario(
        "zaandam-recall",
        "named city (small Dutch town)",
        "any saves I have in Zaandam?",
    ),
    Scenario(
        "phuket-empty-city",
        "named city not in saves (empty path)",
        "what should I do in Phuket?",
        expects_tools={"find_saved"},  # tool still called, just returns empty
    ),
    # ----- Alternate physical location -----
    Scenario(
        "samui-beach",
        "alternate lat/lng (Koh Samui) + driving",
        "good beach spot to check out?",
        location=KOH_SAMUI,
        movement_profile=DRIVING_NORMAL,
    ),
    Scenario(
        "samui-temple",
        "alternate lat/lng + temple category",
        "any famous temples around here?",
        location=KOH_SAMUI,
        movement_profile=DRIVING_NORMAL,
    ),
    Scenario(
        "amsterdam-from-amsterdam",
        "alternate lat/lng (Amsterdam) — geofence + named-area parity",
        "what's worth visiting around here?",
        location=AMSTERDAM,
        movement_profile=WALKING_NORMAL,
    ),
    # ----- Movement profile variations -----
    Scenario(
        "day-trip-driving-far",
        "driving + far reach widens radius",
        "I'm up for a day trip from Bangkok — any temples or sights worth driving to?",
        movement_profile=DRIVING_FAR,
    ),
    Scenario(
        "compact-walking",
        "walking + compact reach narrows radius",
        "right around the corner — any chill spot to sit?",
        movement_profile=WALKING_COMPACT,
    ),
    # ----- Limit picking -----
    Scenario(
        "single-pick-rooftop",
        "limit=1 (single confident pick)",
        "just pick one rooftop bar — don't give me options, I'll trust you",
    ),
    Scenario(
        "small-comparison",
        "small limit (a few options)",
        "give me two or three options for evening drinks",
    ),
    # ----- Atmosphere / vibe tags -----
    Scenario(
        "trendy-vibe",
        "atmosphere tag (trendy)",
        "somewhere trendy and instagrammable?",
    ),
    Scenario(
        "luxurious-vibe",
        "atmosphere tag (luxurious)",
        "somewhere fancy and luxurious for a treat?",
    ),
    # ----- Specific cuisine likely empty -----
    Scenario(
        "italian-empty",
        "cuisine tag not in saves (empty)",
        "any Italian spot you remember from my list?",
    ),
    Scenario(
        "japanese-empty",
        "cuisine tag not in saves (empty) + ramen",
        "any good ramen spot you've seen me save?",
    ),
    Scenario(
        "moroccan-empty",
        "cuisine likely empty",
        "what about somewhere Moroccan?",
    ),
    # ----- No location supplied -----
    Scenario(
        "no-location-explicit-city",
        "no location field, agent names city in message",
        "any cafes you'd recommend in Bangkok?",
        location=None,
        movement_profile=None,
        expects_tools={"find_saved"},
    ),
    Scenario(
        "no-location-no-city",
        "no location, no city — clarification path",
        "what's a good place for dinner?",
        location=None,
        movement_profile=None,
        expects_tools=set(),  # agent should ask, not call any tool
    ),
    # ----- suggest_places only -----
    Scenario(
        "suggest-famous-omakase-tokyo",
        "suggest_places — named city, public knowledge",
        "I'm in Tokyo next week — what are the famous omakase spots?",
        location=None,
        movement_profile=None,
        expects_tools={"suggest_places"},
    ),
    Scenario(
        "suggest-coffee-around-here",
        "suggest_places — open intent on public-knowledge picks",
        "I've never tried any cafes around here — any well-known ones?",
        expects_tools={"suggest_places"},
    ),
    Scenario(
        "suggest-italian-neighborhood",
        "suggest_places — haven't tried + named cuisine",
        "haven't tried Italian in this neighborhood — anything worth going to?",
        expects_tools={"suggest_places"},
    ),
    Scenario(
        "suggest-lisbon-lunch",
        "suggest_places — visiting another city",
        "what are people loving for lunch in Lisbon these days?",
        location=None,
        movement_profile=None,
        expects_tools={"suggest_places"},
    ),
    # ----- both tools in one turn -----
    Scenario(
        "both-dinner-tonight",
        "open intent — saves first, suggest tail",
        "where should I eat dinner tonight? open to anything mine or new",
        expects_tools={"find_saved", "suggest_places"},
    ),
    Scenario(
        "both-chill-afternoon",
        "open intent — afternoon mood",
        "somewhere chill for the afternoon, mine or famous",
        expects_tools={"find_saved", "suggest_places"},
    ),
    # ----- suggest_places + hard constraint awareness -----
    # NOTE: assumes the user has memory like "I'm vegetarian" pre-seeded.
    # Without that, the constraint won't bind. The scenario name is a hint
    # to the operator running the smoke; assertions remain structural.
    Scenario(
        "suggest-veg-famous-spots",
        "suggest_places — vegetarian constraint must be passed through",
        "famous spots for lunch around here?",
        expects_tools={"suggest_places"},
    ),
    Scenario(
        "suggest-out-of-scope-name-bait",
        "suggest_places — 'best anywhere' must stay in-radius",
        "best ramen anywhere — I'm flexible",
        expects_tools={"suggest_places"},
    ),
]


def _post_chat(scenario: Scenario, client: httpx.Client) -> tuple[int, dict[str, Any]]:
    body: dict[str, Any] = {
        "user_id": USER_ID,
        "message": scenario.message,
    }
    if scenario.location is not None:
        body["location"] = scenario.location
    if scenario.movement_profile is not None:
        body["movement_profile"] = scenario.movement_profile
    r = client.post(f"{BASE_URL}/v1/chat", json=body, timeout=TIMEOUT_S)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text}


_KNOWN_TOOLS: tuple[str, ...] = ("find_saved", "suggest_places")


def _steps_for_tool(steps: list[dict[str, Any]], tool: str) -> list[dict[str, Any]]:
    """Return every reasoning step emitted by `tool`, in order."""
    prefix = f"{tool}."
    return [s for s in steps if (s.get("step") or "").startswith(prefix)]


def _tools_called(steps: list[dict[str, Any]]) -> set[str]:
    """Set of tools that emitted at least one reasoning step this turn."""
    return {tool for tool in _KNOWN_TOOLS if _steps_for_tool(steps, tool)}


def _candidate_source_counts(body: dict[str, Any]) -> dict[str, int]:
    """Tally returned candidates by their `source` discriminator.

    The agent's prose response carries a structured `places` list (when
    one is exposed) — we read the per-source breakdown from there if it
    exists, otherwise we count namer/find summary steps as a fallback
    hint. Reads tolerantly; structural smoke only.
    """
    data = body.get("data") or {}
    places = data.get("places") or data.get("candidates") or []
    counts: dict[str, int] = {}
    for p in places:
        src = p.get("source") or ((p.get("user_data") and "saved") or "unknown")
        counts[src] = counts.get(src, 0) + 1
    return counts


def _truncate(text: str, n: int = 200) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


def _evaluate(
    scenario: Scenario, status: int, body: dict[str, Any]
) -> tuple[str, list[str]]:
    """Return (verdict, reasons) where verdict ∈ {PASS, WARN, FAIL}."""
    reasons: list[str] = []
    if status != 200:
        return "FAIL", [f"HTTP {status}"]
    rtype = body.get("type")
    if rtype != "agent":
        return "FAIL", [f"type={rtype!r}"]

    steps = (body.get("data") or {}).get("reasoning_steps") or []
    called = _tools_called(steps)
    expected = scenario.expects_tools

    # Tool-routing assertion: which tools fired vs which were expected.
    if expected and not (expected & called):
        return "WARN", [
            f"expected one of {sorted(expected)} to fire, "
            f"agent called {sorted(called) or 'none'}"
        ]
    if not expected and called:
        reasons.append(
            f"agent called {sorted(called)} but scenario expected "
            "no tool (clarification path)"
        )

    # Per-tool failure breadcrumbs.
    for tool in called:
        last = _steps_for_tool(steps, tool)[-1]
        if (last.get("step") or "").endswith(".failure"):
            return "WARN", [f"{tool}.failure (timeout / exception)"]

    return "PASS", reasons


def _print_result(
    idx: int,
    n: int,
    scenario: Scenario,
    elapsed_ms: int,
    status: int,
    body: dict[str, Any],
) -> bool:
    verdict, reasons = _evaluate(scenario, status, body)
    color = {"PASS": GREEN, "WARN": YELLOW, "FAIL": RED}[verdict]
    header = (
        f"{BOLD}[{idx}/{n}] {scenario.name}{RESET}  {DIM}({scenario.dimension}){RESET}"
    )
    print(header)
    print(f"  message: {scenario.message!r}")
    print(f"  {color}{verdict}{RESET}  {DIM}{elapsed_ms}ms  HTTP {status}{RESET}")
    if reasons:
        for r in reasons:
            print(f"  {YELLOW}note: {r}{RESET}")
    steps = (body.get("data") or {}).get("reasoning_steps") or []
    for tool in _KNOWN_TOOLS:
        tool_steps = _steps_for_tool(steps, tool)
        if not tool_steps:
            continue
        print(f"  {CYAN}{tool}:{RESET}")
        for ts in tool_steps:
            step_id = (ts.get("step") or "").rsplit(".", 1)[-1] or "?"
            print(f"    {DIM}[{step_id}]{RESET} {CYAN}{ts.get('summary')}{RESET}")
    source_counts = _candidate_source_counts(body)
    if source_counts:
        breakdown = ", ".join(f"{src}={n}" for src, n in sorted(source_counts.items()))
        print(f"  {DIM}sources: {breakdown}{RESET}")
    prose = body.get("message") or ""
    if prose:
        print(f"  prose: {_truncate(prose, 240)}")
    tcu = body.get("tool_calls_used")
    if tcu is not None:
        print(f"  {DIM}tool_calls_used={tcu}{RESET}")
    print()
    return verdict == "PASS"


def main(argv: list[str]) -> int:
    list_only = "--list" in argv
    name_filter = next((a for a in argv[1:] if not a.startswith("--")), None)
    selected = [s for s in SCENARIOS if name_filter is None or name_filter in s.name]
    if list_only:
        for s in SCENARIOS:
            print(f"  {s.name}  {DIM}({s.dimension}){RESET}")
        return 0
    if not selected:
        print(f"{RED}no scenarios match filter {name_filter!r}{RESET}")
        return 2

    print(
        f"{BOLD}Running {len(selected)} consult scenario(s) against {BASE_URL}{RESET}\n"
    )
    pass_n = warn_n = fail_n = 0
    with httpx.Client() as client:
        for i, scenario in enumerate(selected, 1):
            t0 = time.monotonic()
            try:
                status, body = _post_chat(scenario, client)
            except httpx.HTTPError as exc:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                print(
                    f"{RED}[{i}/{len(selected)}] {scenario.name}  "
                    f"transport error: {exc}{RESET}\n"
                )
                fail_n += 1
                continue
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            verdict, _ = _evaluate(scenario, status, body)
            if verdict == "PASS":
                pass_n += 1
            elif verdict == "WARN":
                warn_n += 1
            else:
                fail_n += 1
            _print_result(i, len(selected), scenario, elapsed_ms, status, body)

    print(
        f"{BOLD}Summary:{RESET} "
        f"{GREEN}{pass_n} pass{RESET}  "
        f"{YELLOW}{warn_n} warn{RESET}  "
        f"{RED}{fail_n} fail{RESET}  "
        f"({len(selected)} total)"
    )
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
