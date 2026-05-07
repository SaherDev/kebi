"""Response schemas for GET /v1/user/context.

The actual shapes live in core/taste/schemas.py — this module re-exports
them so external references (OpenAPI discovery, product-side type
generation) can keep importing from the api.schemas path.
"""

from kebi.core.taste.schemas import ChipView, UserContext

# Backward-compat aliases for existing imports.
ChipResponse = ChipView
UserContextResponse = UserContext

__all__ = [
    "ChipResponse",
    "ChipView",
    "UserContext",
    "UserContextResponse",
]
