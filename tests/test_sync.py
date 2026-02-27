"""Tests for linus.sync — training sync orchestrator."""

import threading
import time

import pytest

import linus.sync as sync


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    linus_dir = tmp_path / "linus"
    linus_dir.mkdir()
    monkeypatch.setattr(sync, "LINUS_DIR", linus_dir)
    monkeypatch.setattr(sync, "STATE_PATH", linus_dir / "training_state.json")
    return linus_dir


class TestState:
    def test_load_default_when_missing(self, state_dir):
        state = sync.load_state()
        assert state["status"] == "idle"
        assert state["adapter_version"] == 0
        assert state["last_error"] is None

    def test_save_and_load_roundtrip(self, state_dir):
        state = sync.load_state()
        state["status"] = "training"
        state["adapter_version"] = 3
        state["last_eval_metrics"] = {"score": 0.72, "type_accuracy": 0.5}
        sync.save_state(state)

        loaded = sync.load_state()
        assert loaded["status"] == "training"
        assert loaded["adapter_version"] == 3
        assert loaded["last_eval_metrics"]["score"] == 0.72

    def test_load_corrupt_file_returns_default(self, state_dir):
        sync.STATE_PATH.write_text("not json {{{")
        state = sync.load_state()
        assert state["status"] == "idle"
        assert state["adapter_version"] == 0

    def test_save_is_atomic(self, state_dir):
        """save_state uses tmp + os.replace so a crash mid-write won't corrupt."""
        sync.save_state({"status": "training", "adapter_version": 1})
        # No .tmp file should remain
        assert not sync.STATE_PATH.with_suffix(".tmp").exists()
        assert sync.STATE_PATH.exists()


class TestRunModal:
    def test_parse_single_line_json(self, monkeypatch):
        """_run_modal extracts JSON from modal stdout."""
        fake_stdout = 'Loading model...\nUploading...\n{"score": 0.75, "n": 100}\n'
        monkeypatch.setattr(
            sync.subprocess,
            "run",
            lambda *a, **kw: type(
                "R", (), {"returncode": 0, "stdout": fake_stdout, "stderr": ""}
            )(),
        )
        result = sync._run_modal("fake_target")
        assert result == {"score": 0.75, "n": 100}

    def test_parse_multiline_json(self, monkeypatch):
        """_run_modal extracts pretty-printed JSON blocks."""
        fake_stdout = 'Starting...\n{\n  "score": 0.8,\n  "type_accuracy": 0.5\n}\n'
        monkeypatch.setattr(
            sync.subprocess,
            "run",
            lambda *a, **kw: type(
                "R", (), {"returncode": 0, "stdout": fake_stdout, "stderr": ""}
            )(),
        )
        result = sync._run_modal("fake_target")
        assert result["score"] == 0.8
        assert result["type_accuracy"] == 0.5

    def test_nonzero_returncode(self, monkeypatch):
        monkeypatch.setattr(
            sync.subprocess,
            "run",
            lambda *a, **kw: type("R", (), {"returncode": 1, "stdout": "", "stderr": "error"})(),
        )
        assert sync._run_modal("fake") is None

    def test_no_json_in_output(self, monkeypatch):
        monkeypatch.setattr(
            sync.subprocess,
            "run",
            lambda *a, **kw: type(
                "R", (), {"returncode": 0, "stdout": "just text\nno json here\n", "stderr": ""}
            )(),
        )
        assert sync._run_modal("fake") is None

    def test_modal_not_found(self, monkeypatch):
        def raise_fnf(*a, **kw):
            raise FileNotFoundError("no modal")

        monkeypatch.setattr(sync.subprocess, "run", raise_fnf)
        assert sync._run_modal("fake") is None


class TestRunCycle:
    def test_no_internet_sets_error(self, state_dir, monkeypatch):
        monkeypatch.setattr(sync, "check_internet", lambda: False)
        sync.run_cycle()
        state = sync.load_state()
        assert state["status"] == "idle"
        assert state["last_error"] == "no_internet"

    def test_dataset_failure_sets_error(self, state_dir, monkeypatch):
        monkeypatch.setattr(sync, "check_internet", lambda: True)
        monkeypatch.setattr(sync, "build_dataset", lambda: None)
        sync.run_cycle()
        state = sync.load_state()
        assert state["status"] == "idle"
        assert state["last_error"] == "dataset_build_failed"

    def test_training_failure_sets_error(self, state_dir, monkeypatch):
        monkeypatch.setattr(sync, "check_internet", lambda: True)
        monkeypatch.setattr(sync, "build_dataset", lambda: {"total_examples": 100})
        monkeypatch.setattr(sync, "run_training", lambda: None)
        sync.run_cycle()
        state = sync.load_state()
        assert state["last_error"] == "training_failed"

    def test_eval_below_threshold_keeps_old_adapters(self, state_dir, monkeypatch):
        monkeypatch.setattr(sync, "check_internet", lambda: True)
        monkeypatch.setattr(sync, "build_dataset", lambda: {"total_examples": 100})
        monkeypatch.setattr(sync, "run_training", lambda: {"final_train_loss": 0.5})
        eval_result = {"score": 0.1, "type_accuracy": 0, "semantic_similarity": 0.1}
        monkeypatch.setattr(sync, "run_eval", lambda: eval_result)
        sync.run_cycle()
        state = sync.load_state()
        assert state["last_error"] == "eval_below_threshold"
        assert state["adapter_version"] == 0

    def test_full_success_increments_version(self, state_dir, monkeypatch):
        monkeypatch.setattr(sync, "check_internet", lambda: True)
        monkeypatch.setattr(sync, "build_dataset", lambda: {"total_examples": 100})
        monkeypatch.setattr(sync, "run_training", lambda: {"final_train_loss": 0.3})
        eval_result = {"score": 0.8, "type_accuracy": 0.6, "semantic_similarity": 0.9}
        monkeypatch.setattr(sync, "run_eval", lambda: eval_result)
        monkeypatch.setattr(sync, "pull_adapters", lambda: True)
        sync.run_cycle()
        state = sync.load_state()
        assert state["status"] == "idle"
        assert state["last_error"] is None
        assert state["adapter_version"] == 1
        assert state["train_count"] == 1
        assert state["last_eval_metrics"]["score"] == 0.8

    def test_recovers_from_interrupted_state(self, state_dir, monkeypatch):
        interrupted = dict(sync._DEFAULT_STATE, status="training")
        sync.save_state(interrupted)
        monkeypatch.setattr(sync, "check_internet", lambda: False)
        sync.run_cycle()
        state = sync.load_state()
        assert state["status"] == "idle"


class TestThreadAPI:
    def test_trigger_train_starts_thread(self, state_dir, monkeypatch):
        ran = threading.Event()
        monkeypatch.setattr(sync, "run_cycle", lambda: ran.set())

        # Reset global state
        sync._train_thread = None
        assert sync.trigger_train() is True
        ran.wait(timeout=5)
        assert ran.is_set()

    def test_trigger_train_rejects_duplicate(self, state_dir, monkeypatch):
        blocker = threading.Event()
        monkeypatch.setattr(sync, "run_cycle", lambda: blocker.wait(timeout=5))

        sync._train_thread = None
        assert sync.trigger_train() is True
        assert sync.trigger_train() is False
        blocker.set()

    def test_is_training_reflects_thread(self, state_dir, monkeypatch):
        blocker = threading.Event()
        monkeypatch.setattr(sync, "run_cycle", lambda: blocker.wait(timeout=5))

        sync._train_thread = None
        assert sync.is_training() is False
        sync.trigger_train()
        time.sleep(0.1)
        assert sync.is_training() is True
        blocker.set()
        time.sleep(0.5)
        assert sync.is_training() is False

    def test_schedule_starts_and_stops(self):
        sync._schedule_timer = None
        sync.start_schedule()
        assert sync._schedule_timer is not None
        sync.stop_schedule()
        assert sync._schedule_timer is None
