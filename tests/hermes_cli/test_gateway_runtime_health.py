import json

from hermes_cli.gateway import _runtime_health_lines


def test_runtime_health_lines_include_fatal_platform_and_startup_reason(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "startup_failed",
            "exit_reason": "telegram conflict",
            "platforms": {
                "telegram": {
                    "state": "fatal",
                    "error_message": "another poller is active",
                }
            },
        },
    )

    lines = _runtime_health_lines()

    assert "⚠ telegram: another poller is active" in lines
    assert "⚠ Last startup issue: telegram conflict" in lines


def test_runtime_health_lines_render_connected_platform_state(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "platforms": {
                "discord": {
                    "state": "connected",
                    "error_message": None,
                }
            },
        },
    )

    lines = _runtime_health_lines()

    assert "discord: connected" in lines


def test_runtime_health_lines_prune_removed_platform_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "gateway_state.json").write_text(json.dumps({
        "gateway_state": "running",
        "platforms": {
            "discord": {
                "state": "connected",
                "error_message": None,
            },
            "legacychat": {
                "state": "connected",
                "error_message": None,
            },
        },
    }))

    lines = _runtime_health_lines()

    assert "discord: connected" in lines
    assert "legacychat: connected" not in lines
