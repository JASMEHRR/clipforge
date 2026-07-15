"""avatar.py tests — LLM via the mock provider or monkeypatched, no network,
no TTS venv, no torch. TTS subprocess calls are always mocked."""
import json
import subprocess
from pathlib import Path

import pytest

import avatar
import llm
from errors import LLMError
from schemas import validate

TRANSCRIPT = ("The Falcon booster landed on the drone ship after the engine "
              "relight, and the telemetry showed the landing burn started "
              "two seconds late.")
HOOK = "The booster almost missed the drone ship"

CFG = {"llm": {"provider": "mock", "max_retries": 0,
               "backoff_base_seconds": 0.0},
       "avatar": {"script": {"intro_max_words": 30, "outro_max_words": 25}}}


# ------------------------------------------------------------- specificity

def test_script_is_specific_shared_content_word():
    assert avatar.script_is_specific(
        "Watch how the booster handles the landing burn", TRANSCRIPT)


def test_script_is_specific_rejects_generic():
    assert not avatar.script_is_specific(
        "Check this out, you will not believe what happens next!", TRANSCRIPT)


def test_script_is_specific_vacuous_on_empty_transcript():
    assert avatar.script_is_specific("Anything at all here", "")
    assert avatar.script_is_specific("Anything", "um yeah okay so like")


# --------------------------------------------------------------- word caps

def test_cap_words_short_text_unchanged():
    assert avatar._cap_words("Two words.", 30) == "Two words."


def test_cap_words_truncates_and_terminates():
    text = " ".join(f"w{i}" for i in range(40))
    capped = avatar._cap_words(text, 10)
    assert len(capped.split(" ")) == 10
    assert capped.endswith(".")


def test_cap_words_prefers_sentence_boundary():
    text = "First sentence ends right here. Second sentence keeps on going " \
           "with lots of extra words after it"
    capped = avatar._cap_words(text, 8)
    assert capped == "First sentence ends right here."


# ------------------------------------------------------- generate_script

def test_generate_script_mock_deterministic_and_valid():
    a = avatar.generate_script(TRANSCRIPT, HOOK, cfg=CFG, provider="mock")
    b = avatar.generate_script(TRANSCRIPT, HOOK, cfg=CFG, provider="mock")
    assert a == b
    validate(a, "avatar_script")
    assert avatar.script_is_specific(a["intro"], TRANSCRIPT)
    assert avatar.script_is_specific(a["outro"], TRANSCRIPT)


def test_generate_script_respects_word_caps():
    cfg = {**CFG, "avatar": {"script": {"intro_max_words": 6,
                                        "outro_max_words": 5}}}
    out = avatar.generate_script(TRANSCRIPT, HOOK, cfg=cfg, provider="mock")
    assert len(out["intro"].split(" ")) <= 6
    assert len(out["outro"].split(" ")) <= 5
    validate(out, "avatar_script")


def test_generate_script_template_fallback_on_llm_error(monkeypatch):
    def _boom(*args, **kwargs):
        raise LLMError("provider down")
    monkeypatch.setattr(llm, "complete_json", _boom)
    out = avatar.generate_script(TRANSCRIPT, HOOK, cfg=CFG)
    validate(out, "avatar_script")
    assert avatar.script_is_specific(out["intro"], TRANSCRIPT)
    assert avatar.script_is_specific(out["outro"], TRANSCRIPT)


def test_generate_script_retries_once_on_generic_then_accepts(monkeypatch):
    calls = []
    generic = {"intro": "You will not believe this one, wow!",
               "outro": "Like and subscribe for more, folks."}
    specific = {"intro": "The booster telemetry tells the real story here.",
                "outro": "So the landing burn timing decided everything."}

    def _fake(task, schema_name, prompt, **kwargs):
        calls.append(prompt)
        return generic if len(calls) == 1 else specific

    monkeypatch.setattr(llm, "complete_json", _fake)
    out = avatar.generate_script(TRANSCRIPT, HOOK, cfg=CFG)
    assert len(calls) == 2
    assert "MUST mention at least one of these words" in calls[1]
    assert out["intro"] == specific["intro"]


def test_generate_script_template_when_retry_still_generic(monkeypatch):
    generic = {"intro": "You will not believe this one, wow!",
               "outro": "Like and subscribe for more, folks."}
    monkeypatch.setattr(llm, "complete_json",
                        lambda *a, **k: dict(generic))
    out = avatar.generate_script(TRANSCRIPT, HOOK, cfg=CFG)
    validate(out, "avatar_script")
    assert avatar.script_is_specific(out["intro"], TRANSCRIPT)


def test_template_script_grounded_and_valid():
    out = avatar._template_script(HOOK, TRANSCRIPT)
    validate(out, "avatar_script")
    assert avatar.script_is_specific(out["intro"], TRANSCRIPT)
    assert avatar.script_is_specific(out["outro"], TRANSCRIPT)


def test_template_script_empty_inputs_still_valid():
    out = avatar._template_script("", "")
    validate(out, "avatar_script")


# --------------------------------------------------------- synthesize_batch

def _tts_cfg(tmp_path, **over):
    py = tmp_path / "py.exe"
    py.write_bytes(b"\x00")
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"\x00" * 16)
    tts = {"python": str(py), "ref_audio": str(ref), "device": "cpu",
           "timeout_s": 5, **over}
    return {"avatar": {"tts": tts}}


def _ok_reply(jobs):
    return json.dumps({"ok": True, "results": [
        {"out_path": j["out_path"], "duration_s": 4.2} for j in jobs]})


def test_synthesize_batch_missing_venv(tmp_path):
    cfg = _tts_cfg(tmp_path)
    cfg["avatar"]["tts"]["python"] = str(tmp_path / "nope.exe")
    with pytest.raises(avatar.AvatarError, match="setup-venv"):
        avatar.synthesize_batch([{"text": "x", "out_path": "y"}], cfg)


def test_synthesize_batch_missing_ref(tmp_path):
    cfg = _tts_cfg(tmp_path, ref_audio="")
    with pytest.raises(avatar.AvatarError, match="setup-voice"):
        avatar.synthesize_batch([{"text": "x", "out_path": "y"}], cfg)


def test_synthesize_batch_empty_jobs(tmp_path):
    assert avatar.synthesize_batch([], _tts_cfg(tmp_path)) == []


def test_synthesize_batch_happy_path(tmp_path, monkeypatch):
    cfg = _tts_cfg(tmp_path)
    jobs = [{"text": "hello there", "out_path": str(tmp_path / "a.wav")},
            {"text": "goodbye now", "out_path": str(tmp_path / "b.wav")}]

    def _fake_run(args, **kwargs):
        payload = json.loads(kwargs["input"])
        assert payload["ref_audio"] == cfg["avatar"]["tts"]["ref_audio"]
        assert payload["device"] == "cpu"
        for j in payload["jobs"]:
            Path(j["out_path"]).write_bytes(b"\x00" * 64)
        return subprocess.CompletedProcess(args, 0, stdout=_ok_reply(jobs),
                                           stderr="loaded model\n")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    res = avatar.synthesize_batch(jobs, cfg)
    assert [r["out_path"] for r in res] == [j["out_path"] for j in jobs]
    assert all(r["duration_s"] > 0 for r in res)


def test_synthesize_batch_nonzero_exit(tmp_path, monkeypatch):
    cfg = _tts_cfg(tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 1, stdout="",
                                                    stderr="boom traceback"))
    with pytest.raises(avatar.AvatarError, match="exited 1") as ei:
        avatar.synthesize_batch([{"text": "x", "out_path": "y"}], cfg)
    assert "boom" in (ei.value.detail or "")


def test_synthesize_batch_malformed_reply(tmp_path, monkeypatch):
    cfg = _tts_cfg(tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, stdout="not json",
                                                    stderr=""))
    with pytest.raises(avatar.AvatarError, match="malformed"):
        avatar.synthesize_batch([{"text": "x", "out_path": "y"}], cfg)


def test_synthesize_batch_worker_error_reply(tmp_path, monkeypatch):
    cfg = _tts_cfg(tmp_path)
    reply = json.dumps({"ok": False, "error": "RuntimeError: no voice"})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 1, stdout=reply,
                                                    stderr=""))
    with pytest.raises(avatar.AvatarError, match="no voice"):
        avatar.synthesize_batch([{"text": "x", "out_path": "y"}], cfg)


def test_synthesize_batch_timeout(tmp_path, monkeypatch):
    cfg = _tts_cfg(tmp_path)

    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="py", timeout=5)

    monkeypatch.setattr(subprocess, "run", _raise)
    with pytest.raises(avatar.AvatarError, match="timed out"):
        avatar.synthesize_batch([{"text": "x", "out_path": "y"}], cfg)


def test_synthesize_batch_output_missing_on_disk(tmp_path, monkeypatch):
    cfg = _tts_cfg(tmp_path)
    jobs = [{"text": "x", "out_path": str(tmp_path / "never_written.wav")}]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 0,
                                                    stdout=_ok_reply(jobs),
                                                    stderr=""))
    with pytest.raises(avatar.AvatarError, match="missing or empty"):
        avatar.synthesize_batch(jobs, cfg)


# ----------------------------------------------------- composite (pure fns)

TIMING_CFG = {"avatar": {"timing": {"pad_s": 0.4, "intro_min_s": 4.0,
                                    "intro_max_s": 12.0, "outro_min_s": 3.0,
                                    "outro_max_s": 10.0}}}


def test_segment_durations_pads_and_clamps():
    assert avatar.segment_durations(7.0, "intro", TIMING_CFG) == 7.4
    assert avatar.segment_durations(1.0, "intro", TIMING_CFG) == 4.0   # min
    assert avatar.segment_durations(60.0, "intro", TIMING_CFG) == 12.0  # max
    assert avatar.segment_durations(1.0, "outro", TIMING_CFG) == 3.0
    assert avatar.segment_durations(60.0, "outro", TIMING_CFG) == 10.0


LAYOUT = {"clip_scale": 0.62, "clip_y": 0.07, "avatar_scale": 0.42,
          "intro_side": "left", "outro_side": "right", "margin_px": 48}


def test_composite_graph_structure():
    g = avatar.build_composite_graph(1080, 1920, LAYOUT, 8.0, 6.0, 30.0)
    assert "concat=n=3:v=1:a=1[vcat][acat]" in g
    assert "[vcat]null[vout]" in g
    # intro avatar left, outro avatar right
    assert "[introb1][avI]overlay=48:H-h-48" in g
    assert "[outrob1][avO]overlay=W-w-48:H-h-48" in g
    # even-width scales
    assert "scale=668:-2" in g          # 1080*0.62=669.6 -> 668 (even)
    assert "scale=452:-1" in g          # 1080*0.42=453.6 -> 452 (even)
    # TTS trimmed/padded to the exact segment lengths
    assert "atrim=0:8.000" in g and "apad=whole_dur=8.000" in g
    assert "atrim=0:6.000" in g and "apad=whole_dur=6.000" in g


def test_composite_graph_side_swap():
    layout = {**LAYOUT, "intro_side": "right", "outro_side": "left"}
    g = avatar.build_composite_graph(1080, 1920, layout, 8.0, 6.0, 30.0)
    assert "[introb1][avI]overlay=W-w-48:H-h-48" in g
    assert "[outrob1][avO]overlay=48:H-h-48" in g


def test_composite_graph_with_subtitles(tmp_path):
    ass = tmp_path / "avatar.ass"
    g = avatar.build_composite_graph(1080, 1920, LAYOUT, 8.0, 6.0, 30.0,
                                     ass_path=ass, fontsdir=tmp_path)
    assert "[vcat]subtitles=filename=" in g and "[vout]" in g
    assert "fontsdir=" in g
    assert "null" not in g


# ----------------------------------------------------------- avatar captions

CAP_CFG = {
    "captions": {
        "preset": "karaoke-pop", "max_words_per_line": 3,
        "presets": {"karaoke-pop": {
            "font": "Montserrat ExtraBold", "font_size": 88,
            "primary_color": "&H00FFFFFF", "highlight_color": "&H0000D7FF",
            "outline_color": "&H00000000", "outline": 5, "shadow": 1,
            "highlight_scale": 108}}},
    "avatar": {"captions": {"intro_anchor": 0.80, "outro_anchor": 0.80}},
}


def test_write_avatar_ass_positions_and_offsets(tmp_path):
    intro_words = [{"word": "Hello", "start": 0.2, "end": 0.6},
                   {"word": "viewers", "start": 0.7, "end": 1.2}]
    outro_words = [{"word": "Goodbye", "start": 1.0, "end": 1.5}]
    ass = tmp_path / "avatar.ass"
    avatar.write_avatar_ass(intro_words, outro_words, ass, CAP_CFG,
                            intro_dur=8.0, outro_dur=6.0, main_dur=30.0)
    text = ass.read_text(encoding="utf-8-sig")
    # style derived from the active preset, alignment 5, avatar-zone position
    assert "Style: Avatar,Montserrat ExtraBold,61" in text
    assert r"\pos(540,1536)" in text                # 0.80 * 1920
    # intro events start at composite t=0; outro offset by intro+main = 38s
    assert "Dialogue: 0,0:00:00.20," in text
    assert "0:00:39.00" in text                     # outro word at 38 + 1.0
    # karaoke pop present
    assert r"\fscx108\fscy108" in text


def test_write_avatar_ass_drops_words_past_segment(tmp_path):
    intro_words = [{"word": "kept", "start": 0.5, "end": 1.0},
                   {"word": "dropped", "start": 11.9, "end": 12.4}]
    ass = tmp_path / "a.ass"
    avatar.write_avatar_ass(intro_words, [], ass, CAP_CFG,
                            intro_dur=8.0, outro_dur=6.0, main_dur=30.0)
    text = ass.read_text(encoding="utf-8-sig")
    assert "kept" in text
    assert "dropped" not in text


# ------------------------------------------------------------ prepare_avatar

def test_prepare_avatar_batches_all_clips(tmp_path, monkeypatch):
    transcript = {"words": [
        {"word": "quantum", "start": 1.0, "end": 1.4},
        {"word": "entanglement", "start": 1.5, "end": 2.0},
        {"word": "experiment", "start": 11.0, "end": 11.5},
        {"word": "telescope", "start": 12.0, "end": 12.6}]}
    candidates = [{"start": 0.0, "end": 5.0, "hook": "Quantum breakthrough"},
                  {"start": 10.0, "end": 15.0, "hook": "Telescope result"}]
    cfg = {"llm": {"provider": "mock", "max_retries": 0,
                   "backoff_base_seconds": 0.0},
           "avatar": {"script": {"intro_max_words": 30,
                                 "outro_max_words": 25},
                      # off: the offline test must not touch Whisper
                      "captions": {"enabled": False}}}
    batches = []

    def _fake_batch(jobs, c):
        batches.append(jobs)
        return [{"out_path": j["out_path"], "duration_s": 5.5} for j in jobs]

    monkeypatch.setattr(avatar, "synthesize_batch", _fake_batch)
    items = avatar.prepare_avatar(candidates, transcript, tmp_path, cfg,
                                  provider="mock")
    assert len(batches) == 1 and len(batches[0]) == 4   # ONE worker run
    assert len(items) == 2
    for i, item in enumerate(items):
        assert item["intro_s"] == 5.5 and item["outro_s"] == 5.5
        assert f"clip_{i:02d}_intro.wav" in item["intro_wav"]
        json.dumps(items)                                # marker-serializable
    # scripts grounded in each clip's own transcript window
    assert "quantum" in items[0]["intro_script"].lower()
    assert "experiment" in items[1]["intro_script"].lower() or \
        "telescope" in items[1]["intro_script"].lower()


# --------------------------------------------------------------- setup-voice

def test_setup_voice_rejects_bad_duration(tmp_path, monkeypatch):
    import ffutil
    src = tmp_path / "long.wav"
    src.write_bytes(b"\x00" * 16)
    monkeypatch.setattr(ffutil, "probe",
                        lambda p: {"has_audio": True, "duration": 95.0})
    monkeypatch.setattr(avatar, "VOICE_DIR", tmp_path / "voice")
    monkeypatch.setattr(avatar, "save_config", lambda u: u)
    with pytest.raises(avatar.AvatarError, match="3-30s"):
        avatar.setup_voice(str(src))


def test_setup_voice_installs_and_persists(tmp_path, monkeypatch):
    import ffutil
    src = tmp_path / "me.wav"
    src.write_bytes(b"\x00" * 32)
    saved = {}
    monkeypatch.setattr(ffutil, "probe",
                        lambda p: {"has_audio": True, "duration": 7.5})
    monkeypatch.setattr(avatar, "VOICE_DIR", tmp_path / "voice")
    monkeypatch.setattr(avatar, "ROOT", tmp_path)
    monkeypatch.setattr(avatar, "save_config", saved.update)
    dest = avatar.setup_voice(str(src))
    assert dest.is_file()
    assert saved["avatar"]["tts"]["ref_audio"] == "voice/ref.wav"
