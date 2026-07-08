"""Auto-open launch-command construction (Feature 3). No browser is opened."""
import launcher


def test_app_mode_builds_chromeless_command():
    cmd = launcher.build_launch_command("http://127.0.0.1:7860", "app",
                                        r"C:\Edge\msedge.exe")
    assert cmd == [r"C:\Edge\msedge.exe", "--app=http://127.0.0.1:7860",
                   "--new-window"]


def test_tab_mode_returns_none_for_webbrowser_fallback():
    assert launcher.build_launch_command("http://x", "tab",
                                         r"C:\Edge\msedge.exe") is None


def test_app_mode_without_browser_falls_back():
    assert launcher.build_launch_command("http://x", "app", None) is None


def test_open_ui_disabled_is_noop():
    # auto_open false → returns immediately, spawns no thread, opens nothing
    launcher.open_ui("http://127.0.0.1:7860",
                     {"ui": {"auto_open": False}}, block=True)


if __name__ == "__main__":
    test_app_mode_builds_chromeless_command()
    test_tab_mode_returns_none_for_webbrowser_fallback()
    test_app_mode_without_browser_falls_back()
    test_open_ui_disabled_is_noop()
    print("ok")
