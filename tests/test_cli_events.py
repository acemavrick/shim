"""The terminal printer must tolerate every event shape the compiler emits."""
from shim.cli import print_event


def test_superseded_candidate_event_has_no_name(capsys):
    print_event({"type": "action_failed", "candidate_id": "tool-3",
                 "error": "Candidate superseded by the final evidence."})
    assert "tool-3" in capsys.readouterr().out


def test_skip_event_shows_its_detail(capsys):
    print_event({"type": "skip", "what": "agent", "reason": "fetch-error",
                 "detail": "TimeoutError: Page.goto: Timeout 30000ms exceeded"})
    assert "Page.goto" in capsys.readouterr().out
