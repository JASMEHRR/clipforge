"""ffutil.run_ffmpeg argument construction — no real ffmpeg invoked."""
import subprocess

import ffutil


class _FakeProc:
    def __init__(self):
        self.stdout = iter(())
        self.stderr = subprocess.PIPE
        self.returncode = 0

    def wait(self, timeout=None):
        return 0


def test_run_ffmpeg_with_progress_builds_well_formed_command(monkeypatch):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ffutil, "ffmpeg_bin", lambda: "ffmpeg")

    ffutil.run_ffmpeg(["-i", "in.mp4", "out.mp4"], progress_label="test")

    cmd = captured["cmd"]
    assert cmd == ["ffmpeg", "-y", "-v", "error", "-stats_period", "30",
                   "-progress", "pipe:1", "-i", "in.mp4", "out.mp4"]
    # -v must keep its own value, not swallow a later flag.
    assert cmd[cmd.index("-v") + 1] == "error"
    assert cmd[cmd.index("-stats_period") + 1] == "30"
