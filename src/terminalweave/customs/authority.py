from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from terminalweave.core.clock import ensure_utc
from terminalweave.core.errors import ValidationError

@dataclass(frozen=True)
class AuthorityMessage:
    message_id: str
    container_id: str
    authority: str
    message_type: str
    issued_at: datetime
    supersedes: str | None = None
    manifest_version: int | None = None

    def __post_init__(self):
        object.__setattr__(self, 'issued_at', ensure_utc(self.issued_at))

class AuthorityInbox:
    """Stores official messages and derives the effective set.

    Ingestion is idempotent and independent of arrival order: replaying an
    identical message is a no-op, and a superseding message may arrive before
    the message it replaces. The effective set is computed from message
    content and issue time only, so out-of-order replays converge to the same
    state instead of accumulating duplicates.
    """

    def __init__(self):
        self.messages: dict[str, AuthorityMessage] = {}

    def ingest(self, m: AuthorityMessage) -> None:
        existing = self.messages.get(m.message_id)
        if existing is not None:
            if existing == m:
                return
            raise ValidationError('conflicting authority message')
        if m.supersedes:
            if m.supersedes == m.message_id:
                raise ValidationError('message cannot supersede itself')
            old = self.messages.get(m.supersedes)
            if old is not None:
                self._check_supersession(old, m)
        for other in self.messages.values():
            if other.supersedes == m.message_id:
                self._check_supersession(m, other)
        self.messages[m.message_id] = m

    @staticmethod
    def _check_supersession(old: AuthorityMessage, new: AuthorityMessage) -> None:
        if old.container_id != new.container_id or old.authority != new.authority:
            raise ValidationError('supersession scope mismatch')
        if new.issued_at <= old.issued_at:
            raise ValidationError('superseding message must be newer')

    def effective(self, container_id: str) -> tuple[AuthorityMessage, ...]:
        superseded = {m.supersedes for m in self.messages.values() if m.supersedes}
        values = [m for m in self.messages.values() if m.container_id == container_id and m.message_id not in superseded]
        return tuple(sorted(values, key=lambda x: (x.authority, x.issued_at, x.message_id)))

    def latest(self, container_id: str, authority: str, message_type: str) -> AuthorityMessage | None:
        values = [m for m in self.effective(container_id) if m.authority == authority and m.message_type == message_type]
        return max(values, key=lambda x: (x.issued_at, x.message_id)) if values else None
