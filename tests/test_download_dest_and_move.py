# SPDX-License-Identifier: Apache-2.0
"""Tests for per-download destination directories and model moves."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

import omlx.admin.routes as admin_routes
from omlx.admin.hf_downloader import DownloadStatus, DownloadTask, HFDownloader
from omlx.admin.ms_downloader import MSDownloader
from omlx.admin.routes import (
    ModelMoveRequest,
    _find_model_folder,
    _move_tree_with_progress,
    move_hf_model,
)
from omlx.settings import ModelSettings

# ---------------------------------------------------------------------------
# Destination on the task
# ---------------------------------------------------------------------------


def test_dest_dir_round_trips_through_serialization():
    task = DownloadTask(task_id="t1", repo_id="owner/model", dest_dir="/vol/extra")
    row = task.to_dict()
    assert row["dest_dir"] == "/vol/extra"
    assert DownloadTask.from_dict(row).dest_dir == "/vol/extra"


def test_rows_without_dest_dir_read_as_primary():
    legacy = DownloadTask(task_id="t1", repo_id="owner/model").to_dict()
    legacy.pop("dest_dir")
    assert DownloadTask.from_dict(legacy).dest_dir == ""


@pytest.fixture(params=[HFDownloader, MSDownloader], ids=["hf", "ms"])
def downloader(request, tmp_path):
    cls = request.param
    model_dir = tmp_path / "models"
    model_dir.mkdir()

    async def _run(self, task_id, token):
        pass

    with patch.object(cls, "_run_download", new=_run), patch(
        "omlx.admin.ms_downloader.MS_SDK_AVAILABLE", True
    ):
        yield cls(model_dir=str(model_dir), tasks_file=None)


async def test_repo_dir_follows_the_snapshot(downloader, tmp_path):
    other = tmp_path / "extra"
    task = await downloader.start_download("owner/model", dest_dir=str(other))
    assert task.dest_dir == str(other)
    assert downloader.repo_dir(task.repo_id, task.dest_dir) == other / "owner/model"
    assert downloader.repo_dir(task.repo_id) == downloader.model_dir / "owner/model"


async def test_retry_keeps_the_original_destination(downloader, tmp_path):
    other = tmp_path / "extra"
    task = await downloader.start_download("owner/model", dest_dir=str(other))
    downloader._tasks[task.task_id].status = DownloadStatus.FAILED
    # The primary directory moves on; the retry stays where it started.
    downloader.update_model_dir(str(tmp_path / "nowhere"))
    retry = await downloader.retry_download(task.task_id)
    assert retry.dest_dir == str(other)
    assert retry.repo_id == "owner/model"


async def test_restore_resumes_with_the_persisted_destination(tmp_path):
    tasks_file = tmp_path / "queue.json"
    other = tmp_path / "extra"
    tasks_file.write_text(
        json.dumps(
            [
                {
                    "task_id": "t1",
                    "repo_id": "owner/model",
                    "status": "downloading",
                    "dest_dir": str(other),
                }
            ]
        ),
        encoding="utf-8",
    )

    started: dict = {}

    async def _run(self, task_id, token):
        started["task"] = self._tasks[task_id]

    with patch.object(HFDownloader, "_run_download", new=_run):
        downloader = HFDownloader(
            model_dir=str(tmp_path / "models"), tasks_file=tasks_file
        )
        await downloader.restore_tasks()
        # The resumed download runs as a background task.
        await asyncio.gather(*downloader._active_tasks.values())
    assert started["task"].dest_dir == str(other)


# ---------------------------------------------------------------------------
# Destination validation at the API edge
# ---------------------------------------------------------------------------


def _settings_with_dirs(tmp_path, *dirs):
    base = tmp_path / "base"
    model = ModelSettings(model_dirs=[str(d) for d in dirs])
    return SimpleNamespace(
        base_path=base,
        model=model,
        get_effective_model_dirs=lambda: [Path(d).resolve() for d in dirs],
    )


def test_resolve_download_dest(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    settings = _settings_with_dirs(tmp_path, allowed)
    with patch.object(admin_routes, "_get_global_settings", lambda: settings):
        assert admin_routes._resolve_download_dest("") == ""
        assert admin_routes._resolve_download_dest(str(allowed)) == str(
            allowed.resolve()
        )
        with pytest.raises(HTTPException) as exc:
            admin_routes._resolve_download_dest(str(outside))
        assert exc.value.status_code == 400
        # A subdirectory is outside the listing scan, so it fails too.
        with pytest.raises(HTTPException):
            admin_routes._resolve_download_dest(str(allowed / "nested"))
        # A configured directory that is gone (unmounted volume) is refused.
        missing = tmp_path / "missing"
        settings = _settings_with_dirs(tmp_path, missing)
        with patch.object(admin_routes, "_get_global_settings", lambda: settings):
            with pytest.raises(HTTPException) as exc:
                admin_routes._resolve_download_dest(str(missing))
            assert exc.value.status_code == 400


async def test_update_follows_an_existing_secondary_directory(tmp_path):
    primary = tmp_path / "models"
    extra = tmp_path / "extra"
    primary.mkdir()
    (extra / "owner" / "model" / ".cache" / "huggingface" / "download").mkdir(
        parents=True
    )
    (extra / "owner" / "model" / ".cache" / "huggingface" / "download" / "a.metadata").write_text("{}")

    async def _run(self, task_id, token):
        pass

    with patch.object(HFDownloader, "_run_download", new=_run):
        downloader = HFDownloader(
            model_dir=str(primary),
            model_dirs=[str(primary), str(extra)],
        )
        task = await downloader.start_download("owner/model")
    assert task.dest_dir == str(extra)
    assert downloader.destination_dirs() == [primary, extra]


# ---------------------------------------------------------------------------
# Moving models between directories
# ---------------------------------------------------------------------------


def _make_checkpoint(root: Path, org: str, name: str) -> Path:
    folder = root / org / name
    folder.mkdir(parents=True)
    (folder / "config.json").write_text('{"model_type": "llama"}')
    (folder / "model.safetensors").write_bytes(b"weights" * 10)
    return folder


async def _wait_for_move(move_id: str, timeout: float = 5.0) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        state = admin_routes._model_move_tasks[move_id]
        if state["status"] in ("done", "error"):
            return state
        await asyncio.sleep(0.01)
    raise AssertionError(f"move {move_id} did not finish: {state['status']}")


async def test_move_renames_within_a_volume(tmp_path):
    src_root = tmp_path / "primary"
    dest_root = tmp_path / "extra"
    src_root.mkdir()
    dest_root.mkdir()
    folder = _make_checkpoint(src_root, "owner", "model")
    settings = _settings_with_dirs(tmp_path, src_root, dest_root)

    with patch.object(admin_routes, "_get_global_settings", lambda: settings), \
         patch.object(admin_routes, "_get_engine_pool", lambda: None), \
         patch.object(admin_routes, "_hf_downloader", None):
        result = await move_hf_model(
            ModelMoveRequest(name="owner/model", dest_dir=str(dest_root)),
            is_admin=True,
        )
        state = await _wait_for_move(result["move"]["move_id"])

    assert state["status"] == "done", state
    assert not folder.exists()
    # Org layout is preserved at the destination.
    assert (dest_root / "owner" / "model" / "config.json").is_file()
    assert (dest_root / "owner" / "model" / "model.safetensors").is_file()
    # The emptied org folder at the source is tidied away.
    assert not (src_root / "owner").exists()


async def test_move_rejects_bad_requests(tmp_path):
    src_root = tmp_path / "primary"
    dest_root = tmp_path / "extra"
    src_root.mkdir()
    dest_root.mkdir()
    _make_checkpoint(src_root, "owner", "model")
    (dest_root / "owner" / "model").mkdir(parents=True)
    outside = tmp_path / "outside"
    settings = _settings_with_dirs(tmp_path, src_root, dest_root)

    with patch.object(admin_routes, "_get_global_settings", lambda: settings), \
         patch.object(admin_routes, "_get_engine_pool", lambda: None):
        with pytest.raises(HTTPException) as exc:
            await move_hf_model(
                ModelMoveRequest(name="missing/model", dest_dir=str(dest_root)),
                is_admin=True,
            )
        assert exc.value.status_code == 404

        with pytest.raises(HTTPException) as exc:
            await move_hf_model(
                ModelMoveRequest(name="owner/model", dest_dir=str(outside)),
                is_admin=True,
            )
        assert exc.value.status_code == 400

        with pytest.raises(HTTPException) as exc:
            await move_hf_model(
                ModelMoveRequest(name="owner/model", dest_dir=str(src_root)),
                is_admin=True,
            )
        assert exc.value.status_code == 400  # already there

        with pytest.raises(HTTPException) as exc:
            await move_hf_model(
                ModelMoveRequest(name="other/model", dest_dir=str(dest_root)),
                is_admin=True,
            )
        assert exc.value.status_code == 404


def test_find_model_folder_flat_layout(tmp_path):
    root = tmp_path / "models"
    _make_checkpoint(root, "org", "model")
    path, parent, kind = _find_model_folder("org/model", [root])
    assert path == root / "org" / "model"
    assert parent == root
    assert kind == "dir"
    assert _find_model_folder("nope/model", [root]) == (None, None, "")


def test_move_tree_with_progress_copies_files(tmp_path):
    src = tmp_path / "src"
    _make_checkpoint(src, "owner", "model")
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    state = {"total_bytes": 0, "moved_bytes": 0}
    _move_tree_with_progress(src, tmp, state)
    assert state["total_bytes"] > 0
    assert state["moved_bytes"] == state["total_bytes"]
    assert (tmp / "owner" / "model" / "config.json").is_file()


async def test_model_dirs_endpoint_lists_space(tmp_path):
    src_root = tmp_path / "primary"
    dest_root = tmp_path / "extra"
    src_root.mkdir()
    # dest_root intentionally missing: reported as not mounted, free 0.
    settings = _settings_with_dirs(tmp_path, src_root, dest_root)

    with patch.object(admin_routes, "_get_global_settings", lambda: settings):
        result = await admin_routes.list_model_dirs(is_admin=True)

    dirs = result["dirs"]
    assert [d["index"] for d in dirs] == [0, 1]
    assert dirs[0]["path"] == str(src_root.resolve())
    assert dirs[0]["exists"] is True
    assert dirs[0]["free_bytes"] > 0
    assert dirs[1]["exists"] is False
    assert dirs[1]["free_bytes"] == 0


async def test_move_rejects_unmounted_destination(tmp_path):
    src_root = tmp_path / "primary"
    src_root.mkdir()
    _make_checkpoint(src_root, "owner", "model")
    missing = tmp_path / "gone"
    settings = _settings_with_dirs(tmp_path, src_root, missing)

    with patch.object(admin_routes, "_get_global_settings", lambda: settings), \
         patch.object(admin_routes, "_get_engine_pool", lambda: None), \
         patch.object(admin_routes, "_hf_downloader", None), \
         pytest.raises(HTTPException) as exc:
        await move_hf_model(
            ModelMoveRequest(name="owner/model", dest_dir=str(missing)),
            is_admin=True,
        )
    assert exc.value.status_code == 400
    assert "not mounted" in str(exc.value.detail).lower()
