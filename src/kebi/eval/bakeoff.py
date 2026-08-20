"""Live model bakeoff — one role, several config options, one golden set (ADR-175).

    poetry run python -m kebi.eval.bakeoff --role extractor \
        --options current luna qwen-plus
    poetry run python -m kebi.eval.bakeoff --role location_resolver \
        --options current gemini-flash
    poetry run python -m kebi.eval.bakeoff --role extractor \
        --options current --prompt /tmp/trimmed.txt

Each named option comes from the role's optioned block in `config/app.yaml`
(`current` = the block's `default`), runs the role's REAL prompt and REAL
response schema over the committed golden set, and reports quality,
pass-rate, latency, and cost side by side. `--prompt` swaps the prompt
template for every option — the prompt-parity mode that gates trims
(ADR-174): same model, current-vs-trimmed prompt, same scores or no ship.

All calls are traced under feature `eval` so they are distinguishable from
production traffic in Langfuse and the cost report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from kebi.core.agent._trace_context import traced_call
from kebi.core.agent.location import LocationResolution
from kebi.core.config import LLMRoleConfig, get_config, get_env, load_yaml_config
from kebi.core.extraction.enrichers.llm_resolver import _ResolverResponse
from kebi.eval.golden import GoldenCase, GoldenSuite, load_suite
from kebi.providers.llm import InstructorClient

_DYNAMIC_MARKER = "<<<DYNAMIC_CONTEXT>>>"


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class CaseResult(BaseModel):
    option: str
    case_id: str
    score: float
    passed: bool
    latency_ms: float
    cost_usd: float | None = None
    error: str | None = None


class OptionSummary(BaseModel):
    option: str
    provider: str
    model: str
    quality: float
    pass_rate: float
    p50_latency_ms: float
    p95_latency_ms: float
    total_cost_usd: float
    unpriced_calls: int
    error_cases: int
    cost_per_1k_cases_usd: float


class BakeoffReport(BaseModel):
    role: str
    case_count: int
    pass_floor: float
    prompt_override: str | None = None
    options: list[OptionSummary] = Field(default_factory=list)
    results: list[CaseResult] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Role adapters — real prompt + real schema + a scorer per role
# ---------------------------------------------------------------------------


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", value.lower()).strip()


class RoleAdapter(Protocol):
    role: str
    schema: type[BaseModel]

    def prompt_template(self) -> str: ...
    def messages(self, case: GoldenCase, template: str) -> tuple[str, str]: ...
    def score(self, case: GoldenCase, output: BaseModel) -> float: ...


class LocationResolverAdapter:
    """Mirrors the resolve_location node: rules head as system, per-turn
    inputs as the user message, `LocationResolution` schema."""

    role = "location_resolver"
    schema: type[BaseModel] = LocationResolution

    def prompt_template(self) -> str:
        return get_config().prompts["location_resolver"].content

    def messages(self, case: GoldenCase, template: str) -> tuple[str, str]:
        head, _, tail = template.partition(_DYNAMIC_MARKER)
        slots = {
            "current_message": case.input.get("current_message", "(empty)"),
            "conversation_history": case.input.get(
                "conversation_history", "(no prior messages)"
            ),
            "user_actual_location": case.input.get("user_actual_location", "null"),
            "previous_working_location": case.input.get(
                "previous_working_location", "null"
            ),
            "distance_from_previous": case.input.get(
                "distance_from_previous", "first turn — no previous working location"
            ),
            "mobility_profile": case.input.get("mobility_profile", "(default)"),
        }
        return head.strip(), tail.format(**slots).strip()

    def score(self, case: GoldenCase, output: BaseModel) -> float:
        """Fraction of expected fields the resolution got right."""
        expected = case.expected
        if not expected:
            return 0.0
        hits = 0
        for field, want in expected.items():
            got = getattr(output, field, None)
            if isinstance(want, str) and isinstance(got, str):
                hits += _normalized(got) == _normalized(want)
            else:
                hits += got == want
        return hits / len(expected)


class ExtractorAdapter:
    """Mirrors the LLMResolver extraction stage: place_resolver prompt as
    system, the post's content block as the user message. Scored as place
    name F1 against the expected set — the metric that matters is
    which places came out."""

    role = "extractor"
    schema: type[BaseModel] = _ResolverResponse

    def prompt_template(self) -> str:
        return get_config().prompts["place_resolver"].content

    def messages(self, case: GoldenCase, template: str) -> tuple[str, str]:
        return template.strip(), str(case.input.get("user_content", "")).strip()

    def score(self, case: GoldenCase, output: BaseModel) -> float:
        expected = {_normalized(p) for p in case.expected.get("places", [])}
        resolver = output  # _ResolverResponse
        predicted = {
            _normalized(c.raw_name)
            for c in getattr(resolver, "candidates", [])
            if getattr(c, "raw_name", "")
        } | {
            _normalized(d.name)
            for d in getattr(resolver, "discovered", [])
            if getattr(d, "name", "")
        }
        predicted.discard("")
        if not expected and not predicted:
            return 1.0
        if not expected or not predicted:
            return 0.0
        tp = len(expected & predicted)
        precision = tp / len(predicted)
        recall = tp / len(expected)
        if precision + recall == 0:
            return 0.0
        return 2 * precision * recall / (precision + recall)


_ADAPTERS: dict[str, Any] = {
    "location_resolver": LocationResolverAdapter,
    "extractor": ExtractorAdapter,
    # orchestrator: deliberately absent — its bakeoff needs the bound tool
    # loop and an LLM judge, built when its swap round starts (ADR-175).
}


# ---------------------------------------------------------------------------
# Option resolution + clients
# ---------------------------------------------------------------------------


def resolve_options(role: str, names: list[str]) -> dict[str, LLMRoleConfig]:
    """Map requested option names to LLMRoleConfig from the RAW yaml block.

    The runtime config flattens options away at boot — the bakeoff reads
    the unresolved block so inactive candidates stay reachable. `current`
    aliases the block's `default`.
    """
    block = load_yaml_config("app.yaml")["models"].get(role)
    if not isinstance(block, dict):
        raise ValueError(f"models.{role} not found in app.yaml")
    if "default" not in block:
        raise ValueError(f"models.{role} is flat — nothing to bake off against")
    resolved: dict[str, LLMRoleConfig] = {}
    for name in names:
        key = block["default"] if name == "current" else name
        if key not in block or key in ("default", "advanced"):
            options = [k for k in block if k not in ("default", "advanced")]
            raise ValueError(f"models.{role} has no option {key!r}; have {options}")
        resolved[name] = LLMRoleConfig(**block[key])
    return resolved


async def _call_option(
    option: LLMRoleConfig, schema: type[BaseModel], system_text: str, user_text: str
) -> tuple[BaseModel, dict[str, int] | None]:
    """One structured call through the production-equivalent client shape."""
    if option.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = ChatAnthropic(
            model=option.model,
            max_tokens_to_sample=option.max_tokens,
            temperature=option.temperature,
            api_key=get_env().ANTHROPIC_API_KEY,
            timeout=option.timeout_seconds,
            max_retries=0,
            stop=None,
        )
        structured = llm.with_structured_output(schema, include_raw=True)
        raw = await structured.ainvoke(
            [SystemMessage(content=system_text), HumanMessage(content=user_text)]
        )
        if isinstance(raw, dict):
            if raw.get("parsing_error") is not None or raw.get("parsed") is None:
                raise ValueError(f"schema parse failed: {raw.get('parsing_error')}")
            from kebi.core.agent.graph import _ai_message_usage

            return raw["parsed"], (_ai_message_usage(raw.get("raw")) or None)
        return raw, None

    if option.provider in ("openai", "openrouter", "ollama"):
        env = get_env()
        if option.provider == "openrouter":
            api_key, base_url = (
                env.OPENROUTER_API_KEY,
                get_config().providers.openrouter.base_url,
            )
        elif option.provider == "ollama":
            api_key, base_url = "ollama", get_config().providers.ollama.base_url
        else:
            api_key, base_url = env.OPENAI_API_KEY, None
        client = InstructorClient(
            model=option.model,
            api_key=api_key,
            base_url=base_url,
            max_retries=option.max_retries,
            timeout_seconds=option.timeout_seconds,
            reasoning_effort=option.reasoning_effort,
        )
        extraction = await client.extract(
            response_model=schema,
            messages=[
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
        )
        return extraction.data, extraction.usage

    raise ValueError(f"Bakeoff has no client for provider {option.provider!r}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run_bakeoff(
    role: str,
    option_names: list[str],
    *,
    pass_floor: float = 0.8,
    prompt_override: Path | None = None,
    suite: GoldenSuite | None = None,
) -> BakeoffReport:
    adapter_cls = _ADAPTERS.get(role)
    if adapter_cls is None:
        raise ValueError(f"No bakeoff adapter for role {role!r}; have {[*_ADAPTERS]}")
    adapter = adapter_cls()
    suite = suite or load_suite(role)
    options = resolve_options(role, option_names)
    template = (
        prompt_override.read_text() if prompt_override else adapter.prompt_template()
    )
    pricing = get_config().pricing

    results: list[CaseResult] = []
    for option_name, option in options.items():
        for case in suite.cases:
            system_text, user_text = adapter.messages(case, template)
            started = time.perf_counter()
            async with traced_call(
                f"eval.bakeoff.{role}",
                "eval",
                model=option.model,
                extra={"option": option_name, "case": case.id},
                standalone=True,
            ) as t:
                try:
                    output, usage = await _call_option(
                        option, adapter.schema, system_text, user_text
                    )
                except Exception as exc:  # a failed call scores zero, run goes on
                    t.fail(exc)
                    results.append(
                        CaseResult(
                            option=option_name,
                            case_id=case.id,
                            score=0.0,
                            passed=False,
                            latency_ms=(time.perf_counter() - started) * 1000,
                            error=str(exc),
                        )
                    )
                    continue
                latency_ms = (time.perf_counter() - started) * 1000
                score = adapter.score(case, output)
                t.usage = usage
                t.output = {"score": score}
                results.append(
                    CaseResult(
                        option=option_name,
                        case_id=case.id,
                        score=score,
                        passed=score >= pass_floor,
                        latency_ms=latency_ms,
                        cost_usd=pricing.llm_cost_for(option.model, usage),
                    )
                )

    summaries = [
        _summarize(name, options[name], [r for r in results if r.option == name])
        for name in options
    ]
    return BakeoffReport(
        role=role,
        case_count=len(suite.cases),
        pass_floor=pass_floor,
        prompt_override=str(prompt_override) if prompt_override else None,
        options=sorted(summaries, key=lambda s: s.quality, reverse=True),
        results=results,
    )


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100)[max(int(fraction * 100) - 1, 0)]


def _summarize(
    name: str, option: LLMRoleConfig, rows: list[CaseResult]
) -> OptionSummary:
    latencies = [r.latency_ms for r in rows]
    priced = [r.cost_usd for r in rows if r.cost_usd is not None]
    total_cost = sum(priced)
    return OptionSummary(
        option=name,
        provider=option.provider,
        model=option.model,
        quality=statistics.mean([r.score for r in rows]) if rows else 0.0,
        pass_rate=(sum(r.passed for r in rows) / len(rows)) if rows else 0.0,
        p50_latency_ms=_percentile(latencies, 0.50),
        p95_latency_ms=_percentile(latencies, 0.95),
        total_cost_usd=total_cost,
        unpriced_calls=sum(1 for r in rows if r.cost_usd is None and r.error is None),
        error_cases=sum(1 for r in rows if r.error is not None),
        cost_per_1k_cases_usd=(total_cost / len(priced) * 1000) if priced else 0.0,
    )


def render_markdown(report: BakeoffReport) -> str:
    lines = [
        f"# Bakeoff — role `{report.role}` — {report.case_count} cases "
        f"(pass ≥ {report.pass_floor})",
    ]
    if report.prompt_override:
        lines.append(f"Prompt override: `{report.prompt_override}`")
    lines += [
        "",
        "| option | model | quality | pass | p50 ms | p95 ms | $/1k cases | errors |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in report.options:
        lines.append(
            f"| {s.option} | {s.model} | {s.quality:.3f} | {s.pass_rate:.0%} "
            f"| {s.p50_latency_ms:.0f} | {s.p95_latency_ms:.0f} "
            f"| {s.cost_per_1k_cases_usd:.2f} | {s.error_cases} |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=sorted(_ADAPTERS))
    parser.add_argument(
        "--options",
        nargs="+",
        required=True,
        help="option keys from the role's app.yaml block ('current' = default)",
    )
    parser.add_argument("--pass-floor", type=float, default=0.8)
    parser.add_argument("--prompt", type=Path, help="prompt-template override file")
    parser.add_argument("--json-out", type=Path, help="write the full report as JSON")
    args = parser.parse_args()
    report = asyncio.run(
        run_bakeoff(
            args.role,
            args.options,
            pass_floor=args.pass_floor,
            prompt_override=args.prompt,
        )
    )
    print(render_markdown(report))
    if args.json_out:
        args.json_out.write_text(json.dumps(report.model_dump(), indent=2))
        print(f"\nFull report: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
