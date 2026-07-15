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
