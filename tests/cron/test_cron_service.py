import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from nanobot.cron import service as cron_service_module
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronPayload, CronSchedule


def test_add_job_rejects_unknown_timezone(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    with pytest.raises(ValueError, match="unknown timezone 'America/Vancovuer'"):
        service.add_job(
            name="tz typo",
            schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="America/Vancovuer"),
            message="hello",
        )

    assert service.list_jobs(include_disabled=True) == []


def test_add_job_accepts_valid_timezone(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    job = service.add_job(
        name="tz ok",
        schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="America/Vancouver"),
        message="hello",
    )

    assert job.schedule.tz == "America/Vancouver"
    assert job.state.next_run_at_ms is not None


def test_add_job_persists_send_progress_flag(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    service = CronService(store_path)

    job = service.add_job(
        name="quiet share",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="hello",
        send_progress=False,
    )

    assert job.payload.send_progress is False
    raw = json.loads(store_path.read_text())
    assert raw["jobs"][0]["payload"]["sendProgress"] is False

    fresh = CronService(store_path)
    loaded = fresh.get_job(job.id)
    assert loaded is not None
    assert loaded.payload.send_progress is False


def test_add_job_persists_weixin_mirror_flag(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    service = CronService(store_path)

    job = service.add_job(
        name="dual share",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="hello",
        mirror_weixin_allowfrom=True,
    )

    assert job.payload.mirror_weixin_allowfrom is True
    raw = json.loads(store_path.read_text())
    assert raw["jobs"][0]["payload"]["mirrorWeixinAllowFrom"] is True

    fresh = CronService(store_path)
    loaded = fresh.get_job(job.id)
    assert loaded is not None
    assert loaded.payload.mirror_weixin_allowfrom is True


def test_add_job_accepts_daily_random_schedule(tmp_path, monkeypatch) -> None:
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 3, 26, 7, 30, tzinfo=tz)
    monkeypatch.setattr(cron_service_module, "_now_ms", lambda: int(now.timestamp() * 1000))
    monkeypatch.setattr(cron_service_module, "_pick_random_minute", lambda _: 0)

    service = CronService(tmp_path / "cron" / "jobs.json")
    job = service.add_job(
        name="rand ok",
        schedule=CronSchedule(
            kind="daily_random",
            window_start="08:00",
            window_end="20:00",
            tz="Asia/Shanghai",
        ),
        message="hello",
    )

    next_dt = datetime.fromtimestamp(job.state.next_run_at_ms / 1000, tz=tz)
    assert next_dt.hour == 8
    assert next_dt.minute == 0


@pytest.mark.asyncio
async def test_daily_random_start_preserves_existing_next_run(tmp_path, monkeypatch) -> None:
    tz = ZoneInfo("Asia/Shanghai")
    initial_now = datetime(2026, 3, 26, 7, 30, tzinfo=tz)
    monkeypatch.setattr(cron_service_module, "_now_ms", lambda: int(initial_now.timestamp() * 1000))
    monkeypatch.setattr(cron_service_module, "_pick_random_minute", lambda _: 0)

    service = CronService(tmp_path / "cron" / "jobs.json")
    job = service.add_job(
        name="rand",
        schedule=CronSchedule(
            kind="daily_random",
            window_start="08:00",
            window_end="20:00",
            tz="Asia/Shanghai",
        ),
        message="hello",
    )
    first_next = job.state.next_run_at_ms

    monkeypatch.setattr(cron_service_module, "_pick_random_minute", lambda max_offset: max_offset)
    fresh = CronService(tmp_path / "cron" / "jobs.json", on_job=lambda _: asyncio.sleep(0))
    await fresh.start()
    try:
        loaded = fresh.get_job(job.id)
        assert loaded is not None
        assert loaded.state.next_run_at_ms == first_next
        next_dt = datetime.fromtimestamp(loaded.state.next_run_at_ms / 1000, tz=tz)
        assert next_dt.hour == 8
        assert next_dt.minute == 0
    finally:
        fresh.stop()


@pytest.mark.asyncio
async def test_daily_random_reschedules_for_next_day_after_run(tmp_path, monkeypatch) -> None:
    tz = ZoneInfo("Asia/Shanghai")
    times = iter([
        int(datetime(2026, 3, 26, 7, 0, tzinfo=tz).timestamp() * 1000),
        int(datetime(2026, 3, 26, 10, 0, tzinfo=tz).timestamp() * 1000),
        int(datetime(2026, 3, 26, 10, 0, tzinfo=tz).timestamp() * 1000),
        int(datetime(2026, 3, 26, 10, 0, tzinfo=tz).timestamp() * 1000),
    ])
    monkeypatch.setattr(cron_service_module, "_now_ms", lambda: next(times))
    monkeypatch.setattr(cron_service_module, "_pick_random_minute", lambda _: 0)

    service = CronService(tmp_path / "cron" / "jobs.json", on_job=lambda _: asyncio.sleep(0))
    job = service.add_job(
        name="rand",
        schedule=CronSchedule(
            kind="daily_random",
            window_start="08:00",
            window_end="20:00",
            tz="Asia/Shanghai",
        ),
        message="hello",
    )

    await service.run_job(job.id)

    loaded = service.get_job(job.id)
    next_dt = datetime.fromtimestamp(loaded.state.next_run_at_ms / 1000, tz=tz)
    assert next_dt.date().isoformat() == "2026-03-27"
    assert next_dt.hour == 8
    assert next_dt.minute == 0


@pytest.mark.asyncio
async def test_execute_job_records_run_history(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    service = CronService(store_path, on_job=lambda _: asyncio.sleep(0))
    job = service.add_job(
        name="hist",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="hello",
    )
    await service.run_job(job.id)

    loaded = service.get_job(job.id)
    assert loaded is not None
    assert len(loaded.state.run_history) == 1
    rec = loaded.state.run_history[0]
    assert rec.status == "ok"
    assert rec.duration_ms >= 0
    assert rec.error is None


@pytest.mark.asyncio
async def test_run_history_records_errors(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"

    async def fail(_):
        raise RuntimeError("boom")

    service = CronService(store_path, on_job=fail)
    job = service.add_job(
        name="fail",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="hello",
    )
    await service.run_job(job.id)

    loaded = service.get_job(job.id)
    assert len(loaded.state.run_history) == 1
    assert loaded.state.run_history[0].status == "error"
    assert loaded.state.run_history[0].error == "boom"


@pytest.mark.asyncio
async def test_run_history_trimmed_to_max(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    service = CronService(store_path, on_job=lambda _: asyncio.sleep(0))
    job = service.add_job(
        name="trim",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="hello",
    )
    for _ in range(25):
        await service.run_job(job.id)

    loaded = service.get_job(job.id)
    assert len(loaded.state.run_history) == CronService._MAX_RUN_HISTORY


@pytest.mark.asyncio
async def test_run_history_persisted_to_disk(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    service = CronService(store_path, on_job=lambda _: asyncio.sleep(0))
    job = service.add_job(
        name="persist",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="hello",
    )
    await service.run_job(job.id)

    raw = json.loads(store_path.read_text())
    history = raw["jobs"][0]["state"]["runHistory"]
    assert len(history) == 1
    assert history[0]["status"] == "ok"
    assert "runAtMs" in history[0]
    assert "durationMs" in history[0]

    fresh = CronService(store_path)
    loaded = fresh.get_job(job.id)
    assert len(loaded.state.run_history) == 1
    assert loaded.state.run_history[0].status == "ok"


@pytest.mark.asyncio
async def test_running_service_honors_external_disable(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    called: list[str] = []

    async def on_job(job) -> None:
        called.append(job.id)

    service = CronService(store_path, on_job=on_job)
    job = service.add_job(
        name="external-disable",
        schedule=CronSchedule(kind="every", every_ms=200),
        message="hello",
    )
    await service.start()
    try:
        # Wait slightly to ensure file mtime is definitively different
        await asyncio.sleep(0.05)
        external = CronService(store_path)
        updated = external.enable_job(job.id, enabled=False)
        assert updated is not None
        assert updated.enabled is False

        await asyncio.sleep(0.35)
        assert called == []
    finally:
        service.stop()


def test_remove_job_refuses_system_jobs(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    service.register_system_job(CronJob(
        id="dream",
        name="dream",
        schedule=CronSchedule(kind="cron", expr="0 */2 * * *", tz="UTC"),
        payload=CronPayload(kind="system_event"),
    ))

    result = service.remove_job("dream")

    assert result == "protected"
    assert service.get_job("dream") is not None
