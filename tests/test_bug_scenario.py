from datetime import datetime, timedelta, timezone

import pytest

from terminalweave.core.errors import ValidationError
from terminalweave.customs.authority import AuthorityInbox, AuthorityMessage
from terminalweave.customs.clearance import ClearanceService
from terminalweave.customs.holds import CustomsLedger, HoldRecord
from terminalweave.customs.inspection import Inspection, InspectionBay, InspectionScheduler
from terminalweave.customs.manifest import ManifestItem, ManifestLedger, ManifestVersion


UTC = timezone.utc
D = datetime(2026, 1, 1, tzinfo=UTC)


def test_replayed_release_is_idempotent_and_supersedes_old_hold():
    inbox = AuthorityInbox()
    hold = AuthorityMessage("M1", "CONT1", "CUSTOMS", "hold", D)
    release = AuthorityMessage(
        "M2", "CONT1", "CUSTOMS", "release", D + timedelta(hours=1), supersedes="M1"
    )
    inbox.ingest(hold)
    inbox.ingest(release)
    inbox.ingest(release)
    assert inbox.effective("CONT1") == (release,)


def test_out_of_order_superseding_message_converges():
    inbox = AuthorityInbox()
    hold = AuthorityMessage("M1", "CONT1", "CUSTOMS", "hold", D)
    release = AuthorityMessage(
        "M2", "CONT1", "CUSTOMS", "release", D + timedelta(hours=1), supersedes="M1"
    )
    inbox.ingest(release)
    inbox.ingest(hold)
    inbox.ingest(hold)
    assert inbox.effective("CONT1") == (release,)


def test_conflicting_message_id_is_rejected():
    inbox = AuthorityInbox()
    inbox.ingest(AuthorityMessage("M1", "CONT1", "CUSTOMS", "hold", D))
    with pytest.raises(ValidationError):
        inbox.ingest(AuthorityMessage("M1", "CONT1", "CUSTOMS", "release", D))


def _manifest(version: int, at: datetime) -> ManifestVersion:
    item = ManifestItem("CONT1", "BL1", "consignee", "22G1", 10000.0)
    return ManifestVersion(version, at, (item,), "carrier")


def _service():
    manifests = ManifestLedger()
    manifests.submit(_manifest(1, D))
    holds = CustomsLedger()
    inbox = AuthorityInbox()
    inspections = InspectionScheduler([InspectionBay("BAY1", 5)])
    return manifests, holds, inbox, inspections


def _clear_inspection(inspections: InspectionScheduler) -> None:
    inspections.request(Inspection("I1", "CONT1", D + timedelta(hours=2)))
    inspections.schedule("I1", D + timedelta(hours=3))
    inspections.start("I1")
    inspections.finish("I1", cleared=True)


def test_old_version_hold_lifts_after_cleared_inspection_and_release():
    manifests, holds, inbox, inspections = _service()
    holds.place_hold(
        HoldRecord("H1", "CONT1", "CUSTOMS", "EXAM", D + timedelta(hours=1), manifest_version=1)
    )
    inbox.ingest(AuthorityMessage("M1", "CONT1", "CUSTOMS", "hold", D + timedelta(hours=1), manifest_version=1))
    _clear_inspection(inspections)
    release = AuthorityMessage(
        "M2", "CONT1", "CUSTOMS", "release", D + timedelta(hours=4), supersedes="M1", manifest_version=2
    )
    inbox.ingest(release)
    manifests.submit(_manifest(2, D + timedelta(hours=5)))
    service = ClearanceService(manifests, holds, inspections, inbox)
    decision = service.decision("CONT1")
    assert decision.allowed
    assert decision.blockers == ()
    assert decision.manifest_version == 2
    # replaying the same inputs leaves the outcome unchanged
    inbox.ingest(release)
    assert service.decision("CONT1") == decision
    assert service.allowed("CONT1")


def test_missing_release_message_keeps_container_blocked():
    manifests, holds, inbox, inspections = _service()
    holds.place_hold(
        HoldRecord("H1", "CONT1", "CUSTOMS", "EXAM", D + timedelta(hours=1), manifest_version=1)
    )
    inbox.ingest(AuthorityMessage("M1", "CONT1", "CUSTOMS", "hold", D + timedelta(hours=1)))
    _clear_inspection(inspections)
    manifests.submit(_manifest(2, D + timedelta(hours=5)))
    service = ClearanceService(manifests, holds, inspections, inbox)
    decision = service.decision("CONT1")
    assert not decision.allowed
    assert "hold:H1" in decision.blockers
    assert "message:M1" in decision.blockers


def test_pending_inspection_keeps_container_blocked():
    manifests, holds, inbox, inspections = _service()
    holds.place_hold(
        HoldRecord("H1", "CONT1", "CUSTOMS", "EXAM", D + timedelta(hours=1), manifest_version=1)
    )
    inbox.ingest(AuthorityMessage("M1", "CONT1", "CUSTOMS", "hold", D + timedelta(hours=1)))
    inbox.ingest(
        AuthorityMessage("M2", "CONT1", "CUSTOMS", "release", D + timedelta(hours=4), supersedes="M1")
    )
    inspections.request(Inspection("I1", "CONT1", D + timedelta(hours=2)))
    inspections.schedule("I1", D + timedelta(hours=3))
    service = ClearanceService(manifests, holds, inspections, inbox)
    decision = service.decision("CONT1")
    assert not decision.allowed
    assert "inspection:I1" in decision.blockers
    assert "hold:H1" in decision.blockers


def test_release_for_older_manifest_version_does_not_lift_hold():
    manifests, holds, inbox, inspections = _service()
    manifests.submit(_manifest(2, D + timedelta(hours=1)))
    holds.place_hold(
        HoldRecord("H1", "CONT1", "CUSTOMS", "EXAM", D + timedelta(hours=2), manifest_version=2)
    )
    _clear_inspection(inspections)
    inbox.ingest(
        AuthorityMessage("M2", "CONT1", "CUSTOMS", "release", D + timedelta(hours=4), manifest_version=1)
    )
    service = ClearanceService(manifests, holds, inspections, inbox)
    decision = service.decision("CONT1")
    assert not decision.allowed
    assert "hold:H1" in decision.blockers


def test_release_predating_hold_does_not_lift_it():
    manifests, holds, inbox, inspections = _service()
    inbox.ingest(AuthorityMessage("M2", "CONT1", "CUSTOMS", "release", D + timedelta(hours=1)))
    holds.place_hold(
        HoldRecord("H1", "CONT1", "CUSTOMS", "EXAM", D + timedelta(hours=2), manifest_version=1)
    )
    service = ClearanceService(manifests, holds, inspections, inbox)
    assert not service.allowed("CONT1")


def test_out_of_order_replay_yields_same_decision():
    manifests, holds, inbox, inspections = _service()
    holds.place_hold(
        HoldRecord("H1", "CONT1", "CUSTOMS", "EXAM", D + timedelta(hours=1), manifest_version=1)
    )
    _clear_inspection(inspections)
    release = AuthorityMessage(
        "M2", "CONT1", "CUSTOMS", "release", D + timedelta(hours=4), supersedes="M1", manifest_version=2
    )
    hold_msg = AuthorityMessage("M1", "CONT1", "CUSTOMS", "hold", D + timedelta(hours=1), manifest_version=1)
    # release arrives before the hold it supersedes, then both are replayed
    inbox.ingest(release)
    inbox.ingest(hold_msg)
    inbox.ingest(release)
    inbox.ingest(hold_msg)
    manifests.submit(_manifest(2, D + timedelta(hours=5)))
    service = ClearanceService(manifests, holds, inspections, inbox)
    assert service.allowed("CONT1")
    assert inbox.effective("CONT1") == (release,)
