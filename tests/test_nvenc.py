"""NVENC probe logic (ffutil._probe_nvenc / nvenc_available), subprocess mocked.
No GPU or ffmpeg binary is touched — this asserts the decision + reason logic."""
import subprocess
from types import SimpleNamespace

import ffutil


def _fake_runner(smi_rc=0, encoders="h264_nvenc", smoke_rc=0, smoke_err=""):
    """Return a subprocess.run stand-in keyed on which command it sees."""
    def run(cmd, *a, **k):
        exe = " ".join(cmd)
        if "nvidia-smi" in exe:
            return SimpleNamespace(returncode=smi_rc, stdout="", stderr="")
        if "-encoders" in cmd:
            return SimpleNamespace(returncode=0, stdout=encoders, stderr="")
        if "h264_nvenc" in cmd:  # the smoke encode
            return SimpleNamespace(returncode=smoke_rc, stdout="", stderr=smoke_err)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


def _patch(monkeypatch, **kw):
    monkeypatch.setattr(subprocess, "run", _fake_runner(**kw))
    monkeypatch.setattr(ffutil, "ffmpeg_bin", lambda: "ffmpeg")
    ffutil._nvenc = None  # reset the cache between cases


def test_available_when_all_pass(monkeypatch):
    _patch(monkeypatch)
    assert ffutil.nvenc_available() is True


def test_no_gpu(monkeypatch):
    _patch(monkeypatch, smi_rc=1)
    ok, reason = ffutil._probe_nvenc()
    assert ok is False and "no NVIDIA GPU" in reason


def test_encoder_not_compiled_in(monkeypatch):
    _patch(monkeypatch, encoders="libx264 av1")
    ok, reason = ffutil._probe_nvenc()
    assert ok is False and "no h264_nvenc" in reason


def test_smoke_encode_fails_reports_driver_line(monkeypatch):
    err = ("[h264_nvenc @ 0x0] Driver does not support the required nvenc API "
           "version. Required: 13.1 Found: 13.0\n"
           "[vf#0:0] Task finished with error code: -40\n")
    _patch(monkeypatch, smoke_rc=1, smoke_err=err)
    ok, reason = ffutil._probe_nvenc()
    assert ok is False
    assert "NVENC init failed" in reason
    assert "Driver does not support" in reason  # picks the informative line
    assert "-40" not in reason                   # not the generic teardown line


def test_probe_error_is_caught(monkeypatch):
    def boom(*a, **k):
        raise OSError("nvidia-smi missing")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(ffutil, "ffmpeg_bin", lambda: "ffmpeg")
    ffutil._nvenc = None
    ok, reason = ffutil._probe_nvenc()
    assert ok is False and "probe error" in reason
