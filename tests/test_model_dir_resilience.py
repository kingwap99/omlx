# SPDX-License-Identifier: Apache-2.0
"""Model directories on stale mounts must not stall or break startup."""

import time
from pathlib import Path

from omlx.model_discovery import model_directory_access_error
from omlx.settings import GlobalSettings
from omlx.utils import path_probe


def setup_function():
    path_probe.forget_dir()


def test_probe_reports_available_dir(tmp_path):
    assert path_probe.probe_dir(tmp_path) == (True, None)


def test_probe_reports_missing_dir(tmp_path):
    available, error = path_probe.probe_dir(tmp_path / "missing")
    assert available is False
    assert error == "does not exist"


def test_probe_bounds_a_hung_mount(tmp_path, monkeypatch):
    real_exists = Path.exists

    def hung_exists(self, *args, **kwargs):
        if self == tmp_path:
            # What a dead NFS mount does: the stat blocks, then raises.
            time.sleep(0.5)
            raise TimeoutError(60, "Operation timed out")
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", hung_exists)

    started = time.monotonic()
    available, error = path_probe.probe_dir(tmp_path, timeout=0.05)
    elapsed = time.monotonic() - started

    assert available is False
    assert "timed out" in (error or "")
    assert elapsed < 0.4


def test_probe_caches_a_failure(tmp_path, monkeypatch):
    calls = {"count": 0}
    real_exists = Path.exists

    def counting_exists(self, *args, **kwargs):
        if self == tmp_path:
            calls["count"] += 1
            raise TimeoutError(60, "Operation timed out")
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", counting_exists)

    assert path_probe.probe_dir(tmp_path, timeout=0.2)[0] is False
    assert path_probe.probe_dir(tmp_path, timeout=0.2)[0] is False
    assert calls["count"] == 1


def test_ensure_directories_skips_an_unreachable_model_dir(tmp_path, monkeypatch):
    online = tmp_path / "models"
    offline = tmp_path / "nas-models"
    settings = GlobalSettings(base_path=tmp_path / "omlx")
    settings.model.model_dirs = [str(online), str(offline)]

    real_probe = path_probe.probe_dir

    def fake_probe(path, **kwargs):
        if Path(path) == offline:
            return False, "timed out after 1.5s"
        return real_probe(path, **kwargs)

    monkeypatch.setattr(path_probe, "probe_dir", fake_probe)

    settings.ensure_directories()

    assert online.exists()
    assert not offline.exists()
    # The offline dir stays configured so it is picked up after reconnecting.
    assert str(offline.resolve()) in settings.model.model_dirs


def test_access_error_mentions_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(
        path_probe, "probe_dir", lambda path, **kwargs: (False, "timed out after 1.5s")
    )

    error = model_directory_access_error(tmp_path)
    assert "not readable" in error
    assert "timed out" in error
