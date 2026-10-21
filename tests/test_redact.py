"""Tests for safety.redact: keys and tokens taken out of command and task output, by shape and by value."""

import json
import time

import pytest

from alfred.safety import host
from alfred.safety.redact import REDACTED, redact
from alfred.tasks import command_jobs

KEY = "sk-ant-api03-" + "A1b2C3d4" * 4


@pytest.mark.parametrize("text", [
    f"key: {KEY}", "ghp_" + "x" * 36, "github_pat_" + "Ab1_" * 10, "AKIAABCDEFGHIJKLMNOP", "AIza" + "B" * 35,
    "xoxb-1234567890-abcdefghij", "sk_live_" + "a1" * 12, "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijklmnop",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEA\n-----END OPENSSH PRIVATE KEY-----",
])
def test_keys_are_redacted_by_their_shape(text):
    assert REDACTED in redact(text) and text not in redact(text)


@pytest.mark.parametrize("text, kept", [
    ("ELEVENLABS_API_KEY=abcdef0123456789", "ELEVENLABS_API_KEY="),
    ('export GITHUB_TOKEN="ghx0123456789"', "export GITHUB_TOKEN=\""),
    ('{"client_secret": "zzzzzzzzzz12", "name": "x"}', '"name": "x"'),
    ("Authorization: Bearer abc.def.ghijklmnop", "Authorization: Bearer "),
])
def test_a_value_given_to_a_key_like_name_is_redacted(text, kept):
    out = redact(text)
    assert kept in out and REDACTED in out


@pytest.mark.parametrize("text", [
    "author: Johnathan Smithers", "keys: ELEVENLABS_API_KEY (environment), PEXELS_API_KEY (.env)",
    "tokens used: 900000000", "commit 3f2a9c1e0b7d4a6f8e5c2b1a0d9e8f7c6b5a4d3e",
    "Uploaded 3 videos to https://youtube.com/watch?v=abc", "",
])
def test_ordinary_output_is_left_alone(text):
    assert redact(text) == text


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / ".env").write_text("VOICE_ID=pNInz6obpgDQGcFmaJgB\nPORT=3000\nMODE=production\nOUT=/tmp/out\n")
    (root / ".env.example").write_text("VOICE_ID=your-voice-id-here\n")
    return root


def test_the_values_of_the_projects_env_are_redacted_wherever_it_ran_in_it(project):
    out = "voice pNInz6obpgDQGcFmaJgB on port 3000 in production, to /tmp/out; your-voice-id-here"
    for folder in (project, project / "scripts"):
        assert redact(out, folder) == f"voice {REDACTED} on port 3000 in production, to /tmp/out; your-voice-id-here"
    assert redact(out) == out  # without the folder, its values aren't known
    (project / ".env").write_text("VOICE_ID=a-new-voice-0001\n")  # read again once it changes
    assert redact("a-new-voice-0001", project) == REDACTED


def test_alfreds_own_keys_are_redacted_by_value(monkeypatch):
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "q7w8e9r0t1y2u3")
    assert redact("got q7w8e9r0t1y2u3 back") == f"got {REDACTED} back"


def test_command_output_is_redacted_before_anyone_reads_it(project, tmp_path):
    log = tmp_path / "1.log"
    log.write_text(f"starting\nusing {KEY}\nvoice pNInz6obpgDQGcFmaJgB\ndone\n")
    tail = command_jobs.tail(log, 4000, project)
    assert KEY not in tail and "pNInz6obpgDQGcFmaJgB" not in tail and tail.count(REDACTED) == 2
    jobs = host.Jobs(tmp_path / "jobs")
    jobs.folder.mkdir()
    (jobs.folder / "1.log").write_text("VOICE pNInz6obpgDQGcFmaJgB\n")
    view = jobs._view({"id": 1, "title": "t", "command": "x", "folder": str(project), "started": time.time(),
                       "pid": 0}, time.time())
    assert view["output_tail"] == f"VOICE {REDACTED}\n"
    assert REDACTED in json.dumps(view)


def test_a_number_given_to_a_key_like_name_is_redacted_too():
    assert redact("PASSCODE=48213977") == f"PASSCODE={REDACTED}"
