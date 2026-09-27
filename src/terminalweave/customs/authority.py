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
        if self.supersedes is not None and self.supersedes == self.message_id:
            raise ValidationError('message cannot supersede itself')
        if self.manifest_version is not None and self.manifest_version < 1:
            raise ValidationError('manifest version must be positive')

class AuthorityInbox:

    def __init__(self):
        self.messages: dict[str, AuthorityMessage] = {}

    def ingest(self, m: AuthorityMessage) -> None:
        existing = self.messages.get(m.message_id)
        if existing is not None:
            if existing == m:
                return
            raise ValidationError('conflicting authority message')
        if m.supersedes and m.supersedes in self.messages:
            self._check_supersession(m, self.messages[m.supersedes])
        for other in self.messages.values():
            if other.supersedes == m.message_id:
                self._check_supersession(other, m)
        self.messages[m.message_id] = m

    @staticmethod
    def _check_supersession(newer: AuthorityMessage, older: AuthorityMessage) -> None:
        if older.container_id != newer.container_id or older.authority != newer.authority:
            raise ValidationError('supersession scope mismatch')
        if newer.issued_at <= older.issued_at:
            raise ValidationError('superseding message must be newer')

    def effective(self, container_id: str) -> tuple[AuthorityMessage, ...]:
        superseded = {m.supersedes for m in self.messages.values() if m.supersedes}
        values = [m for m in self.messages.values() if m.container_id == container_id and m.message_id not in superseded]
        return tuple(sorted(values, key=lambda x: (x.authority, x.issued_at, x.message_id)))

    def latest(self, container_id: str, authority: str, message_type: str) -> AuthorityMessage | None:
        values = [m for m in self.effective(container_id) if m.authority == authority and m.message_type == message_type]
        return max(values, key=lambda x: x.issued_at) if values else None
