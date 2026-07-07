"""Helpers for safely interpolating user-derived content into LLM prompts.

Any string that originated from a user — a saved memory fact, a taste
profile summary the extractor wrote from user messages, the user's
intent, transcripts pulled from social URLs, captions — must NOT be
treated as instruction-grade by the model. We isolate that content by
wrapping it in XML tags carrying `trust="low"` and (where needed)
brace-escaping the content so `.format(...)` slots cannot be hijacked.

The accompanying system-prompt directive (see `config/prompts/agent.txt`)
tells the model to treat anything inside `trust="low"` tags as data,
never instruction.

This is a defense-in-depth measure. It does not make prompt injection
impossible — the model may still follow injected directives — but it
narrows the attack surface and makes ignored directives the trained
behavior rather than the exception.

Two helpers:

- `wrap_untrusted(content, tag)`: brace-escape, then wrap. Use when the
  wrapped result will be substituted into a parent template via
  `str.format(...)`.
- `wrap_untrusted_raw(content, tag, *, fmt=None)`: wrap without brace
  escape. Use when the result is concatenated directly into the prompt
  (no `.format(...)` pass) — e.g. a JSON-stringified payload where
  brace-escaping would mangle valid JSON syntax.
"""

from __future__ import annotations

_DEFAULT_MAX_LEN = 2000


def wrap_untrusted(
    content: str | None,
    tag: str,
    *,
    max_len: int = _DEFAULT_MAX_LEN,
) -> str:
    """Wrap `content` for safe `.format(...)` substitution.

    Brace-escapes `{` / `}` so an injected `{var}` cannot collide with
    later `.format(...)` calls on the parent template. Length-capped.
    Empty / None content returns an empty `<tag trust="low"/>` self-close
    so the slot stays visible to the model.
    """
    body = (content or "")[:max_len]
    body = body.replace("{", "{{").replace("}", "}}")
    if not body:
        return f'<{tag} trust="low"/>'
    return f'<{tag} trust="low">\n{body}\n</{tag}>'


def wrap_untrusted_raw(
    content: str | None,
    tag: str,
    *,
    max_len: int = _DEFAULT_MAX_LEN,
    fmt: str | None = None,
) -> str:
    """Wrap `content` without brace-escaping.

    Use when the wrapped result is concatenated directly into the
    prompt and is NOT later passed through `str.format(...)`. JSON
    payloads are the canonical case — brace-escaping would mangle
    valid JSON.

    Optional `fmt` is rendered as a `format="..."` attribute on the tag
    so the model has explicit signal about the payload shape.
    """
    body = (content or "")[:max_len]
    fmt_attr = f' format="{fmt}"' if fmt else ""
    if not body:
        return f'<{tag} trust="low"{fmt_attr}/>'
    return f'<{tag} trust="low"{fmt_attr}>\n{body}\n</{tag}>'
