from datetime import datetime, timedelta, timezone

from terminalweave.customs.authority import AuthorityInbox, AuthorityMessage


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
