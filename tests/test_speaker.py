"""Tests for jarvis.voice.speaker: voiceprints, judging utterances, dropping other speakers, enrolling."""

import asyncio
import base64
import json
import logging

import numpy as np
import pytest
from livekit import rtc
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

from jarvis import config, panel, tools
from jarvis.panel import preferences as panel_preferences
from jarvis.state import conversation, preferences, settings, voiceprints
from jarvis.state.voiceprints import OWNER, Voiceprint
from jarvis.voice import speaker, speech
from jarvis.voice.floor import Floor
from jarvis.voice.speaker import RATE, WINDOW, SpeakerCheck, Thresholds
from jarvis.voice.speech_gate import SpeechGate

DIM = 8  # real embeddings are 256-d; distinct voices, and voice vs room sound, are near-orthogonal
USER = np.eye(DIM, dtype=np.float32)[0]


def voice(alike: float, axis: int = 1) -> np.ndarray:
    """Unit embedding with cosine `alike` to USER, the rest along `axis` (1: user via another mic, 2+: others)."""
    v = np.zeros(DIM, np.float32)
    v[0], v[axis] = alike, np.sqrt(max(0.0, 1 - alike ** 2))
    return v


OTHER = voice(0.0, 2)


def clip(seconds: float, alike: float, axis: int | None = None) -> np.ndarray:
    """Fake audio for `seconds` of speech by a voice `alike` to the user's.

    Default axis: 1 (the user via some mic) if alike >= 0.5, else 2 (someone else). The fake embedder reads it back
    from any even-aligned sample pair, so slices of it embed the same.
    """
    audio = np.empty(max(2, int(seconds * RATE) // 2 * 2), np.float32)
    audio[0::2], audio[1::2] = alike, (axis or (1 if alike >= 0.5 else 2)) / 10
    return audio


class FakeEmbedder:
    available = True
    path = config.SPEAKER_MODEL

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, speech_: np.ndarray) -> np.ndarray:
        self.calls += 1
        if len(speech_) < speaker.MIN_EMBED_SECONDS * RATE:
            raise ValueError("too little speech to embed")
        return voice(float(speech_[0]), round(float(speech_[1]) * 10))


def all_speech(audio: np.ndarray) -> np.ndarray:
    return np.ones(len(audio) // WINDOW + (len(audio) % WINDOW > 0), np.float32)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def check(mode: str = "on", tmp_path=None, **thresholds) -> tuple[SpeakerCheck, Clock]:
    clock = Clock()
    kept: list[Voiceprint] = []
    c = SpeakerCheck(mode, Thresholds(**{"threshold": 0.35, "adapt": True, **thresholds}), FakeEmbedder(), all_speech,
                     record=tmp_path / "speaker.jsonl" if tmp_path else None, clock=clock,
                     keep=lambda p: kept.append(p) or p)
    c.prints = [Voiceprint(USER, 25.0, 0.8)]
    c.kept = kept
    return c, clock


def forget_all() -> None:
    """Remove every voiceprint from the test database."""
    for p in voiceprints.every():
        voiceprints.remove(p.name)


# ── Features and the model ──────────────────────────────────────


def test_fbank_has_a_frame_every_10_ms_and_80_mel_bands():
    assert speaker.fbank(np.zeros(RATE, np.float32)).shape == (98, 80)  # 1 + (16000 - 400) // 160
    assert speaker.fbank(np.zeros(399, np.float32)).shape == (0, 80)


def test_fbank_of_silence_is_kaldis_floor_and_ignores_a_dc_offset():
    assert np.allclose(speaker.fbank(np.zeros(800, np.float32)), np.log(speaker.EPSILON))
    t = np.arange(RATE // 2) / RATE
    tone = (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    assert np.allclose(speaker.fbank(tone), speaker.fbank(tone + 0.1), atol=1e-3)  # each frame's DC is taken out


def test_fbank_puts_a_tone_in_the_mel_band_around_it():
    t = np.arange(RATE) / RATE
    low, high = (speaker.fbank((0.3 * np.sin(2 * np.pi * hz * t)).astype(np.float32)).mean(axis=0).argmax()
                 for hz in (300, 3000))
    centres = 700 * (np.exp((speaker._mel(20) + (np.arange(80) + 1) * (speaker._mel(8000) - speaker._mel(20)) / 81)
                            / 1127) - 1)
    assert abs(centres[low] - 300) < 40 and abs(centres[high] - 3000) < 200


@pytest.mark.skipif(not config.SPEAKER_MODEL.is_file(), reason="the speaker model isn't downloaded (make models)")
def test_the_real_model_gives_a_unit_embedding_alike_for_the_same_audio():
    embed = speaker.Embedder()
    rng = np.random.default_rng(0)
    audio = (0.1 * rng.standard_normal(2 * RATE)).astype(np.float32)
    a, b = embed(audio), embed(audio.copy())
    assert a.shape == (256,) and abs(np.linalg.norm(a) - 1) < 1e-5 and speaker.cosine(a, b) > 0.999
    with pytest.raises(ValueError):
        embed(audio[:1000])


def test_only_speech_is_judged_widened_a_little_at_each_edge():
    audio = np.arange(20 * WINDOW, dtype=np.float32)
    probs = np.zeros(20, np.float32)
    probs[10] = 0.9
    kept = speaker.speech_only(audio, lambda a: probs)
    assert np.array_equal(kept, audio[8 * WINDOW:13 * WINDOW])  # window 10, plus two either side
    assert len(speaker.speech_only(audio, lambda a: np.zeros(20))) == 0


# ── What a clip needs ───────────────────────────────────────────


def test_short_clips_need_less_and_less_again_in_a_conversation():
    t = Thresholds(threshold=0.35, short_margin=0.1, relax=0.1, short_seconds=1.0, long_seconds=3.0)
    assert t.needed(5.0) == t.needed(3.0) == 0.35
    assert t.needed(0.5) == t.needed(1.0) == 0.25
    assert t.needed(2.0) == 0.3
    assert t.needed(0.5, engaged=True) == 0.15
    assert t.needed(2.0, engaged=True) == 0.25
    assert t.needed(4.0, engaged=True) == 0.35  # a long one is judged in full


# ── Judging clips ───────────────────────────────────────────────


def test_without_a_voiceprint_or_with_the_check_off_nothing_is_judged():
    c, _ = check()
    c.prints = []
    assert not c.on and c.judge(clip(3, 0.0)) is None
    c, _ = check("off")
    assert c.judge(clip(3, 0.0)) is None and c._embed.calls == 0


def test_the_users_voice_is_kept_and_someone_elses_dropped(tmp_path):
    c, _ = check(tmp_path=tmp_path)
    mine, theirs = c.judge(clip(3, 0.7)), c.judge(clip(3, 0.1))
    assert mine.verdict == "kept" and mine.score == pytest.approx(0.7, abs=1e-3) and mine.reference == OWNER
    assert theirs.drop and theirs.why == "not the user's voice" and theirs.needed == 0.35


def test_in_shadow_mode_someone_else_is_only_logged(tmp_path):
    c, _ = check("shadow", tmp_path=tmp_path)
    j = c.judge(clip(3, 0.1))
    assert j.verdict == "would drop" and not j.drop and j.other


def test_a_short_yes_right_after_jarvis_speaks_needs_less():
    c, clock = check()
    assert c.judge(clip(0.6, 0.2)).drop  # cold: 0.25 needed
    c.jarvis_speaking(True)
    c.jarvis_speaking(False)
    clock.now += 3
    j = c.judge(clip(0.6, 0.2))
    assert j.engaged and j.needed == 0.15 and j.verdict == "kept"
    clock.now += config.SPEAKER_REPLY_SECONDS + 1
    assert c.judge(clip(0.6, 0.1)).drop  # long after: judged as any other


def test_too_short_to_tell_goes_through_only_in_a_conversation():
    c, clock = check()
    j = c.judge(clip(0.3, -0.5))
    assert j.drop
    c.judge(clip(3, 0.8))  # the user, clearly
    clock.now += 5
    j = c.judge(clip(0.3, -0.5))
    assert j.verdict == "kept" and j.why == "too short to tell, in a conversation"
    j = c.judge(clip(0.1, 0.0))  # no embedding at all
    assert j.score is None and j.verdict == "kept"


def test_once_someone_else_is_heard_short_clips_lose_the_benefit():
    c, clock = check()
    c.judge(clip(3, 0.8))
    clock.now += 2
    assert c.judge(clip(2, 0.0)).drop  # the TV, long enough to tell
    clock.now += 1
    j = c.judge(clip(0.6, 0.2))
    assert not j.engaged and j.drop
    c.jarvis_speaking(True)  # the user asked something, and JARVIS answers it
    c.jarvis_speaking(False)
    clock.now += 1
    assert c.judge(clip(0.6, 0.2)).verdict == "kept"


def test_the_users_clear_utterances_become_a_reference_for_this_call():
    c, _ = check()
    c.prints = [Voiceprint(voice(0.5, 3), 25.0, 0.8)]  # enrolled via another mic; in this call they sound like USER
    assert c.judge(clip(4, 1.0, 1)).score == pytest.approx(0.5, abs=1e-3)  # 0.5 >= 0.35 + 0.1: adapted
    j = c.judge(clip(2, 0.95, 1))
    assert j.reference == "this call" and j.score > 0.8
    assert c.judge(clip(0.8, 0.95, 1)).reference == OWNER  # too short to compare with the call's
    assert c.judge(clip(3, 0.1)).drop  # someone else is no closer to it


def test_the_call_reference_stays_anchored_to_the_voiceprint():
    c, _ = check()
    c.judge(clip(4, 0.5, 1))  # the user, faintly like their voiceprint: adapted
    j = c.judge(clip(3, 0.0, 1))  # all room, no user voice: halfway back to the voiceprint, no more
    assert j.reference == "this call" and j.score < 0.65


def test_adapting_can_be_switched_off():
    c, _ = check(adapt=False)
    c.judge(clip(4, 1.0))
    assert c.judge(clip(2, 0.95)).reference == OWNER


def test_another_enrolled_voice_counts_as_the_user():
    c, _ = check()
    c.prints.append(Voiceprint(OTHER, 20.0, 0.8, name="guest"))
    j = c.judge(clip(3, 0.0))  # OTHER exactly
    assert j.verdict == "kept" and j.reference == "guest"


def test_a_failing_model_lets_clips_through_and_says_so_once(caplog):
    c, _ = check()

    def broken(_):
        raise RuntimeError("onnx went away")

    c._embed = type("Broken", (), {"available": True, "__call__": staticmethod(broken)})()
    with caplog.at_level(logging.ERROR, "jarvis.voice.speaker"):
        assert c.judge(clip(3, 0.0)) is None and c.judge(clip(3, 0.0)) is None
    assert len([r for r in caplog.records if "couldn't judge" in r.message]) == 1


def test_a_missing_model_turns_the_check_off(caplog):
    c, _ = check()
    c._embed = speaker.Embedder(config.ROOT / "models/none-such.onnx")
    with caplog.at_level(logging.WARNING, "jarvis.voice.speaker"):
        c.use("on", [Voiceprint(USER, 25, 0.8)], 0.35)
    assert not c.on and "make models" in caplog.text


def test_use_takes_the_conversations_settings_and_forgets_the_last_ones():
    c, _ = check()
    c.judge(clip(4, 1.0))
    c.use("shadow", [Voiceprint(OTHER, 20, 0.8)], 0.5)
    assert c.mode == "shadow" and c.thresholds.threshold == 0.5 and not c._session and not c.engaged()
    assert "shadow mode" in c.describe() and "0.5" in c.describe()
    c.use("on", [], 0.35)
    assert "no voiceprint" in c.describe()


def test_every_judged_clip_is_logged_and_summarised(tmp_path, caplog):
    c, _ = check(tmp_path=tmp_path)
    with caplog.at_level(logging.INFO, "jarvis.voice.speaker"):
        c.record(c.judge(clip(3, 0.7)), "What's on today?")
        c.record(c.judge(clip(3, 0.0)), "tomato")
    lines = [json.loads(l) for l in (tmp_path / "speaker.jsonl").read_text().splitlines()]
    assert [l["verdict"] for l in lines] == ["kept", "dropped"] and lines[1]["text"] == "tomato"
    assert "speaker check dropped" in caplog.text and "tomato" in caplog.text
    found = speaker.summary(tmp_path / "speaker.jsonl")
    assert "2 clips, 1 judged not the user's" in found and "'tomato'" in found and "What's on" not in found
    assert "doesn't exist" in speaker.summary(tmp_path / "none.jsonl")


# ── Making a voiceprint ─────────────────────────────────────────


def test_a_voiceprint_averages_its_pieces_and_leaves_out_an_odd_one():
    speech_ = np.concatenate([clip(3, 0.9), clip(3, 0.95), clip(3, 0.85), clip(3, 0.9), clip(3, -0.9)])
    made = speaker.voiceprint(speech_, FakeEmbedder(), minimum=5)
    assert made.seconds == pytest.approx(15) and made.consistency > 0.95 and made.via == "voice"
    assert speaker.cosine(made.embedding, USER) > 0.85  # the -0.9 piece (a cough, someone else) is excluded


def test_a_voiceprint_needs_enough_speech_from_one_voice():
    with pytest.raises(ValueError, match="only 4 seconds"):
        speaker.voiceprint(clip(4, 0.9), FakeEmbedder(), minimum=10)
    mixed = np.concatenate([clip(3, 1.0), clip(3, 0.0, 2), clip(3, 0.0, 3)])  # three people
    with pytest.raises(ValueError, match="one voice"):
        speaker.voiceprint(mixed, FakeEmbedder(), minimum=5)


def test_pieces_join_a_short_end_to_the_one_before():
    assert [len(p) / RATE for p in speaker.pieces(np.zeros(int(7.2 * RATE)))] == [3.0, 4.2]
    assert [len(p) / RATE for p in speaker.pieces(np.zeros(int(8 * RATE)))] == [3.0, 3.0, 2.0]


def test_enrolling_from_a_recording_keeps_only_its_speech(monkeypatch):
    saved = []
    monkeypatch.setattr(voiceprints, "save", lambda p: saved.append(p) or p)
    audio = np.concatenate([np.zeros(10 * WINDOW, np.float32), clip(15, 0.9)])

    def probs(a: np.ndarray) -> np.ndarray:  # silence, then speech
        return np.r_[np.zeros(10), np.ones(len(a) // WINDOW - 10)]

    made = speaker.enroll(audio, via="cli", embed=FakeEmbedder(), speech_probs=probs)
    assert saved == [made] and made.via == "cli" and 15 <= made.seconds < 15.2


def test_pcm16_comes_in_as_float_audio():
    pcm = np.array([0, 16384, -32768], "<i2").tobytes() + b"\x01"  # a stray odd byte is ignored
    assert speaker.to_float(pcm).tolist() == [0.0, 0.5, -1.0]


# ── Keeping voiceprints ─────────────────────────────────────────


def test_voiceprints_are_kept_replaced_and_forgotten():
    forget_all()
    voiceprints.save(Voiceprint(voice(0.3), 20.0, 0.7, via="panel"))
    voiceprints.save(Voiceprint(USER, 26.0, 0.8, via="voice"))  # enrolling again replaces it
    voiceprints.save(Voiceprint(OTHER, 18.0, 0.75, name="guest"))
    voiceprints.save(Voiceprint(OTHER, 18.0, 0.75, name="old", model="ecapa-tdnn"))
    found = voiceprints.load()
    assert [p.name for p in found] == [OWNER, "guest"] and np.allclose(found[0].embedding, USER)
    assert found[0].seconds == 26.0 and found[0].via == "voice" and found[0].created > 0
    assert [p.name for p in voiceprints.every()] == [OWNER, "guest", "old"]  # another model's print is left out of load
    assert voiceprints.every()[2].described()["current"] is False
    assert voiceprints.remove("old") and voiceprints.remove("guest") and not voiceprints.remove("guest")
    assert voiceprints.remove() and voiceprints.load() == []


# ── Enrolling by voice ──────────────────────────────────────────


def test_enrolling_by_voice_collects_clips_then_keeps_the_voiceprint(monkeypatch):
    monkeypatch.setattr(speaker, "SPEAKER_ENROLL_SECONDS", 12.0)
    c, _ = check()
    c.prints = []
    told = c.start_enrollment()
    assert c.enrolling and "thirty seconds" in told and "stop" in told
    assert c.collect(clip(5, 0.9), "My day started early.") is None
    c.jarvis_speaking(True)
    assert c.collect(clip(5, 0.0), "JARVIS's own voice") is None  # collected nothing while JARVIS speaks
    c.jarvis_speaking(False)
    assert c.collect(clip(4, 0.92), "and then") is None
    said = c.collect(clip(4, 0.88), "that's all.")
    assert said.startswith("Got it") and "ignore other voices" in said and not c.enrolling
    [made] = c.kept
    assert made.via == "voice" and made.seconds == pytest.approx(13) and speaker.cosine(made.embedding, USER) > 0.85
    assert c.prints == [made] and c.on and c.engaged()  # the new voiceprint applies at once


def test_saying_stop_ends_an_enrollment_and_keeps_the_voiceprint():
    c, _ = check()
    before = list(c.prints)
    c.start_enrollment()
    assert c.collect(clip(3, 0.9), "Cancel that.").startswith("Okay, I've stopped")
    assert not c.enrolling and c.prints == before and c.kept == []


def test_an_enrollment_that_cant_make_a_voiceprint_says_why(monkeypatch):
    monkeypatch.setattr(speaker, "SPEAKER_ENROLL_SECONDS", 6.0)
    c, _ = check("shadow")
    c.start_enrollment()
    c.collect(clip(3, 1.0), "")
    said = c.collect(clip(3, -1.0), "")
    assert said.startswith("I couldn't learn your voice") and not c.enrolling and c.kept == []


def test_a_spoken_enrollment_finishes_after_a_pause_or_gives_up(monkeypatch):
    monkeypatch.setattr(speaker, "IDLE_SECONDS", 0.01)
    monkeypatch.setattr(speaker, "SPEAKER_ENROLL_MIN_SECONDS", 4.0)
    monkeypatch.setattr(speaker, "SPEAKER_ENROLL_TIMEOUT", 0.05)

    async def run(seconds: float) -> list[str]:
        c, _ = check("off")
        said: list[str] = []
        c.announce = said.append
        c.start_enrollment()
        await asyncio.to_thread(c.collect, clip(seconds, 0.9), "")
        c.rearm()
        await asyncio.sleep(0.3)
        assert not c.enrolling
        return said

    [done] = asyncio.run(run(5))
    assert done.startswith("Got it") and "switch it on in Settings" in done  # the check is off
    [gave_up] = asyncio.run(run(1))
    assert "only 1 seconds" in gave_up and "learn your voice again" in gave_up


def test_an_old_enrollments_timer_leaves_a_new_one_alone(monkeypatch):
    monkeypatch.setattr(speaker, "SPEAKER_ENROLL_TIMEOUT", 0.05)

    async def run() -> list[str]:
        c, _ = check()
        said: list[str] = []
        c.announce = said.append
        c.start_enrollment()
        old = c._enrolling
        c.cancel_enrollment()
        c.start_enrollment()
        await c._lapse(old)  # the first one's timer, had it fired: no effect
        assert c.enrolling and not said
        c.cancel_enrollment()
        return said

    assert asyncio.run(run()) == []


def test_enrolling_again_keeps_the_users_voiceprint_first(monkeypatch):
    monkeypatch.setattr(speaker, "SPEAKER_ENROLL_SECONDS", 6.0)
    c, _ = check()
    c.prints = [Voiceprint(voice(0.6), 25.0, 0.8), Voiceprint(OTHER, 20.0, 0.8, name="guest")]
    c.start_enrollment()
    assert c.collect(clip(6, 1.0), "").startswith("Got it")
    assert [p.name for p in c.prints] == [OWNER, "guest"] and np.allclose(c.prints[0].embedding, USER, atol=1e-3)


def test_without_the_model_enrolling_says_to_download_it():
    c = SpeakerCheck(embedder=speaker.Embedder(config.ROOT / "models/none-such.onnx"), record=None)
    assert "make models" in c.start_enrollment() and not c.enrolling


def test_the_voice_enroll_tool_reaches_this_conversations_check(monkeypatch):
    monkeypatch.setattr(conversation.current(), "speaker", None)
    assert "only works in a voice conversation" in asyncio.run(tools.run("voice_enroll", {}))[0]
    c, _ = check()
    monkeypatch.setattr(conversation.current(), "speaker", c)
    text, error = asyncio.run(tools.run("voice_enroll", {}))
    assert not error and "Listening for their voice" in text
    text, _ = asyncio.run(tools.run("voice_enroll", {"stop": True}))
    assert text.startswith("Stopped") and not c.enrolling
    assert asyncio.run(tools.run("voice_enroll", {"stop": True}))[0] == "No enrollment was under way."
    c.cancel_enrollment()


def test_announcements_wait_while_the_user_enrolls():
    assert Floor().held("listening", "listening", 0, enrolling=True) == "the user is enrolling their voice"
    assert Floor().held("listening", "listening", 0) is None


# ── In the voice pipeline ───────────────────────────────────────


class FakeExecutor:
    def __init__(self, text: str) -> None:
        self.text, self.calls = text, 0

    async def do_inference(self, method: str, data: bytes) -> bytes:
        self.calls += 1
        return json.dumps({"text": self.text, "no_speech": 0.0, "avg_logprob": -0.2}).encode()


def hear(stt: speech.WhisperSTT, audio: np.ndarray) -> str:
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    event = asyncio.run(stt._recognize_impl([rtc.AudioFrame(pcm.tobytes(), RATE, 1, len(pcm))],
                                            conn_options=DEFAULT_API_CONNECT_OPTIONS))
    return event.alternatives[0].text


def pipeline(text: str, c: SpeakerCheck) -> tuple[speech.WhisperSTT, FakeExecutor]:
    executor = FakeExecutor(text)
    return speech.WhisperSTT(gate=SpeechGate("off", record=None, audio_dir=None), executor=executor, speaker=c), executor


def test_someone_elses_words_never_reach_the_brain(tmp_path):
    c, _ = check(tmp_path=tmp_path)
    stt, executor = pipeline("As then the URL it has six membership classes", c)
    assert hear(stt, clip(3, 0.05)) == "" and executor.calls == 1  # transcribed alongside, then dropped
    [line] = (tmp_path / "speaker.jsonl").read_text().splitlines()
    assert json.loads(line)["verdict"] == "dropped"


def test_the_users_words_go_through_and_are_logged(tmp_path):
    c, _ = check(tmp_path=tmp_path)
    stt, _ = pipeline("What's on my calendar?", c)
    assert hear(stt, clip(3, 0.7)) == "What's on my calendar?"
    assert json.loads((tmp_path / "speaker.jsonl").read_text())["text"] == "What's on my calendar?"


def test_in_shadow_mode_someone_elses_words_still_go_through(tmp_path):
    c, _ = check("shadow", tmp_path=tmp_path)
    stt, _ = pipeline("tomato", c)
    assert hear(stt, clip(3, 0.0)) == "tomato"


def test_with_no_voiceprint_everything_goes_through_unjudged():
    c, _ = check()
    c.prints = []
    stt, _ = pipeline("Hello.", c)
    assert hear(stt, clip(3, -1.0)) == "Hello." and c._embed.calls == 0


def test_while_enrolling_clips_go_to_the_voiceprint_not_the_brain(monkeypatch):
    monkeypatch.setattr(speaker, "SPEAKER_ENROLL_SECONDS", 5.0)
    c, _ = check()
    said: list[str] = []
    c.announce = said.append
    c.start_enrollment()
    stt, executor = pipeline("I had a long day at work today.", c)
    assert stt.enrolling
    assert hear(stt, clip(6, 0.9)) == "" and executor.calls == 1 and said and said[0].startswith("Got it")
    assert not stt.enrolling and c.kept


# ── Settings and the panel ──────────────────────────────────────


def test_the_check_and_its_threshold_are_preferences():
    settings.set_preference("speaker_check", "Shadow")
    settings.set_preference("speaker_threshold", "0.45")
    assert preferences.speaker_check() == "shadow" and preferences.speaker_threshold() == 0.45
    with pytest.raises(ValueError, match="on, shadow, off"):
        settings.set_preference("speaker_check", "maybe")
    with pytest.raises(ValueError, match="from 0.1 to 0.8"):
        settings.set_preference("speaker_threshold", "0.95")
    settings.set_preference("speaker_check", "")
    settings.set_preference("speaker_threshold", "")
    assert preferences.speaker_check() == "on" and preferences.speaker_threshold() == 0.35
    shown = settings.preferences()["preferences"]
    assert shown["speaker_check"]["default"] == "on" and shown["speaker_threshold"]["default"] == "0.35"


def test_the_panel_shows_enrolls_and_forgets_the_voiceprint(monkeypatch):
    forget_all()
    got = asyncio.run(panel.call("voiceprint", {}))
    assert got["owner"] is None and got["passage"] == speaker.PASSAGE and got["seconds"] == 30
    heard = []

    def enroll(audio, via):
        heard.append((audio, via))
        return voiceprints.save(Voiceprint(USER, 24.0, 0.8, via=via))

    monkeypatch.setattr(speaker, "enroll", enroll)
    pcm = (np.full(RATE, 0.25) * 32768).astype("<i2").tobytes()
    got = asyncio.run(panel.call("voiceprint_enroll", {"audio": base64.b64encode(pcm).decode()}))
    assert got["owner"]["via"] == "panel" and got["owner"]["seconds"] == 24.0 and "embedding" not in got["owner"]
    assert heard[0][1] == "panel" and np.allclose(heard[0][0], 0.25)
    with pytest.raises(ValueError, match="didn't arrive whole"):
        asyncio.run(panel.call("voiceprint_enroll", {"audio": "not base64!"}))
    too_long = base64.b64encode(b"\0" * (panel_preferences.MAX_ENROLL_SECONDS * RATE * 2 + 2)).decode()
    with pytest.raises(ValueError, match="too long"):
        asyncio.run(panel.call("voiceprint_enroll", {"audio": too_long}))
    assert asyncio.run(panel.call("voiceprint_remove", {}))["owner"] is None


def test_the_panel_passes_on_why_a_recording_wont_do(monkeypatch):
    monkeypatch.setattr(speaker, "Embedder", FakeEmbedder)
    monkeypatch.setattr(speaker, "silero", lambda: all_speech)
    pcm = (np.full(3 * RATE, 0.25) * 32768).astype("<i2").tobytes()
    with pytest.raises(ValueError, match="only 3 seconds of speech"):
        asyncio.run(panel.call("voiceprint_enroll", {"audio": base64.b64encode(pcm).decode()}))


# ── The command line ────────────────────────────────────────────


def test_the_command_line_enrolls_shows_tests_and_forgets(monkeypatch, capsys, tmp_path):
    forget_all()
    model = tmp_path / "model.onnx"
    model.write_bytes(b"")
    monkeypatch.setattr(speaker, "SPEAKER_MODEL", model)
    monkeypatch.setattr(speaker, "Embedder", FakeEmbedder)
    monkeypatch.setattr(speaker, "silero", lambda: all_speech)
    monkeypatch.setattr(speaker.time, "sleep", lambda s: None)
    assert speaker.main(["status"]) == 0 and "No voiceprint" in capsys.readouterr().out
    assert speaker.main(["enroll", "--seconds", "20"], record=lambda s: clip(s, 0.9)) == 0
    out = capsys.readouterr().out
    assert "Read this aloud" in out and "Saved owner's voiceprint: 20 s of speech" in out
    assert speaker.main(["status"]) == 0 and "owner: 20 s of speech" in capsys.readouterr().out
    assert speaker.main(["test"], record=lambda s: clip(s, 0.9)) == 0
    assert "owner: 1.00" in capsys.readouterr().out
    assert speaker.main(["remove"]) == 0 and "Forgotten." in capsys.readouterr().out
    assert speaker.main(["remove"]) == 0 and "There's no voiceprint" in capsys.readouterr().out
