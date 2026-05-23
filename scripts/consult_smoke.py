"""Smoke-test `find_saved` end-to-end through `POST /v1/chat`.

Hits a running local kebi server with a curated set of consult prompts
spanning the dimensions of the `find_saved` tool: geofence, named
neighborhood, named city, alternate location, movement profile (walking
vs driving, normal vs far reach), limit, multi-category OR, category-only
browse, feature/atmosphere tags, empty-result, no-location.

For each scenario:
- POSTs `/v1/chat`.
- Prints scenario name, HTTP status, response type, the `find_saved`
  reasoning step (if the agent called the tool), and a truncated prose
  preview.
- Tags PASS / WARN / FAIL based on response shape.

This is a manual debugging tool, not a CI test. The agent's behaviour
is non-deterministic; the assertions only check structural sanity
(`type=="agent"`, 200 response, etc.). Read the prose to judge whether
the agent picked the right tool args.

Usage:
    poetry run python scripts/consult_smoke.py            # all scenarios
    poetry run python scripts/consult_smoke.py rooftop    # filter by name
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
    expects_tool_call: bool = True


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
        expects_tool_call=True,  # tool still called, just returns empty
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
        expects_tool_call=True,
    ),
    Scenario(
        "no-location-no-city",
        "no location, no city — clarification path",
        "what's a good place for dinner?",
        location=None,
        movement_profile=None,
        expects_tool_call=False,  # agent should ask, not call the tool
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


def _find_step(steps: list[dict[str, Any]], prefix: str) -> dict[str, Any] | None:
    for s in steps:
        if (s.get("step") or "").startswith(prefix):
            return s
    return None


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
    find_step = _find_step(steps, "find_saved")
    if scenario.expects_tool_call:
        if find_step is None:
            return "WARN", ["expected find_saved step, agent didn't call the tool"]
    else:
        if find_step is not None:
            reasons.append("agent called tool but scenario expected clarification path")
    if find_step and find_step.get("step", "").endswith(".failure"):
        return "WARN", ["find_saved.failure (timeout / exception)"]
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
        f"{BOLD}[{idx}/{n}] {scenario.name}{RESET}  "
        f"{DIM}({scenario.dimension}){RESET}"
    )
    print(header)
    print(f"  message: {scenario.message!r}")
    print(f"  {color}{verdict}{RESET}  {DIM}{elapsed_ms}ms  HTTP {status}{RESET}")
    if reasons:
        for r in reasons:
            print(f"  {YELLOW}note: {r}{RESET}")
    steps = (body.get("data") or {}).get("reasoning_steps") or []
    find_step = _find_step(steps, "find_saved")
    if find_step:
        print(f"  {CYAN}find_saved: {find_step.get('summary')}{RESET}")
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
    name_filter = next(
        (a for a in argv[1:] if not a.startswith("--")), None
    )
    selected = [
        s for s in SCENARIOS
        if name_filter is None or name_filter in s.name
    ]
    if list_only:
        for s in SCENARIOS:
            print(f"  {s.name}  {DIM}({s.dimension}){RESET}")
        return 0
    if not selected:
        print(f"{RED}no scenarios match filter {name_filter!r}{RESET}")
        return 2

    print(
        f"{BOLD}Running {len(selected)} consult scenario(s) against "
        f"{BASE_URL}{RESET}\n"
    )
    pass_n = warn_n = fail_n = 0
    with httpx.Client() as client:
        for i, scenario in enumerate(selected, 1):
            t0 = time.monotonic()
            try:
                status, body = _post_chat(scenario, client)
            except httpx.HTTPError as exc:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                print(f"{RED}[{i}/{len(selected)}] {scenario.name}  "
                      f"transport error: {exc}{RESET}\n")
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
