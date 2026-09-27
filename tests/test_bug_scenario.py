from datetime import datetime, timedelta, timezone

from terminalweave.core.errors import ValidationError
from terminalweave.customs.authority import AuthorityInbox, AuthorityMessage
from terminalweave.customs.clearance import CustomsClearance
from terminalweave.customs.holds import CustomsLedger, HoldRecord
from terminalweave.customs.inspection import Inspection, InspectionBay, InspectionScheduler
from terminalweave.customs.manifest import ManifestItem, ManifestLedger, ManifestVersion
from terminalweave.customs.release_queue import ReleaseQueue, ReleaseRequest


UTC = timezone.utc
D = datetime(2026, 1, 1, tzinfo=UTC)


def _raises(error, fn):
    try:
        fn()
    except error:
        return True
    return False


def _item(container_id="CONT1"):
    return ManifestItem(container_id, "BL1", "CONSIGNEE", "22G1", 12000.0)


def _manifest(version, at, containers=("CONT1",)):
    ledger = ManifestLedger()
    for v in range(1, version + 1):
        items = tuple(_item(c) for c in containers)
        ledger.submit(ManifestVersion(v, at + timedelta(hours=v - 1), items, "CARRIER"))
    return ledger


def _cleared_inspections(container_id="CONT1", requested_at=D):
    scheduler = InspectionScheduler([InspectionBay("BAY1", 5)])
    scheduler.request(Inspection("I1", container_id, requested_at))
    scheduler.schedule("I1", requested_at + timedelta(hours=1))
    scheduler.start("I1")
    scheduler.finish("I1", cleared=True)
    return scheduler


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


def test_out_of_order_supersession_resolves_without_duplicate_state():
    inbox = AuthorityInbox()
    hold = AuthorityMessage("M1", "CONT1", "CUSTOMS", "hold", D)
    release = AuthorityMessage(
        "M2", "CONT1", "CUSTOMS", "release", D + timedelta(hours=1), supersedes="M1"
    )
    inbox.ingest(release)
    inbox.ingest(hold)
    inbox.ingest(hold)
    assert inbox.effective("CONT1") == (release,)


def test_conflicting_replay_is_rejected():
    inbox = AuthorityInbox()
    inbox.ingest(AuthorityMessage("M1", "CONT1", "CUSTOMS", "hold", D))
    conflicting = AuthorityMessage("M1", "CONT1", "CUSTOMS", "release", D)
    assert _raises(ValidationError, lambda: inbox.ingest(conflicting))


def test_release_after_inspection_lifts_old_manifest_version_hold():
    manifests = _manifest(2, D)
    holds = CustomsLedger()
    holds.place_hold(HoldRecord("H1", "CONT1", "CUSTOMS", "EXAM", D, manifest_version=1))
    inbox = AuthorityInbox()
    inbox.ingest(AuthorityMessage("M1", "CONT1", "CUSTOMS", "hold", D))
    inbox.ingest(
        AuthorityMessage("M2", "CONT1", "CUSTOMS", "release", D + timedelta(hours=3), supersedes="M1")
    )
    clearance = CustomsClearance(manifests, holds, _cleared_inspections(), inbox)
    first = clearance.decide("CONT1")
    assert first.allowed and first.reasons == ()
    assert clearance.decide("CONT1") == first


def test_missing_release_message_keeps_container_blocked():
    manifests = _manifest(2, D)
    holds = CustomsLedger()
    holds.place_hold(HoldRecord("H1", "CONT1", "CUSTOMS", "EXAM", D, manifest_version=1))
    clearance = CustomsClearance(manifests, holds, _cleared_inspections(), AuthorityInbox())
    decision = clearance.decide("CONT1")
    assert not decision.allowed and decision.reasons
    assert clearance.decide("CONT1") == decision


def test_unfinished_inspection_keeps_container_blocked():
    manifests = _manifest(1, D)
    holds = CustomsLedger()
    scheduler = InspectionScheduler([InspectionBay("BAY1", 5)])
    scheduler.request(Inspection("I1", "CONT1", D))
    inbox = AuthorityInbox()
    inbox.ingest(AuthorityMessage("M1", "CONT1", "CUSTOMS", "release", D + timedelta(hours=1)))
    clearance = CustomsClearance(manifests, holds, scheduler, inbox)
    assert not clearance.allowed("CONT1")


def test_release_predating_hold_does_not_lift_it():
    manifests = _manifest(1, D)
    holds = CustomsLedger()
    holds.place_hold(HoldRecord("H1", "CONT1", "CUSTOMS", "EXAM", D + timedelta(hours=2)))
    inbox = AuthorityInbox()
    inbox.ingest(AuthorityMessage("M1", "CONT1", "CUSTOMS", "release", D))
    clearance = CustomsClearance(manifests, holds, InspectionScheduler([]), inbox)
    assert not clearance.allowed("CONT1")


def test_versioned_release_must_cover_current_manifest_version():
    manifests = _manifest(2, D)
    holds = CustomsLedger()
    holds.place_hold(HoldRecord("H1", "CONT1", "CUSTOMS", "EXAM", D, manifest_version=1))
    stale = AuthorityInbox()
    stale.ingest(
        AuthorityMessage("M1", "CONT1", "CUSTOMS", "release", D + timedelta(hours=3), manifest_version=1)
    )
    clearance = CustomsClearance(manifests, holds, _cleared_inspections(), stale)
    assert not clearance.allowed("CONT1")
    current = AuthorityInbox()
    current.ingest(
        AuthorityMessage("M1", "CONT1", "CUSTOMS", "release", D + timedelta(hours=3), manifest_version=2)
    )
    clearance = CustomsClearance(manifests, holds, _cleared_inspections(), current)
    assert clearance.allowed("CONT1")


def test_container_absent_from_current_manifest_stays_blocked():
    manifests = _manifest(2, D, containers=())
    holds = CustomsLedger()
    inbox = AuthorityInbox()
    inbox.ingest(AuthorityMessage("M1", "CONT1", "CUSTOMS", "release", D + timedelta(hours=1)))
    clearance = CustomsClearance(manifests, holds, InspectionScheduler([]), inbox)
    assert not clearance.allowed("CONT1")


def test_newer_hold_message_reblocks_after_release():
    manifests = _manifest(1, D)
    holds = CustomsLedger()
    holds.place_hold(HoldRecord("H1", "CONT1", "CUSTOMS", "EXAM", D))
    inbox = AuthorityInbox()
    inbox.ingest(AuthorityMessage("M1", "CONT1", "CUSTOMS", "release", D + timedelta(hours=1)))
    inbox.ingest(AuthorityMessage("M2", "CONT1", "CUSTOMS", "hold", D + timedelta(hours=2)))
    clearance = CustomsClearance(manifests, holds, InspectionScheduler([]), inbox)
    assert not clearance.allowed("CONT1")


def test_inspection_and_release_queue_replays_are_idempotent():
    scheduler = InspectionScheduler([InspectionBay("BAY1", 5)])
    item = Inspection("I1", "CONT1", D)
    scheduler.request(item)
    scheduler.request(Inspection("I1", "CONT1", D))
    assert len(scheduler.items) == 1
    queue = ReleaseQueue()
    request = ReleaseRequest("R1", "CONT1", D, 1, True, "low")
    queue.submit(request)
    queue.submit(request)
    assert queue.ready() == [request]
