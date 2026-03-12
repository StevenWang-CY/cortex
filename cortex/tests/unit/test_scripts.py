"""
Tests for Phase 15: Scripts & Developer Tools

Tests cover:
- run_dev: DevServer creation, signal handling, ServiceProcess lifecycle
- run_capture: argument parsing, statistics computation
- run_llm_server: status checking, argument parsing
- calibrate: simulation, baseline computation, save/load
- replay_session: JSONL loading, summary, replay
- seed_config: directory creation, env file, baseline, gitignore
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# ============================================================================
# run_dev tests
# ============================================================================


class TestServiceProcess:
    """Tests for ServiceProcess lifecycle."""

    def test_create_service_process(self):
        from cortex.scripts.run_dev import ServiceProcess

        proc = ServiceProcess("test-svc", lambda: None)
        assert proc.name == "test-svc"
        assert proc.process is None
        assert not proc.alive

    def test_alive_when_no_process(self):
        from cortex.scripts.run_dev import ServiceProcess

        proc = ServiceProcess("x", lambda: None)
        assert not proc.alive

    def test_stop_when_no_process(self):
        from cortex.scripts.run_dev import ServiceProcess

        proc = ServiceProcess("x", lambda: None)
        # Should not raise
        proc.stop()


class TestDevServer:
    """Tests for DevServer initialization."""

    def test_create_with_default_config(self):
        from cortex.scripts.run_dev import DevServer

        server = DevServer()
        assert server.config is not None
        assert server._processes == []

    def test_create_with_custom_config(self):
        from cortex.libs.config.settings import CortexConfig
        from cortex.scripts.run_dev import DevServer

        config = CortexConfig()
        server = DevServer(config)
        assert server.config is config


class TestRunDevMain:
    """Tests for run_dev main entry point."""

    def test_main_function_exists(self):
        from cortex.scripts.run_dev import main

        assert callable(main)

    def test_async_services_list(self):
        from cortex.scripts.run_dev import _ASYNC_SERVICES

        assert "capture_service" in _ASYNC_SERVICES
        assert "physio_engine" in _ASYNC_SERVICES
        assert "state_engine" in _ASYNC_SERVICES

    def test_run_ws_server_uses_api_config_object(self):
        from cortex.libs.config.settings import CortexConfig
        from cortex.scripts.run_dev import _run_ws_server

        config = CortexConfig()

        def _close_coro(coro):
            coro.close()

        with patch("cortex.services.api_gateway.websocket_server.WebSocketServer") as ws_cls:
            with patch(
                "cortex.scripts.run_dev.asyncio.run",
                side_effect=_close_coro,
            ) as run_mock:
                _run_ws_server(config)

        ws_cls.assert_called_once_with(config.api)
        run_mock.assert_called_once()


# ============================================================================
# run_capture tests
# ============================================================================


class TestRunCapture:
    """Tests for standalone capture script."""

    def test_parse_args_defaults(self):
        from cortex.scripts.run_capture import _parse_args

        with patch("sys.argv", ["run_capture"]):
            args = _parse_args()
        assert args.device is None
        assert args.fps is None
        assert args.no_display is False
        assert args.duration == 0

    def test_parse_args_custom(self):
        from cortex.scripts.run_capture import _parse_args

        with patch("sys.argv", [
            "run_capture", "--device", "1", "--fps", "15",
            "--no-display", "--duration", "5",
        ]):
            args = _parse_args()
        assert args.device == 1
        assert args.fps == 15
        assert args.no_display is True
        assert args.duration == 5.0

    def test_main_exists(self):
        from cortex.scripts.run_capture import main

        assert callable(main)


# ============================================================================
# run_llm_server tests
# ============================================================================


class TestRunLLMServer:
    """Tests for LLM server management script."""

    def test_parse_args_defaults(self):
        from cortex.scripts.run_llm_server import _parse_args

        with patch("sys.argv", ["run_llm_server"]):
            args = _parse_args()
        assert args.local is False

    def test_parse_args_start(self):
        from cortex.scripts.run_llm_server import _parse_args

        with patch("sys.argv", ["run_llm_server", "--start"]):
            args = _parse_args()
        assert args.start is True

    def test_parse_args_test(self):
        from cortex.scripts.run_llm_server import _parse_args

        with patch("sys.argv", ["run_llm_server", "--test"]):
            args = _parse_args()
        assert args.test is True

    def test_parse_args_local(self):
        from cortex.scripts.run_llm_server import _parse_args

        with patch("sys.argv", ["run_llm_server", "--local"]):
            args = _parse_args()
        assert args.local is True

    def test_print_status(self):
        from cortex.scripts.run_llm_server import _print_status

        info = {
            "host": "gwhiz1.cis.upenn.edu",
            "port": 8800,
            "reachable": True,
            "vllm_running": True,
            "models": ["qwen3-8b"],
        }
        # Should not raise
        _print_status(info)

    def test_print_status_local(self):
        from cortex.scripts.run_llm_server import _print_status

        info = {
            "host": "localhost",
            "port": 11434,
            "reachable": False,
            "models": [],
        }
        _print_status(info, local=True)

    @pytest.mark.asyncio
    async def test_check_local_status_unreachable(self):
        from cortex.scripts.run_llm_server import check_local_status

        # Should not raise, just report unreachable
        info = await check_local_status("localhost", 99999)
        assert info["reachable"] is False


# ============================================================================
# calibrate tests
# ============================================================================


class TestCalibrate:
    """Tests for calibration script."""

    def test_simulate_calibration(self):
        from cortex.scripts.calibrate import _simulate_calibration

        samples = _simulate_calibration(2)  # 2 seconds
        assert "hr" in samples
        assert "hrv" in samples
        assert "blink_rate" in samples
        assert "mouse_velocity" in samples
        assert "shoulder_y" in samples
        # 2 seconds * 2 Hz = 4 samples
        assert len(samples["hr"]) == 4

    def test_compute_baselines(self):
        from cortex.scripts.calibrate import compute_baselines

        samples = {
            "hr": [70.0, 72.0, 68.0, 74.0],
            "hrv": [48.0, 52.0, 50.0, 46.0],
            "blink_rate": [16.0, 18.0, 17.0, 15.0],
            "mouse_velocity": [480.0, 520.0, 500.0, 510.0],
            "mouse_variance": [9000.0, 11000.0, 10000.0, 10500.0],
            "shoulder_y": [0.48, 0.52, 0.50, 0.49],
        }

        baselines = compute_baselines(samples)
        assert baselines.hr_baseline == pytest.approx(71.0, abs=0.1)
        assert baselines.hrv_baseline == pytest.approx(49.0, abs=0.1)
        assert baselines.blink_rate_baseline == pytest.approx(16.5, abs=0.1)
        assert baselines.is_calibrated  # calibrated_at is set

    def test_compute_baselines_empty(self):
        from cortex.scripts.calibrate import compute_baselines

        samples: dict[str, list[float]] = {
            "hr": [],
            "hrv": [],
            "blink_rate": [],
            "mouse_velocity": [],
            "mouse_variance": [],
            "shoulder_y": [],
        }

        baselines = compute_baselines(samples)
        # Should use defaults
        assert baselines.hr_baseline == 72.0
        assert baselines.hrv_baseline == 50.0

    def test_save_baselines(self):
        from cortex.scripts.calibrate import compute_baselines, save_baselines

        samples = {
            "hr": [70.0, 72.0],
            "hrv": [48.0, 52.0],
            "blink_rate": [16.0, 18.0],
            "mouse_velocity": [480.0, 520.0],
            "mouse_variance": [9000.0, 11000.0],
            "shoulder_y": [0.48, 0.52],
        }
        baselines = compute_baselines(samples)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test_baseline.json"
            result = save_baselines(baselines, str(path))
            assert result.exists()

            # Verify JSON is valid
            with open(result) as f:
                data = json.load(f)
            assert data["hr_baseline"] == pytest.approx(71.0, abs=0.1)

    def test_parse_args_defaults(self):
        from cortex.scripts.calibrate import _parse_args

        with patch("sys.argv", ["calibrate"]):
            args = _parse_args()
        assert args.duration == 120
        assert args.simulate is False

    def test_parse_args_simulate(self):
        from cortex.scripts.calibrate import _parse_args

        with patch("sys.argv", ["calibrate", "--simulate", "--duration", "10"]):
            args = _parse_args()
        assert args.simulate is True
        assert args.duration == 10


# ============================================================================
# replay_session tests
# ============================================================================


class TestReplaySession:
    """Tests for session replay script."""

    def _create_session_file(self, tmpdir: str) -> str:
        """Create a sample session JSONL file."""
        path = os.path.join(tmpdir, "test_session.jsonl")
        events = [
            {"ts": 100.0, "type": "state", "data": {
                "state": "FLOW", "confidence": 0.90,
                "dwell_seconds": 5.0,
            }},
            {"ts": 101.0, "type": "features", "data": {
                "hr": 72.0, "blink_rate": 17.0,
                "mouse_velocity_mean": 500.0,
            }},
            {"ts": 102.0, "type": "transition", "data": {
                "from_state": "FLOW", "to_state": "HYPER",
                "trigger_reasons": ["elevated HR"],
            }},
            {"ts": 103.0, "type": "state", "data": {
                "state": "HYPER", "confidence": 0.88,
                "dwell_seconds": 1.0,
            }},
            {"ts": 110.0, "type": "intervention", "data": {
                "user_action": "dismissed",
                "duration_seconds": 7.0,
                "recovery_detected": False,
            }},
        ]
        with open(path, "w") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")
        return path

    def test_load_session(self):
        from cortex.scripts.replay_session import load_session

        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._create_session_file(tmpdir)
            events = load_session(path)
            assert len(events) == 5
            assert events[0]["type"] == "state"

    def test_load_session_missing_file(self):
        from cortex.scripts.replay_session import load_session

        with pytest.raises(SystemExit):
            load_session("/nonexistent/file.jsonl")

    def test_load_session_malformed_lines(self):
        from cortex.scripts.replay_session import load_session

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "bad.jsonl")
            with open(path, "w") as f:
                f.write('{"ts": 1.0, "type": "state", "data": {}}\n')
                f.write("not json\n")
                f.write('{"ts": 2.0, "type": "state", "data": {}}\n')
                f.write("# comment line\n")

            events = load_session(path)
            assert len(events) == 2  # skips malformed and comments

    def test_show_summary(self, capsys):
        from cortex.scripts.replay_session import load_session, show_summary

        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._create_session_file(tmpdir)
            events = load_session(path)
            show_summary(events)

        captured = capsys.readouterr()
        assert "Session Summary" in captured.out
        assert "Duration" in captured.out
        assert "State Distribution" in captured.out

    def test_show_summary_empty(self, capsys):
        from cortex.scripts.replay_session import show_summary

        show_summary([])
        captured = capsys.readouterr()
        assert "No events" in captured.out

    def test_replay_instant(self, capsys):
        from cortex.scripts.replay_session import load_session, replay_session

        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._create_session_file(tmpdir)
            events = load_session(path)
            replay_session(events, instant=True, color=False)

        captured = capsys.readouterr()
        assert "STATE" in captured.out
        assert "TRANS" in captured.out
        assert "FEAT" in captured.out
        assert "INTV" in captured.out
        assert "Replay complete" in captured.out

    def test_replay_with_filter(self, capsys):
        from cortex.scripts.replay_session import load_session, replay_session

        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._create_session_file(tmpdir)
            events = load_session(path)
            replay_session(
                events, instant=True, event_filter="transition", color=False
            )

        captured = capsys.readouterr()
        assert "TRANS" in captured.out
        assert "STATE" not in captured.out

    def test_replay_empty(self, capsys):
        from cortex.scripts.replay_session import replay_session

        replay_session([], instant=True)
        captured = capsys.readouterr()
        assert "No events" in captured.out

    def test_format_timestamp(self):
        from cortex.scripts.replay_session import _format_timestamp

        assert _format_timestamp(65.5, 0.0) == "01:05.50"
        assert _format_timestamp(10.0, 10.0) == "00:00.00"

    def test_parse_args(self):
        from cortex.scripts.replay_session import _parse_args

        with patch("sys.argv", ["replay", "session.jsonl", "--speed", "2.0", "--summary"]):
            args = _parse_args()
        assert args.session_file == "session.jsonl"
        assert args.speed == 2.0
        assert args.summary is True


# ============================================================================
# seed_config tests
# ============================================================================


class TestSeedConfig:
    """Tests for configuration seeder."""

    def test_create_storage_dirs(self):
        from cortex.scripts.seed_config import create_storage_dirs

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            created = create_storage_dirs(root)
            assert len(created) > 0
            assert (root / "storage" / "sessions").is_dir()
            assert (root / "storage" / "cache").is_dir()
            assert (root / "storage" / "baselines").is_dir()
            assert (root / "storage" / "logs").is_dir()
            assert (root / "storage" / "exports").is_dir()

    def test_create_storage_dirs_idempotent(self):
        from cortex.scripts.seed_config import create_storage_dirs

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            create_storage_dirs(root)
            # Second call should create nothing
            created = create_storage_dirs(root)
            assert len(created) == 0

    def test_create_storage_dirs_dry_run(self):
        from cortex.scripts.seed_config import create_storage_dirs

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            created = create_storage_dirs(root, dry_run=True)
            # Should report but not create
            assert len(created) > 0
            assert not (root / "storage" / "sessions").exists()

    def test_create_env_file(self):
        from cortex.libs.config.settings import CortexConfig
        from cortex.scripts.seed_config import create_env_file

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = CortexConfig()
            result = create_env_file(root, config)
            assert result is True
            assert (root / ".env").exists()

            content = (root / ".env").read_text()
            assert "CORTEX_LLM__MODE" in content
            assert "CORTEX_API__PORT" in content

    def test_create_env_file_skip_existing(self):
        from cortex.libs.config.settings import CortexConfig
        from cortex.scripts.seed_config import create_env_file

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".env").write_text("existing")

            config = CortexConfig()
            result = create_env_file(root, config)
            assert result is False  # skipped

    def test_create_env_file_force(self):
        from cortex.libs.config.settings import CortexConfig
        from cortex.scripts.seed_config import create_env_file

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".env").write_text("existing")

            config = CortexConfig()
            result = create_env_file(root, config, force=True)
            assert result is True

    def test_create_default_baseline(self):
        from cortex.scripts.seed_config import create_default_baseline

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = create_default_baseline(root)
            assert result is True

            baseline_path = root / "storage" / "baselines" / "default.json"
            assert baseline_path.exists()

            with open(baseline_path) as f:
                data = json.load(f)
            assert data["hr_baseline"] == 72.0

    def test_create_gitignore_new(self):
        from cortex.scripts.seed_config import create_gitignore

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = create_gitignore(root)
            assert result is True
            assert (root / ".gitignore").exists()
            content = (root / ".gitignore").read_text()
            assert "# Cortex" in content
            assert "storage/sessions/*" in content

    def test_create_gitignore_append(self):
        from cortex.scripts.seed_config import create_gitignore

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".gitignore").write_text("*.pyc\n")

            result = create_gitignore(root)
            assert result is True
            content = (root / ".gitignore").read_text()
            assert "*.pyc" in content  # original preserved
            assert "# Cortex" in content  # appended

    def test_create_gitignore_skip_if_present(self):
        from cortex.scripts.seed_config import create_gitignore

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".gitignore").write_text("# Cortex\nstorage/\n")

            result = create_gitignore(root)
            assert result is False  # already has Cortex entries

    def test_parse_args_defaults(self):
        from cortex.scripts.seed_config import _parse_args

        with patch("sys.argv", ["seed_config"]):
            args = _parse_args()
        assert args.root == "."
        assert args.force is False
        assert args.dry_run is False


# ============================================================================
# Import tests
# ============================================================================


class TestScriptImports:
    """Verify all scripts can be imported without errors."""

    def test_import_run_dev(self):
        import cortex.scripts.run_dev as run_dev

        assert run_dev is not None

    def test_import_run_capture(self):
        import cortex.scripts.run_capture as run_capture

        assert run_capture is not None

    def test_import_run_llm_server(self):
        import cortex.scripts.run_llm_server as run_llm_server

        assert run_llm_server is not None

    def test_import_calibrate(self):
        import cortex.scripts.calibrate as calibrate

        assert calibrate is not None

    def test_import_replay_session(self):
        import cortex.scripts.replay_session as replay_session

        assert replay_session is not None

    def test_import_seed_config(self):
        import cortex.scripts.seed_config as seed_config

        assert seed_config is not None
