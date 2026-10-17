"""Tests for voice.speech_gate: audio and Whisper-confidence checks, phantom phrases, shadow mode, clip saving.

Silero is faked (it marks chosen stretches as speech) except in one test of the real model.
"""

import asyncio
import dataclasses
import importlib
import json
import logging
import re
import wave

import numpy as np
import pytest
from livekit import rtc

from jarvis import config
from jarvis.state import preferences
from jarvis.voice import speech, speech_gate
from jarvis.voice.speech_gate import RATE, WINDOW, Measure, SpeechGate, Thresholds

LOOSE = Thresholds(min_seconds=0.3, min_speech_seconds=0.1, min_dbfs=-55, no_speech=0.8, weak_speech_seconds=0.25,
                   faint_speech_seconds=0.25, faint_dbfs=-28, faint_max_seconds=1.4, phantom_dbfs=-22,
                   phantom_speech_seconds=0.6, phantoms=frozenset(speech_gate.normalise(p) for p in speech_gate.PHANTOMS),
                   min_logprob=-1.0, unsure_speech_seconds=0.5, max_compression=2.4)


def clip(seconds: float, speech: tuple[float, float] | None = None, level: float = 0.1, floor: float = 0.001) -> np.ndarray:
    """Quiet noise, with a loud stretch (start, end) in seconds standing in for speech."""
    rng = np.random.default_rng(0)
    audio = (rng.standard_normal(int(seconds * RATE)) * floor).astype(np.float32)
    if speech:
        a, b = int(speech[0] * RATE), int(speech[1] * RATE)
        audio[a:b] += np.sin(np.arange(b - a) * 2 * np.pi * 220 / RATE).astype(np.float32) * level
    return audio


def fake_vad(speech: tuple[float, float] | None):
    """A VAD that calls exactly the stretch `speech` speech."""
    def probs(audio: np.ndarray) -> np.ndarray:
        starts = np.arange(len(audio) // WINDOW) * WINDOW / RATE
        if not speech:
            return np.zeros(len(starts), np.float32)
        return ((starts >= speech[0]) & (starts < speech[1])).astype(np.float32)
    return probs


def gate(mode: str, speech, tmp_path=None, keep: int = 300, save_all: bool = True) -> SpeechGate:
    return SpeechGate(mode, LOOSE, fake_vad(speech), record=tmp_path / "gate.jsonl" if tmp_path else None,
                      audio_dir=tmp_path / "audio" if tmp_path else None, keep_audio=keep, save_all=save_all,
                      short=(2.0, 0.5))


def records(tmp_path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "gate.jsonl").read_text().splitlines()]


def read_wav(path) -> tuple[wave._wave_params, np.ndarray]:
    with wave.open(str(path)) as f:
        return f.getparams(), np.frombuffer(f.readframes(f.getnframes()), "<i2").astype(np.float32) / 32767


def test_a_clip_is_measured():
    m = speech_gate.measure(clip(1.5, (0.5, 0.9)), fake_vad((0.5, 0.9)))
    assert m.seconds == 1.5
    assert 0.38 <= m.speech_seconds <= 0.42 and 0.25 <= m.speech_ratio <= 0.28
    assert -25 < m.loudest_dbfs < -15 and m.rms_dbfs < m.loudest_dbfs


def test_real_short_replies_always_get_through(caplog):
    for mode in ("shadow", "enforce"):
        g = gate(mode, (0.5, 0.85))
        for text in ("Okay.", "No.", "Yes.", "Go ahead.", "Sure, go ahead.", "Yeah."):
            with caplog.at_level(logging.INFO, "jarvis.voice.speech_gate"):
                assert g.passes(g.measure(clip(1.4, (0.5, 0.85))), text, no_speech=0.1, avg_logprob=-0.8)
    assert "speech gate" not in caplog.text


def test_a_real_thank_you_gets_through_when_said_clearly(caplog):
    """A phantom phrase passes only when loud, with enough speech, and Whisper is sure."""
    g = gate("enforce", (0.4, 1.1))
    with caplog.at_level(logging.INFO, "jarvis.voice.speech_gate"):
        assert g.passes(g.measure(clip(1.8, (0.4, 1.1), level=0.3)), "Thank you.", no_speech=0.0, avg_logprob=-0.6)
    assert "speech gate" not in caplog.text
    g = gate("enforce", (0.5, 0.85))  # the same words with a short reply's 0.35 s of speech
    assert not g.passes(g.measure(clip(1.4, (0.5, 0.85), level=0.3)), "Thank you.", no_speech=0.0, avg_logprob=-0.6)


def test_a_phantom_thank_you_on_noise_is_logged_in_shadow_mode_and_kept(tmp_path, caplog):
    g = gate("shadow", None, tmp_path)
    with caplog.at_level(logging.INFO, "jarvis.voice.speech_gate"):
        assert g.passes(g.measure(clip(1.2)), "Thank you.", no_speech=0.93)
    line = caplog.text
    assert "would drop (shadow)" in line and "no speech" in line and "too quiet" in line
    assert "1.20 s" in line and "dBFS" in line and "speech 0.00 s (0%)" in line and "no_speech 0.93" in line
    assert "'Thank you.'" in line
    record = json.loads((tmp_path / "gate.jsonl").read_text())
    assert record["verdict"] == "would drop" and record["text"] == "Thank you." and record["no_speech"] == 0.93
    assert {"seconds", "rms_dbfs", "loudest_dbfs", "speech_seconds", "speech_ratio"} <= record.keys()


def test_enforce_mode_drops_it(tmp_path):
    g = gate("enforce", None, tmp_path)
    assert not g.passes(g.measure(clip(1.2)), "Thank you.", no_speech=0.93)
    assert json.loads((tmp_path / "gate.jsonl").read_text())["verdict"] == "dropped"


def test_each_check_on_its_own():
    loud_blip = clip(1.2, (0.5, 0.56))
    assert speech_gate.reasons(speech_gate.measure(loud_blip, fake_vad((0.5, 0.56))), LOOSE) == ["no speech"]
    quiet_speech = clip(1.2, (0.4, 0.9), level=0.001)
    assert speech_gate.reasons(speech_gate.measure(quiet_speech, fake_vad((0.4, 0.9))), LOOSE) == ["too quiet"]
    tiny = clip(0.2, (0.0, 0.2))
    assert speech_gate.reasons(speech_gate.measure(tiny, fake_vad((0.0, 0.2))), LOOSE) == ["too short"]


def test_whisper_no_speech_only_counts_alongside_weak_vad_evidence():
    weak = Measure(1.2, -40, -20, speech_seconds=0.15, speech_ratio=0.12, no_speech=0.9)
    assert speech_gate.reasons(weak, LOOSE) == ["whisper hears no speech"]
    strong = Measure(1.2, -40, -20, speech_seconds=0.6, speech_ratio=0.5, no_speech=0.99)
    assert speech_gate.reasons(strong, LOOSE) == []  # Whisper alone never drops a clip
    assert speech_gate.reasons(Measure(1.2, -40, -20, 0.15, 0.12, no_speech=None), LOOSE) == []


def heard(seconds: float, loudest_dbfs: float, speech_seconds: float, avg_logprob: float | None = None) -> Measure:
    """A clip as logs/speech-gate.jsonl recorded it (Whisper's no_speech was 0.0 on every one)."""
    return Measure(seconds, loudest_dbfs - 9, loudest_dbfs, speech_seconds, round(speech_seconds / seconds, 3), 0.0,
                   avg_logprob)


def test_faint_phantoms_get_caught():
    """Real phantoms from the log, just over the 0.1 s of speech that "no speech" needs."""
    assert speech_gate.reasons(heard(1.172, -39.3, 0.16), LOOSE) == ["faint"]  # "You"
    assert speech_gate.reasons(heard(1.172, -30.9, 0.192), LOOSE) == ["faint"]  # "Thank you."


@pytest.mark.parametrize("text, seconds, loudest, speech_s", [
    ("Okay.", 1.492, -31.5, 0.16),  # as faint as the phantoms, but longer: the session's VAD heard more of it
    ("Cut.", 1.428, -38.4, 0.16),
    ("Yes.", 1.844, -35.4, 0.512),  # the quietest real replies have plenty of speech
    ("Yeah.", 1.204, -34.6, 0.416),
    ("Okay.", 1.3, -25.8, 0.256),
    ("Well.", 1.268, -26.7, 0.192),  # little speech and short, but loud enough
    ("Thank you.", 1.268, -20.3, 0.16),
    ("Bye.", 1.268, -18.9, 0.192),
])
def test_real_short_replies_from_the_log_are_not_faint(text, seconds, loudest, speech_s):
    assert speech_gate.reasons(heard(seconds, loudest, speech_s), LOOSE) == [], text


def test_faint_needs_all_three():
    assert speech_gate.reasons(heard(1.2, -35, 0.2), LOOSE) == ["faint"]
    assert speech_gate.reasons(heard(1.2, -35, 0.25), LOOSE) == []  # enough speech
    assert speech_gate.reasons(heard(1.2, -28, 0.2), LOOSE) == []  # loud enough
    assert speech_gate.reasons(heard(1.4, -35, 0.2), LOOSE) == []  # long enough


@pytest.mark.parametrize("text, seconds, loudest, speech_s", [
    # real phantoms that passed the audio checks and reached the brain as user turns
    ("Thank you.", 1.204, -27.8, 0.352),
    ("Thank you.", 1.588, -31.1, 0.512),
    ("Thank you.", 1.556, -32.4, 0.736),  # plenty of VAD "speech", but quiet: the tail of JARVIS's own voice
    ("Thank you.", 1.908, -17.9, 0.352),  # loud enough, but little speech
    ("Thank you.", 1.172, -15.9, 0.224),
    ("like", 1.364, -25.1, 0.352),
    ("Mm-hmm.", 1.236, -30.6, 0.416),
    ("Bye-bye.", 1.396, -25.0, 0.384),
    ("You", 1.876, -19.0, 0.3),
    ("Thanks for watching!", 2.0, -30.0, 0.5),
])
def test_phantom_phrases_on_weak_audio_are_caught(text, seconds, loudest, speech_s):
    assert speech_gate.reasons(heard(seconds, loudest, speech_s, -0.7), LOOSE, text) == ["phantom phrase"], text


@pytest.mark.parametrize("text", ["Yes.", "No.", "Okay.", "Sure.", "Go ahead.", "Sure, go ahead.", "Yeah, sure.",
                                  "Thank you, that's all.", "Stop.", "What?"])
def test_real_replies_on_the_same_weak_audio_are_not_phantoms(text):
    """Only listed phrases face the higher bar, and only as the whole transcript."""
    assert speech_gate.reasons(heard(1.524, -27.0, 0.352, -0.8), LOOSE, text) == [], text


def test_a_phantom_phrase_needs_loud_long_and_sure():
    clear = heard(1.78, -14.3, 0.704, -0.92)
    assert speech_gate.reasons(clear, LOOSE, "Thank you.") == []
    assert speech_gate.reasons(heard(1.78, -23.0, 0.704, -0.92), LOOSE, "Thank you.") == ["phantom phrase"]  # quiet
    assert speech_gate.reasons(heard(1.78, -14.3, 0.5, -0.92), LOOSE, "Thank you.") == ["phantom phrase"]  # short
    assert speech_gate.reasons(heard(1.78, -14.3, 0.704, -1.3), LOOSE, "Thank you.") == ["phantom phrase"]  # unsure
    assert speech_gate.reasons(heard(1.78, -14.3, 0.704, None), LOOSE, "Thank you.") == []  # no logprob: audio only
    assert speech_gate.reasons(clear, LOOSE) == []  # without the words, only the audio is judged


def test_phantom_phrases_are_matched_whole_and_without_punctuation():
    assert speech_gate.normalise("Mm-hmm.") == "mm hmm" and speech_gate.normalise("  Thank you!! ") == "thank you"
    assert speech_gate.normalise("I'm here.") == "i'm here" and speech_gate.normalise(", ... ♪") == ""
    weak = heard(1.3, -30.0, 0.3, -0.7)
    for text in ("THANK YOU", "thank you.", "Thank you...", "Bye. Bye.", "So."):
        assert "phantom phrase" in speech_gate.reasons(weak, LOOSE, text), text
    for text in ("Thank you, Jarvis.", "So what's next?", "Like this."):
        assert speech_gate.reasons(weak, LOOSE, text) == [], text


def test_the_phantom_list_can_be_replaced():
    mine = dataclasses.replace(LOOSE, phantoms=frozenset({"cheers"}))
    weak = heard(1.3, -30.0, 0.3, -0.7)
    assert speech_gate.reasons(weak, mine, "Cheers!") == ["phantom phrase"]
    assert speech_gate.reasons(weak, mine, "Thank you.") == []


@pytest.mark.parametrize("text, speech_s, logprob, why", [
    ("asses", 0.48, -3.2, ["whisper unsure"]),  # garbled: every retry failed, so the words are a guess
    ("Nngo!", 0.256, -1.9, ["whisper unsure"]),
    ("Show them.", 0.64, -1.5, []),  # just as unsure, but with real speech in it
    ("Hey.", 0.32, -0.99, []),  # sure enough
    ("what", 0.32, None, []),  # no logprob, no judgement
])
def test_whisper_unsure_needs_weak_speech_too(text, speech_s, logprob, why):
    assert speech_gate.reasons(heard(1.5, -12.0, speech_s, logprob), LOOSE, text) == why, text


def test_a_transcript_without_words_is_dropped_however_loud():
    loud = heard(1.8, -10.0, 0.8, -0.5)
    for text in (",", "...", "♪", " - "):
        assert speech_gate.reasons(loud, LOOSE, text) == ["no words"], text
    assert speech_gate.reasons(loud, LOOSE, "42") == []


def test_a_looping_transcript_is_dropped():
    loud = heard(12.0, -10.0, 8.0, -0.4)
    assert speech_gate.reasons(loud, LOOSE, "Thank you. " * 12) == ["repetitive"]
    assert speech_gate.reasons(loud, LOOSE, "Can you check my calendar for tomorrow and move the standup to ten, "
                                            "then email Priya that I'll be late?") == []
    assert speech_gate.compression_ratio("Thank you.") < 1 < 2.4 < speech_gate.compression_ratio("Thank you. " * 12)


def test_the_new_measurements_are_logged(tmp_path, caplog):
    g = gate("enforce", (0.5, 0.85), tmp_path)
    with caplog.at_level(logging.INFO, "jarvis.voice.speech_gate"):
        assert not g.passes(g.measure(clip(1.4, (0.5, 0.85), level=0.02)), "Thank you.", 0.0, avg_logprob=-0.6123)
    record = records(tmp_path)[0]
    assert record["reasons"] == ["phantom phrase"] and record["avg_logprob"] == -0.612
    assert record["compression_ratio"] == speech_gate.compression_ratio("Thank you.")
    assert "dropped a 1.40 s clip (phantom phrase)" in caplog.text and "logprob -0.61" in caplog.text
    assert "By reason: phantom phrase 1." in speech_gate.summary(tmp_path / "gate.jsonl")


def test_enforce_mode_drops_a_faint_clip(tmp_path):
    audio = clip(1.172, (0.5, 0.66), level=0.02)  # 0.16 s of speech, its loudest 100 ms about -37 dBFS
    g = gate("enforce", (0.5, 0.66), tmp_path)
    m = g.measure(audio)
    assert m.speech_seconds == 0.16 and -40 < m.loudest_dbfs < -30
    assert not g.passes(m, "Hello.", no_speech=0.0)
    assert records(tmp_path)[0]["reasons"] == ["faint"]
    assert "under 0.25 s of speech, quieter than -28 dBFS and under 1.4 s long" in g.describe()


def test_off_checks_nothing(tmp_path):
    g = gate("off", None, tmp_path)
    assert g.measure(clip(1.2)) is None and g.passes(None, "Thank you.", 0.99)
    assert not (tmp_path / "gate.jsonl").exists()


def test_a_failing_vad_lets_the_clip_through(caplog):
    def broken(audio):
        raise RuntimeError("no model")
    g = SpeechGate("enforce", LOOSE, broken, record=None, audio_dir=None)
    assert g.measure(clip(1.0)) is None and g.passes(None, "Hello.", 0.9)
    assert "couldn't measure" in caplog.text


def test_the_summary_lists_what_it_would_drop(tmp_path):
    g = gate("shadow", None, tmp_path)
    g.passes(g.measure(clip(1.2)), "Thank you.", 0.9)
    g2 = SpeechGate("shadow", LOOSE, fake_vad((0.4, 1.0)), record=tmp_path / "gate.jsonl", audio_dir=None)
    g2.passes(g2.measure(clip(1.4, (0.4, 1.0))), "What's on my calendar?", 0.02)
    text = speech_gate.summary(tmp_path / "gate.jsonl")
    assert text.startswith("2 clips, 1 the gate would drop") and "'Thank you.'" in text and "calendar" not in text
    assert "doesn't exist" in speech_gate.summary(tmp_path / "missing.jsonl")


def test_the_mode_is_described_for_the_log():
    assert "shadow mode" in gate("shadow", None).describe() and "drops nothing" in gate("shadow", None).describe()
    assert gate("off", None).describe() == "speech gate: off"


def buffer(audio: np.ndarray, rate: int = 48000) -> list[rtc.AudioFrame]:
    up = np.interp(np.linspace(0, len(audio) - 1, len(audio) * rate // RATE), np.arange(len(audio)), audio)
    pcm = (up * 32767).astype(np.int16)
    return [rtc.AudioFrame(pcm.tobytes(), rate, 1, len(pcm))]


def transcribes(text: str, no_speech: float | None, avg_logprob: float | None = -0.5):
    """Stands in for Whisper in the shared process."""
    async def fake(audio: np.ndarray) -> dict:
        return {"text": text, "no_speech": no_speech, "avg_logprob": avg_logprob}
    return fake


@pytest.mark.parametrize("mode, heard", [("shadow", "Thank you."), ("enforce", ""), ("off", "Thank you.")])
def test_whisper_hands_on_nothing_only_when_enforcing(monkeypatch, mode, heard):
    stt = speech.WhisperSTT(gate=gate(mode, None))
    monkeypatch.setattr(stt, "_transcribe", transcribes("Thank you.", 0.95))
    assert asyncio.run(stt._hear(buffer(clip(1.2)))) == heard


def test_whisper_keeps_real_speech_when_enforcing(monkeypatch):
    stt = speech.WhisperSTT(gate=gate("enforce", (0.4, 0.8)))
    monkeypatch.setattr(stt, "_transcribe", transcribes("Okay.", 0.95))
    assert asyncio.run(stt._hear(buffer(clip(1.2, (0.4, 0.8))))) == "Okay."


def test_whisper_hands_the_gate_its_confidence(monkeypatch):
    """Garbled clip: real sound, every Whisper retry failed; its guess never reaches the brain."""
    stt = speech.WhisperSTT(gate=gate("enforce", (0.4, 0.65)))
    monkeypatch.setattr(stt, "_transcribe", transcribes("Nngo!", 0.0, avg_logprob=-1.9))
    assert asyncio.run(stt._hear(buffer(clip(1.0, (0.4, 0.65), level=0.4)))) == ""
    monkeypatch.setattr(stt, "_transcribe", transcribes("No.", 0.0, avg_logprob=-0.7))
    assert asyncio.run(stt._hear(buffer(clip(1.0, (0.4, 0.65), level=0.4)))) == "No."


def test_the_real_silero_hears_no_speech_in_silence_or_hum():
    probs = speech_gate.SileroProbs()
    for audio in (np.zeros(RATE, np.float32), clip(1.0, (0.0, 1.0), level=0.05)):
        p = probs(audio)
        assert len(p) == RATE // WINDOW and p.max() < 0.5


# ── Saving each clip as audio ───────────────────────────────────


def test_each_clip_is_saved_as_the_wav_whisper_heard_and_its_line_says_where(tmp_path, caplog):
    g = gate("shadow", None, tmp_path)
    audio = clip(1.172)
    with caplog.at_level(logging.INFO, "jarvis.voice.speech_gate"):
        assert g.passes(g.measure(audio), "Thank you.", no_speech=0.0, audio=audio)
    record = records(tmp_path)[0]
    path = tmp_path / "audio" / record["audio"].rsplit("/", 1)[-1]
    assert record["audio"] == str(path) and path.is_file()
    assert re.fullmatch(r"\d{8}-\d{6}-\d{3}\.wav", path.name)
    params, samples = read_wav(path)
    assert (params.nchannels, params.sampwidth, params.framerate, params.nframes) == (1, 2, RATE, len(audio))
    assert np.abs(samples - audio).max() < 1e-4
    assert str(path) in caplog.text  # the agent log's would-drop line points at it too


def test_kept_clips_are_saved_too_in_either_mode(tmp_path):
    """Some phantoms get through, so kept clips are worth hearing too."""
    for mode in ("shadow", "enforce"):
        g = gate(mode, (0.4, 0.8), tmp_path)
        audio = clip(1.2, (0.4, 0.8))
        assert g.passes(g.measure(audio), "Okay.", no_speech=0.0, audio=audio)
    assert [r["verdict"] for r in records(tmp_path)] == ["kept", "kept"]
    assert all(r["audio"] for r in records(tmp_path)) and len(list((tmp_path / "audio").glob("*.wav"))) == 2


def test_saving_changes_nothing_about_what_is_dropped(tmp_path):
    for mode, kept in (("shadow", True), ("enforce", False)):
        g = gate(mode, None, tmp_path)
        audio = clip(1.172)
        assert g.passes(g.measure(audio), "Thank you.", no_speech=0.0, audio=audio) is kept


def test_only_the_newest_files_are_kept(tmp_path):
    at = 1790605434.9
    paths = [speech_gate.save_clip(clip(0.1), tmp_path, at + i * 61.5, keep=3) for i in range(5)]
    assert sorted(tmp_path.glob("*.wav")) == paths[2:]
    same_ms = speech_gate.save_clip(clip(0.1), tmp_path, at + 4 * 61.5, keep=3)  # a name already taken
    assert same_ms != paths[4] and same_ms.is_file() and paths[4].is_file() and not paths[2].exists()


def test_the_gate_prunes_as_it_saves(tmp_path):
    g = gate("shadow", None, tmp_path, keep=2)
    for _ in range(4):
        audio = clip(0.5)
        g.passes(g.measure(audio), "Thank you.", 0.0, audio)
    saved = sorted((tmp_path / "audio").glob("*.wav"))
    assert len(saved) == 2 and [str(p) for p in saved] == [r["audio"] for r in records(tmp_path)[-2:]]


def test_with_saving_off_no_audio_is_written(tmp_path):
    g = SpeechGate("shadow", LOOSE, fake_vad(None), record=tmp_path / "gate.jsonl", audio_dir=None)
    audio = clip(1.2)
    g.passes(g.measure(audio), "Thank you.", 0.9, audio)
    assert records(tmp_path)[0]["audio"] is None and list(tmp_path.iterdir()) == [tmp_path / "gate.jsonl"]
    assert "saving" not in g.describe()


def test_a_clip_that_cant_be_saved_still_gets_judged(tmp_path, caplog):
    (tmp_path / "audio").write_text("not a folder")
    g = gate("enforce", (0.4, 0.8), tmp_path)
    audio = clip(1.2, (0.4, 0.8))
    assert g.passes(g.measure(audio), "Hello.", 0.0, audio)
    assert records(tmp_path)[0]["audio"] is None and "couldn't save" in caplog.text


def test_whisper_hands_the_gate_the_audio_it_heard(tmp_path, monkeypatch):
    stt = speech.WhisperSTT(gate=gate("shadow", None, tmp_path))
    monkeypatch.setattr(stt, "_transcribe", transcribes("Thank you.", 0.0))
    assert asyncio.run(stt._hear(buffer(clip(1.172)))) == "Thank you."
    params, _ = read_wav(records(tmp_path)[0]["audio"])
    assert params.framerate == RATE and abs(params.nframes - int(1.172 * RATE)) <= 1


def test_the_summary_points_at_each_flagged_clip(tmp_path):
    g = gate("shadow", None, tmp_path)
    audio = clip(1.172)
    g.passes(g.measure(audio), "Thank you.", 0.0, audio)
    assert records(tmp_path)[0]["audio"] in speech_gate.summary(tmp_path / "gate.jsonl")
    assert "saving each clip to" in g.describe()


def test_with_save_all_off_only_the_short_clips_it_lets_through_are_saved(tmp_path):
    g = gate("enforce", (0.4, 0.8), tmp_path, save_all=False)
    assert g.passes(g.measure(audio := clip(1.2, (0.4, 0.8))), "Okay.", 0.0, audio)  # short: saved
    g = gate("enforce", (0.3, 2.5), tmp_path, save_all=False)
    assert g.passes(g.measure(audio := clip(3.0, (0.3, 2.5))), "What's on my calendar today?", 0.0, audio)  # long: not
    g = gate("enforce", (1.0, 1.4), tmp_path, save_all=False)
    assert g.passes(g.measure(audio := clip(2.5, (1.0, 1.4))), "Yes.", 0.0, audio)  # long, but little speech: saved
    g = gate("enforce", None, tmp_path, save_all=False)
    assert not g.passes(g.measure(audio := clip(1.2)), "Thank you.", 0.0, audio)  # dropped: not saved
    g = gate("shadow", None, tmp_path, save_all=False)
    assert g.passes(g.measure(audio := clip(1.2)), "Thank you.", 0.0, audio)  # would drop, but let through: saved
    saved = [r["audio"] for r in records(tmp_path)]
    assert [bool(a) for a in saved] == [True, False, True, False, True]
    assert sorted(str(p) for p in (tmp_path / "audio").glob("*.wav")) == sorted(a for a in saved if a)
    assert "saving the short clips it lets through (under 2 s, or under 0.5 s of speech) to" in g.describe()


def test_short_clips_are_pruned_to_the_same_limit(tmp_path):
    g = gate("enforce", (0.4, 0.8), tmp_path, keep=2, save_all=False)
    for _ in range(4):
        g.passes(g.measure(audio := clip(1.2, (0.4, 0.8))), "Okay.", 0.0, audio)
    assert len(list((tmp_path / "audio").glob("*.wav"))) == 2


def test_the_phantom_and_vad_settings(monkeypatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)  # the real .env mustn't leak in
    for var in ("JARVIS_SPEECH_GATE_PHANTOM_DBFS", "JARVIS_SPEECH_GATE_PHANTOM_SPEECH_SECONDS",
                "JARVIS_SPEECH_GATE_PHANTOMS", "JARVIS_SPEECH_GATE_MIN_LOGPROB", "JARVIS_SPEECH_GATE_UNSURE_SPEECH_SECONDS",
                "JARVIS_SPEECH_GATE_MAX_COMPRESSION", "JARVIS_VAD_MIN_SPEECH", "JARVIS_VAD_THRESHOLD"):
        monkeypatch.delenv(var, raising=False)
    try:
        importlib.reload(config)
        assert (config.SPEECH_GATE_PHANTOM_DBFS, config.SPEECH_GATE_PHANTOM_SPEECH_SECONDS, config.SPEECH_GATE_PHANTOMS,
                config.SPEECH_GATE_MIN_LOGPROB, config.SPEECH_GATE_UNSURE_SPEECH_SECONDS,
                config.SPEECH_GATE_MAX_COMPRESSION) == (-22, 0.6, [], -1.0, 0.5, 2.4)
        assert (config.VAD_MIN_SPEECH, config.VAD_THRESHOLD) == (0.2, 0.5)
        monkeypatch.setenv("JARVIS_SPEECH_GATE_PHANTOMS", "Thank you., cheers ,")
        monkeypatch.setenv("JARVIS_VAD_MIN_SPEECH", "0.15")
        importlib.reload(config)
        assert config.SPEECH_GATE_PHANTOMS == ["Thank you.", "cheers"] and config.VAD_MIN_SPEECH == 0.15
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_the_default_phantoms_never_include_a_real_reply():
    for reply in ("yes", "no", "okay", "ok", "sure", "go ahead", "yeah", "yep", "nope", "stop", "wait", "cancel"):
        assert reply not in Thresholds().phantoms and reply not in LOOSE.phantoms, reply


def test_saving_is_on_by_default_only_in_shadow_mode(monkeypatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)  # the real .env mustn't leak in
    for var in ("JARVIS_SPEECH_GATE", "JARVIS_SPEECH_GATE_SAVE_AUDIO", "JARVIS_SPEECH_GATE_AUDIO_KEEP",
                "JARVIS_SPEECH_GATE_SAVE_SHORT", "JARVIS_SPEECH_GATE_SHORT_SECONDS",
                "JARVIS_SPEECH_GATE_SHORT_SPEECH_SECONDS", "JARVIS_SPEECH_GATE_FAINT_SPEECH_SECONDS",
                "JARVIS_SPEECH_GATE_FAINT_DBFS", "JARVIS_SPEECH_GATE_FAINT_MAX_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    try:
        importlib.reload(config)
        assert preferences.speech_gate() == "enforce" and preferences.speech_gate_save_audio() is False
        monkeypatch.setenv("JARVIS_SPEECH_GATE", "shadow")
        importlib.reload(config)
        assert preferences.speech_gate_save_audio() is True
        assert config.SPEECH_GATE_AUDIO_KEEP == 300
        assert config.SPEECH_GATE_AUDIO_DIR == config.LOGS_DIR / "speech-gate-audio"
        assert (config.SPEECH_GATE_FAINT_SPEECH_SECONDS, config.SPEECH_GATE_FAINT_DBFS,
                config.SPEECH_GATE_FAINT_MAX_SECONDS) == (0.25, -28, 1.4)
        monkeypatch.setenv("JARVIS_SPEECH_GATE", "enforce")
        importlib.reload(config)
        assert preferences.speech_gate_save_audio() is False
        assert config.SPEECH_GATE_SAVE_SHORT is True  # short clips it lets through are still saved
        assert (config.SPEECH_GATE_SHORT_SECONDS, config.SPEECH_GATE_SHORT_SPEECH_SECONDS) == (2.0, 0.5)
        monkeypatch.setenv("JARVIS_SPEECH_GATE_SAVE_SHORT", "off")
        monkeypatch.setenv("JARVIS_SPEECH_GATE_FAINT_DBFS", "-32")
        importlib.reload(config)
        assert config.SPEECH_GATE_SAVE_SHORT is False and config.SPEECH_GATE_FAINT_DBFS == -32
        monkeypatch.setenv("JARVIS_SPEECH_GATE_SAVE_AUDIO", "on")
        monkeypatch.setenv("JARVIS_SPEECH_GATE_AUDIO_KEEP", "50")
        importlib.reload(config)
        assert preferences.speech_gate_save_audio() is True and config.SPEECH_GATE_AUDIO_KEEP == 50
        monkeypatch.setenv("JARVIS_SPEECH_GATE", "shadow")
        monkeypatch.setenv("JARVIS_SPEECH_GATE_SAVE_AUDIO", "off")
        importlib.reload(config)
        assert preferences.speech_gate_save_audio() is False
    finally:
        monkeypatch.undo()
        importlib.reload(config)
