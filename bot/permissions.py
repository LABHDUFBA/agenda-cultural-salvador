"""Server-side authorisation.

Roles are always read from the database, keyed by the Telegram numeric id taken
from the *update envelope*, never from a command argument or callback payload.
Anything the client sends about its own identity is untrusted input.
"""

from __future__ import annotations

from typing import Optional, Protocol

from .db import AuditRepository, UserRepository
from .models import PermissionDenied, UserRole


class RoleResolver(Protocol):
    def role_of(self, telegram_id: int) -> UserRole: ...


class PermissionService:
    def __init__(self, users: UserRepository, audit: AuditRepository):
        self._users = users
        self._audit = audit

    def role_of(self, telegram_id: Optional[int]) -> UserRole:
        if not telegram_id or telegram_id <= 0:
            return UserRole.PUBLIC
        return self._users.get_role(int(telegram_id))

    def is_at_least(self, telegram_id: Optional[int], minimum: UserRole) -> bool:
        return self.role_of(telegram_id).rank >= minimum.rank

    def require(self, telegram_id: Optional[int], minimum: UserRole, action: str) -> UserRole:
        """Return the caller's role or raise + audit a denial."""
        role = self.role_of(telegram_id)
        if role.rank < minimum.rank:
            self._audit.log(
                actor_id=telegram_id,
                action=action,
                entity="user",
                entity_id=str(telegram_id) if telegram_id else None,
                outcome="denied",
                detail=f"perfil {role.value} < {minimum.value}",
            )
            raise PermissionDenied(
                "Ação restrita. Seu perfil não tem permissão para isso."
            )
        return role
