from __future__ import annotations
from dataclasses import dataclass
from terminalweave.customs.authority import AuthorityInbox, AuthorityMessage
from terminalweave.customs.holds import CustomsLedger, HoldRecord
from terminalweave.customs.inspection import Inspection, InspectionScheduler, InspectionState
from terminalweave.customs.manifest import ManifestLedger

@dataclass(frozen=True)
class ClearanceDecision:
    container_id: str
    allowed: bool
    manifest_version: int | None
    blockers: tuple[str, ...]

def _version_covers(release_version: int | None, held_version: int | None) -> bool:
    if release_version is None or held_version is None:
        return True
    return release_version >= held_version

class ClearanceService:
    """Release determination for container pickup.

    Combines manifest versions, customs holds, inspections and official
    authority messages. Evidence is matched by manifest version and event
    time rather than arrival order, so out-of-order replays converge to the
    same outcome and repeated inputs leave the result unchanged. When the
    required evidence is missing the container stays blocked.
    """

    def __init__(self, manifests: ManifestLedger, holds: CustomsLedger, inspections: InspectionScheduler, inbox: AuthorityInbox):
        self._manifests = manifests
        self._holds = holds
        self._inspections = inspections
        self._inbox = inbox

    def allowed(self, container_id: str) -> bool:
        return self.decision(container_id).allowed

    def decision(self, container_id: str) -> ClearanceDecision:
        latest = self._manifests.latest()
        version = latest.version if latest else None
        blockers: list[str] = []
        for hold in self._holds.active_holds(container_id):
            if not self._hold_lifted(hold):
                blockers.append(f'hold:{hold.hold_id}')
        inspection = self._latest_inspection(container_id)
        if inspection is not None and inspection.state != InspectionState.CLEARED:
            blockers.append(f'inspection:{inspection.inspection_id}')
        blockers.extend(self._message_blockers(container_id))
        return ClearanceDecision(container_id, not blockers, version, tuple(blockers))

    def _latest_inspection(self, container_id: str) -> Inspection | None:
        related = [i for i in self._inspections.items.values() if i.container_id == container_id]
        if not related:
            return None
        return max(related, key=lambda i: (i.requested_at, i.inspection_id))

    def _hold_lifted(self, hold: HoldRecord) -> bool:
        release = self._inbox.latest(hold.container_id, hold.authority, 'release')
        if release is None or release.issued_at < hold.created_at:
            return False
        if not _version_covers(release.manifest_version, hold.manifest_version):
            return False
        inspection = self._latest_inspection(hold.container_id)
        return inspection is None or inspection.state == InspectionState.CLEARED

    def _message_blockers(self, container_id: str) -> list[str]:
        effective = self._inbox.effective(container_id)
        releases = [m for m in effective if m.message_type == 'release']
        blockers = []
        for msg in effective:
            if msg.message_type != 'hold':
                continue
            lifted = any((r.authority == msg.authority and r.issued_at >= msg.issued_at and _version_covers(r.manifest_version, msg.manifest_version) for r in releases))
            if not lifted:
                blockers.append(f'message:{msg.message_id}')
        return blockers
