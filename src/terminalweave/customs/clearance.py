from __future__ import annotations
from dataclasses import dataclass
from terminalweave.customs.authority import AuthorityInbox, AuthorityMessage
from terminalweave.customs.holds import CustomsLedger, HoldRecord
from terminalweave.customs.inspection import InspectionScheduler, InspectionState
from terminalweave.customs.manifest import ManifestLedger

RELEASE_MESSAGE_TYPE = 'release'

@dataclass(frozen=True)
class ClearanceDecision:
    container_id: str
    allowed: bool
    reasons: tuple[str, ...]

def _covers(message_version: int | None, required_version: int | None) -> bool:
    if message_version is None or required_version is None:
        return True
    return message_version >= required_version

class CustomsClearance:
    """Decides whether a container may leave customs control.

    The decision combines the current manifest version, the hold ledger,
    the inspection scheduler and the authority inbox. It is deterministic
    for identical inputs and fails closed: whenever release evidence is
    missing, stale or superseded, the container stays blocked.
    """

    def __init__(self, manifests: ManifestLedger, holds: CustomsLedger, inspections: InspectionScheduler, inbox: AuthorityInbox):
        self._manifests = manifests
        self._holds = holds
        self._inspections = inspections
        self._inbox = inbox

    def decide(self, container_id: str) -> ClearanceDecision:
        reasons: list[str] = []
        current_version = self._manifest_version_reasons(container_id, reasons)
        self._inspection_reasons(container_id, reasons)
        stances: dict[str, AuthorityMessage | None] = {}
        for hold in self._holds.active_holds(container_id):
            if hold.authority not in stances:
                stances[hold.authority] = self._stance(container_id, hold.authority)
            self._hold_reasons(hold, stances[hold.authority], current_version, reasons)
        return ClearanceDecision(container_id, not reasons, tuple(reasons))

    def allowed(self, container_id: str) -> bool:
        return self.decide(container_id).allowed

    def _manifest_version_reasons(self, container_id: str, reasons: list[str]) -> int | None:
        latest = self._manifests.latest()
        if latest is None:
            reasons.append('no manifest submitted')
            return None
        if not any((x.container_id == container_id for x in latest.items)):
            reasons.append('container absent from current manifest version')
        return latest.version

    def _inspection_reasons(self, container_id: str, reasons: list[str]) -> None:
        latest = None
        for item in self._inspections.items.values():
            if item.container_id != container_id:
                continue
            if latest is None or (item.requested_at, item.inspection_id) > (latest.requested_at, latest.inspection_id):
                latest = item
        if latest is not None and latest.state != InspectionState.CLEARED:
            reasons.append(f'inspection {latest.inspection_id} not cleared ({latest.state.value})')

    def _hold_reasons(self, hold: HoldRecord, stance: AuthorityMessage | None, current_version: int | None, reasons: list[str]) -> None:
        if stance is None:
            reasons.append(f'hold {hold.hold_id} has no release message from {hold.authority}')
            return
        if stance.message_type != RELEASE_MESSAGE_TYPE:
            reasons.append(f'hold {hold.hold_id} not released by {hold.authority}')
            return
        if stance.issued_at < hold.created_at:
            reasons.append(f'release {stance.message_id} predates hold {hold.hold_id}')
            return
        if not _covers(stance.manifest_version, hold.manifest_version):
            reasons.append(f'release {stance.message_id} does not cover hold {hold.hold_id} manifest version')
            return
        if not _covers(stance.manifest_version, current_version):
            reasons.append(f'release {stance.message_id} predates current manifest version')

    def _stance(self, container_id: str, authority: str) -> AuthorityMessage | None:
        values = [m for m in self._inbox.effective(container_id) if m.authority == authority]
        if not values:
            return None
        return max(values, key=lambda x: (x.issued_at, x.message_id))
