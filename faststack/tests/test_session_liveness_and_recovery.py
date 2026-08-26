"""Regression tests for FS-P1-004 and FS-P1-006 (session registry lifecycle).

FS-P1-004 -- the Windows "boot id" was ``int(time.time() - GetTickCount64()/1000)``,
which mixes the *adjustable* wall clock with monotonic uptime. Any clock
correction changed it without a reboot, so a running sibling's stored id no
longer matched, ``scan_stale()`` stopped recognising it as live, and that live
instance's session file was offered as a crash survivor and then deleted.
Liveness on Windows is now (PID + process creation time), which is immune to
clock changes and to PID reuse.

FS-P1-006 -- ``respawn_for_directory()`` swallowed ``Popen`` failures, and
``main()`` pruned every stale record regardless, so a folder the user selected
could end up neither reopened nor preserved for another attempt.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from faststack.io import session as session_mod
from faststack.io.session import SessionRegistry, respawn_for_directory

# --------------------------------------------------------------------------
# FS-P1-004: liveness identity
# --------------------------------------------------------------------------


@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setattr(session_mod, "_sessions_dir", lambda: directory)
    return directory


def _write_record(sessions_dir, folder, **overrides):
    payload = {
        "dir": str(folder),
        "index": 0,
        "path": None,
        "grid": True,
        "pid": 4321,
        "boot_id": "",
        "proc_id": "win:130000000000000000",
        "updated": 1000.0,
    }
    payload.update(overrides)
    name = overrides.pop("_name", f"{abs(hash(str(payload))) % 10**8}.json")
    path = sessions_dir / name
    path.write_text(json.dumps(payload))
    return path


def test_live_windows_sibling_survives_a_wall_clock_change(sessions_dir, tmp_path):
    """The exact process is still running, so its session must not be touched."""
    folder = tmp_path / "photos"
    folder.mkdir()
    record = _write_record(sessions_dir, folder, proc_id="win:130000000000000000")

    with (
        patch.object(session_mod, "_current_boot_id", return_value=""),
        patch.object(session_mod, "_pid_alive", return_value=True),
        # Same creation time despite an arbitrary wall-clock jump.
        patch.object(
            session_mod, "_process_identity", return_value="win:130000000000000000"
        ),
    ):
        records, stale_paths = SessionRegistry.scan_stale()

    assert records == []
    assert stale_paths == []
    assert record.exists(), "a live instance's session file was deleted"


def test_pid_reuse_by_a_different_process_is_stale(sessions_dir, tmp_path):
    folder = tmp_path / "photos"
    folder.mkdir()
    record = _write_record(sessions_dir, folder, proc_id="win:130000000000000000")

    with (
        patch.object(session_mod, "_current_boot_id", return_value=""),
        patch.object(session_mod, "_pid_alive", return_value=True),
        # PID exists, but it is a *different* process now.
        patch.object(
            session_mod, "_process_identity", return_value="win:999999999999999999"
        ),
    ):
        records, stale_paths = SessionRegistry.scan_stale()

    assert [r["dir"] for r in records] == [str(folder.resolve())]
    assert [Path(p) for p in stale_paths] == [record]


def test_dead_process_is_stale(sessions_dir, tmp_path):
    folder = tmp_path / "photos"
    folder.mkdir()
    record = _write_record(sessions_dir, folder)

    with (
        patch.object(session_mod, "_current_boot_id", return_value=""),
        patch.object(session_mod, "_pid_alive", return_value=False),
    ):
        records, stale_paths = SessionRegistry.scan_stale()

    assert len(records) == 1
    assert [Path(p) for p in stale_paths] == [record]


def test_legacy_windows_record_without_proc_id_is_handled_safely(
    sessions_dir, tmp_path
):
    """Old files have no proc_id (and a bogus derived boot id). Never crash."""
    folder = tmp_path / "photos"
    folder.mkdir()
    legacy = _write_record(sessions_dir, folder, boot_id="boot:1700000000")
    data = json.loads(legacy.read_text())
    del data["proc_id"]
    legacy.write_text(json.dumps(data))

    with (
        patch.object(session_mod, "_current_boot_id", return_value=""),
        patch.object(session_mod, "_pid_alive", return_value=True),
        patch.object(session_mod, "_process_identity", return_value="win:1"),
    ):
        records, stale_paths = SessionRegistry.scan_stale()

    # No identity to compare and no boot id: fall back to the PID check, which
    # says live. Erring toward "live" only costs a recovery offer; erring the
    # other way deletes a running instance's session.
    assert records == []
    assert stale_paths == []
    assert legacy.exists()


def test_malformed_proc_id_does_not_invalidate_the_record(sessions_dir, tmp_path):
    folder = tmp_path / "photos"
    folder.mkdir()
    bad = _write_record(sessions_dir, folder)
    data = json.loads(bad.read_text())
    data["proc_id"] = {"not": "a string"}
    bad.write_text(json.dumps(data))

    with (
        patch.object(session_mod, "_current_boot_id", return_value=""),
        patch.object(session_mod, "_pid_alive", return_value=False),
    ):
        records, stale_paths = SessionRegistry.scan_stale()

    # Coerced to "", record still usable, and correctly stale (dead PID).
    assert len(records) == 1
    assert [Path(p) for p in stale_paths] == [bad]


def test_linux_boot_id_behaviour_is_unchanged(sessions_dir, tmp_path):
    folder = tmp_path / "photos"
    folder.mkdir()
    same_boot = _write_record(
        sessions_dir, folder, boot_id="boot-uuid-A", proc_id="", _name="a.json"
    )
    other = tmp_path / "other"
    other.mkdir()
    old_boot = _write_record(
        sessions_dir, other, boot_id="boot-uuid-B", proc_id="", _name="b.json"
    )

    with (
        patch.object(session_mod, "_current_boot_id", return_value="boot-uuid-A"),
        patch.object(session_mod, "_pid_alive", return_value=True),
    ):
        records, stale_paths = SessionRegistry.scan_stale()

    # Same boot + live PID -> live sibling; different boot -> reboot survivor.
    assert [r["dir"] for r in records] == [str(other.resolve())]
    assert [Path(p) for p in stale_paths] == [old_boot]
    assert same_boot.exists()


def test_linux_boot_id_is_still_read_from_proc(monkeypatch):
    """The Linux path is untouched by the Windows identity work."""
    if not Path("/proc/sys/kernel/random/boot_id").exists():
        pytest.skip("no /proc boot_id on this platform")
    assert session_mod._current_boot_id()


def test_update_records_the_process_identity(sessions_dir, tmp_path, monkeypatch):
    folder = tmp_path / "photos"
    folder.mkdir()
    monkeypatch.setattr(session_mod, "_process_identity", lambda *a: "win:42")
    registry = SessionRegistry()
    registry.update(folder, 3, True, folder / "x.jpg")

    data = json.loads(registry.path.read_text())
    assert data["proc_id"] == "win:42"
    assert data["pid"] > 0


def test_windows_boot_id_no_longer_mixes_the_wall_clock(monkeypatch):
    """Simulate Windows and prove the clock-derived boot id is gone.

    Pre-fix this returned ``f"boot:{int(time.time() - ticks/1000)}"``, so two
    calls straddling a wall-clock correction disagreed even though no reboot
    had happened. Post-fix Windows has no boot id and liveness comes from
    ``_process_identity`` instead.
    """

    class FakeKernel32:
        @staticmethod
        def GetTickCount64():
            return 60_000  # one minute of uptime

    class FakeWindll:
        kernel32 = FakeKernel32()

    # No /proc boot_id, and ctypes looks like Windows.
    monkeypatch.setattr(session_mod.Path, "exists", lambda self: False)
    monkeypatch.setattr(session_mod.ctypes, "windll", FakeWindll(), raising=False)

    before = session_mod._current_boot_id()
    monkeypatch.setattr(session_mod.time, "time", lambda: 1_800_000_000.0)
    after = session_mod._current_boot_id()

    assert (
        before == "" and after == ""
    ), "Windows must not derive a boot id from the wall clock"
    assert before == after


# --------------------------------------------------------------------------
# FS-P1-006: a failed respawn must not be pruned
# --------------------------------------------------------------------------


def test_respawn_reports_success_and_failure():
    with patch("subprocess.Popen") as popen:
        assert respawn_for_directory("/tmp/x", True) is True
        assert popen.called

    with patch("subprocess.Popen", side_effect=OSError("no exec")):
        assert respawn_for_directory("/tmp/x", True) is False

    assert respawn_for_directory("", True) is False


def _prune_plan(chosen, stale_paths, spawn_results):
    """Re-run main()'s recovery pruning decision with a stubbed respawn.

    Mirrors the block in ``faststack.app.main`` verbatim so the invariant is
    testable without launching a Qt application.
    """
    unconsumed_paths: set = set()
    for other_record in chosen[1:]:
        if not spawn_results.pop(0):
            record_path = other_record.get("_path")
            if record_path:
                unconsumed_paths.add(str(record_path))
    return [p for p in stale_paths if str(p) not in unconsumed_paths]


def test_failed_secondary_spawn_keeps_its_session_file(tmp_path):
    dir_a = tmp_path / "A"
    dir_b = tmp_path / "B"
    dir_a.mkdir()
    dir_b.mkdir()
    rec_a_path = tmp_path / "a.json"
    rec_b_path = tmp_path / "b.json"
    orphan = tmp_path / "orphan.json"
    for p in (rec_a_path, rec_b_path, orphan):
        p.write_text("{}")

    rec_a = {"dir": str(dir_a), "_path": str(rec_a_path)}
    rec_b = {"dir": str(dir_b), "_path": str(rec_b_path)}
    stale_paths = [rec_a_path, rec_b_path, orphan]

    # A opens in-process; B's respawn fails.
    prunable = _prune_plan([rec_a, rec_b], stale_paths, spawn_results=[False])

    assert rec_b_path not in prunable, "a failed respawn must keep its record"
    assert rec_a_path in prunable, "the folder reopened in-process is consumed"
    assert orphan in prunable, "unrelated orphan files stay prunable"

    SessionRegistry.prune(prunable)
    assert rec_b_path.exists()
    assert not rec_a_path.exists()
    assert not orphan.exists()


def test_successful_secondary_spawn_consumes_its_record(tmp_path):
    rec_a_path = tmp_path / "a.json"
    rec_b_path = tmp_path / "b.json"
    for p in (rec_a_path, rec_b_path):
        p.write_text("{}")

    rec_a = {"dir": "A", "_path": str(rec_a_path)}
    rec_b = {"dir": "B", "_path": str(rec_b_path)}

    prunable = _prune_plan(
        [rec_a, rec_b], [rec_a_path, rec_b_path], spawn_results=[True]
    )

    assert prunable == [rec_a_path, rec_b_path]
    SessionRegistry.prune(prunable)
    assert not rec_a_path.exists()
    assert not rec_b_path.exists()


def test_main_recovery_block_matches_the_tested_prune_plan():
    """Guard the duplicated logic above against drifting from main()."""
    import inspect

    from faststack import app

    source = inspect.getsource(app.main)
    assert "unconsumed_paths" in source
    assert "spawned = respawn_for_directory(" in source
    assert "if not spawned:" in source
    assert 'record_path = other_record.get("_path")' in source
    assert (
        "prunable = [p for p in stale_paths if str(p) not in unconsumed_paths]"
        in source
    )
