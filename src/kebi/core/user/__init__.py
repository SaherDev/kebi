"""User-lifecycle services (AI-data erase for NestJS's account-delete flow)."""

from kebi.core.user.service import DataScope, UserDataDeletionService

__all__ = ["DataScope", "UserDataDeletionService"]
