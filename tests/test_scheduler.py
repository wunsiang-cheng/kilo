import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from fakes import text_turn
from kilo.config import settings
from kilo.context import current_session_id
from kilo.models.schedule import Run, RunKind, RunStatus, ScheduleSpec, ScheduleUpdate
from kilo.scheduling.launcher import INTERRUPTED
from kilo.scheduling.scheduler import ScheduleError, ScheduleNotFound, next_run

def spec(**fields) -> ScheduleSpec:
    return ScheduleSpec(**{"name": "n", "instructions": "do it", **fields})


async def add(kilo, **fields):
    """A schedule the agent proposed and the user approved."""
    created = await kilo.scheduler.create(spec(**fields))
    return await kilo.scheduler.approve(created.id)


async def test_missed_within_catchup_fires(kilo, model, clock):
    model.turns.append(text_turn("ok"))
    created = await add(kilo, run_in_minutes=1)
    clock.advance(minutes=6)  # 5 minutes late: still inside KILO_JOB_CATCHUP_MINUTES
    [run] = await kilo.scheduler.tick()
    await kilo.launcher.wait_idle()
    assert (await kilo.schedule_store.get_run(run.id)).status == RunStatus.SUCCEEDED
    assert await kilo.scheduler.get(created.id) is None  # a one-off runs once, then is deleted


async def test_missed_beyond_catchup_is_skipped(kilo, model, clock):
    await add(kilo, run_in_minutes=1)
    clock.advance(minutes=7)
    [run] = await kilo.scheduler.tick()
    assert run.status == RunStatus.SKIPPED and "Missed" in run.summary
    assert model.requests == []


async def test_recurring_schedule_missed_many_times_records_one_skip(kilo, model, clock):
    created = await add(kilo, cron="0 * * * *")
    clock.advance(days=2, minutes=30)  # no slot inside the catch-up window
    runs = await kilo.scheduler.tick()
    assert [r.status for r in runs] == [RunStatus.SKIPPED]
    assert model.requests == []
    assert (await kilo.scheduler.get(created.id)).next_run_at > clock()


async def test_latest_slot_inside_catchup_fires_after_long_downtime(kilo, model, clock):
    model.turns.append(text_turn("ok"))
    await add(kilo, cron="0 10 * * *", timezone="UTC")
    clock.now = datetime(2026, 10, 30, 10, 2, tzinfo=timezone.utc)  # two days down, back 2 minutes after 10:00
    [run] = await kilo.scheduler.tick()
    await kilo.launcher.wait_idle()
    assert len(model.requests) == 1
    assert run.scheduled_for == datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)


async def test_slot_exactly_at_the_catchup_limit_fires(kilo, model, clock):
    model.turns.append(text_turn("ok"))
    await add(kilo, cron="0 10 * * *", timezone="UTC")
    clock.now = datetime(2026, 10, 30, 10, 5, tzinfo=timezone.utc)
    await kilo.scheduler.tick()
    await kilo.launcher.wait_idle()
    assert len(model.requests) == 1


async def test_slot_past_the_catchup_limit_is_skipped_after_long_downtime(kilo, model, clock):
    await add(kilo, cron="0 10 * * *", timezone="UTC")
    clock.now = datetime(2026, 10, 30, 10, 6, tzinfo=timezone.utc)
    [run] = await kilo.scheduler.tick()
    assert run.status == RunStatus.SKIPPED and model.requests == []


async def test_one_off_stays_missed_after_long_downtime(kilo, model, clock):
    await add(kilo, run_in_minutes=1)
    clock.advance(days=2)
    [run] = await kilo.scheduler.tick()
    assert run.status == RunStatus.SKIPPED and model.requests == []


async def test_not_due_yet_does_nothing(kilo, clock):
    await add(kilo, run_in_minutes=10)
    clock.advance(minutes=9)
    assert await kilo.scheduler.tick() == []


@pytest.mark.parametrize("timing", [{}, {"cron": "0 * * * *", "run_in_minutes": 5}])
async def test_exactly_one_timing_required(kilo, timing):
    with pytest.raises(ScheduleError, match="exactly one"):
        await add(kilo, **timing)


async def test_name_and_instructions_required(kilo):
    with pytest.raises(ScheduleError):
        await add(kilo, name=" ", cron="0 * * * *")
    with pytest.raises(ScheduleError):
        await add(kilo, instructions=" ", cron="0 * * * *")


async def test_active_dates_only_for_cron(kilo):
    with pytest.raises(ScheduleError, match="cron schedules only"):
        await add(kilo, run_in_minutes=5, active_from=date(2026, 11, 1))


async def test_active_until_before_active_from_refused(kilo):
    dates = {"active_from": date(2026, 11, 2), "active_until": date(2026, 11, 1)}
    with pytest.raises(ScheduleError, match="before"):
        await add(kilo, cron="0 * * * *", **dates)


async def test_active_from_delays_the_first_run(kilo):
    created = await add(kilo, cron="0 9 * * *", timezone="Asia/Taipei", active_from=date(2026, 11, 5))
    assert created.next_run_at == datetime(2026, 11, 5, 1, 0, tzinfo=timezone.utc)


async def test_schedule_that_can_never_run_refused(kilo):
    with pytest.raises(ScheduleError, match="never run"):
        await add(kilo, cron="0 0 1 * *", active_until=date(2026, 10, 29))


async def test_past_run_at_refused(kilo, clock):
    with pytest.raises(ScheduleError, match="in the past"):
        await add(kilo, run_at=clock() - timedelta(days=1))


async def test_unknown_timezone_refused(kilo):
    with pytest.raises(ScheduleError, match="Unknown timezone"):
        await add(kilo, cron="0 * * * *", timezone="Mars/Base")


async def test_invalid_cron_refused(kilo):
    with pytest.raises(ScheduleError, match="Invalid cron"):
        await add(kilo, cron="not cron")


async def test_naive_run_at_uses_schedule_timezone(kilo):
    created = await add(kilo, run_at=datetime(2026, 10, 29, 9, 0), timezone="Asia/Taipei")
    assert created.run_at == datetime(2026, 10, 29, 1, 0, tzinfo=timezone.utc)


async def test_default_timezone_is_the_configured_one(kilo):
    created = await add(kilo, cron="0 9 * * *")
    assert created.timezone == settings.kilo_timezone


async def test_cron_in_timezone_converts_to_utc(kilo):
    created = await add(kilo, cron="0 9 * * *", timezone="Asia/Taipei")
    assert created.next_run_at == datetime(2026, 10, 29, 1, 0, tzinfo=timezone.utc)


async def test_cron_inside_a_daylight_saving_gap_still_runs(kilo, clock):
    clock.now = datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc)
    created = await add(kilo, cron="30 2 8 3 *", timezone="America/New_York")
    assert next_run(created, clock()) == datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)


async def test_update_can_switch_from_recurring_to_one_off(kilo):
    created = await add(kilo, cron="0 * * * *")
    await kilo.scheduler.update(created.id, ScheduleUpdate(run_in_minutes=5))
    updated = await kilo.scheduler.approve(created.id)
    assert updated.cron is None and updated.run_at is not None


async def test_paused_schedule_does_not_fire_until_resumed(kilo, model, clock):
    created = await add(kilo, cron="0 * * * *")
    await kilo.scheduler.pause(created.id)
    clock.advance(hours=2)
    assert await kilo.scheduler.tick() == []
    await kilo.scheduler.resume(created.id)
    assert (await kilo.scheduler.get(created.id)).next_run_at is None  # still paused until approved
    resumed = await kilo.scheduler.approve(created.id)
    assert resumed.enabled and resumed.next_run_at > clock()


async def test_resuming_a_running_schedule_is_refused(kilo):
    created = await add(kilo, cron="0 * * * *")
    with pytest.raises(ScheduleError, match="not paused"):
        await kilo.scheduler.resume(created.id)


async def test_unknown_schedule_not_found(kilo):
    with pytest.raises(ScheduleNotFound):
        await kilo.scheduler.delete("sched_x")
    with pytest.raises(ScheduleNotFound):
        await kilo.scheduler.approve("sched_x")


async def test_new_schedule_does_not_fire_before_approval(kilo, model, clock):
    created = await kilo.scheduler.create(spec(cron="0 * * * *"))
    assert not created.approved and created.next_run_at is None
    clock.advance(hours=2)
    assert await kilo.scheduler.tick() == []
    assert [s.id for s in await kilo.scheduler.awaiting_approval()] == [created.id]


async def test_approval_plans_the_schedule(kilo, clock):
    created = await kilo.scheduler.create(spec(cron="0 * * * *"))
    approved = await kilo.scheduler.approve(created.id)
    assert approved.approved and approved.pending is None and approved.next_run_at > clock()
    assert await kilo.scheduler.awaiting_approval() == []
    with pytest.raises(ScheduleError, match="nothing awaiting approval"):
        await kilo.scheduler.approve(created.id)


async def test_refused_new_schedule_is_deleted(kilo):
    created = await kilo.scheduler.create(spec(cron="0 * * * *"))
    assert await kilo.scheduler.reject(created.id) is None
    assert await kilo.scheduler.get(created.id) is None


async def test_change_waits_for_approval_and_the_old_version_runs_meanwhile(kilo, model, clock):
    created = await add(kilo, cron="0 * * * *", instructions="old job")
    await kilo.scheduler.update(created.id, ScheduleUpdate(instructions="new job"))
    model.turns.append(text_turn("ok"))
    clock.advance(hours=1)
    [run] = await kilo.scheduler.tick()
    await kilo.launcher.wait_idle()
    assert "old job" in (await kilo.db.get_session(run.session_id)).messages[0].content
    refused = await kilo.scheduler.reject(created.id)
    assert refused.instructions == "old job" and refused.pending is None and refused.next_run_at is not None
    await kilo.scheduler.update(created.id, ScheduleUpdate(instructions="new job"))
    assert (await kilo.scheduler.approve(created.id)).instructions == "new job"


async def test_changes_to_a_new_schedule_add_to_its_proposal(kilo):
    created = await kilo.scheduler.create(spec(cron="0 * * * *"))
    changed = await kilo.scheduler.update(created.id, ScheduleUpdate(name="renamed"))
    assert changed.name == "renamed" and not changed.approved and changed.next_run_at is None
    assert (await kilo.scheduler.approve(created.id)).name == "renamed"


async def test_run_in_minutes_counts_from_approval(kilo, clock):
    created = await kilo.scheduler.create(spec(run_in_minutes=10))
    clock.advance(minutes=30)
    approved = await kilo.scheduler.approve(created.id)
    assert approved.run_at == clock() + timedelta(minutes=10)


async def test_approving_a_one_off_whose_time_passed_discards_it(kilo, clock):
    created = await kilo.scheduler.create(spec(run_at=clock() + timedelta(minutes=5)))
    clock.advance(days=1)
    with pytest.raises(ScheduleError, match="discarded"):
        await kilo.scheduler.approve(created.id)
    assert await kilo.scheduler.get(created.id) is None


async def test_pausing_applies_at_once_and_keeps_the_proposal_paused(kilo):
    created = await add(kilo, cron="0 * * * *")
    await kilo.scheduler.update(created.id, ScheduleUpdate(name="renamed"))
    paused = await kilo.scheduler.pause(created.id)
    assert not paused.enabled and paused.next_run_at is None
    approved = await kilo.scheduler.approve(created.id)
    assert approved.name == "renamed" and not approved.enabled


async def test_awaiting_approval_filters_by_session(kilo):
    mine = await kilo.scheduler.create(spec(cron="0 * * * *", origin_session_id="sess_a"))
    await kilo.scheduler.create(spec(cron="0 * * * *", origin_session_id="sess_b"))
    assert [s.id for s in await kilo.scheduler.awaiting_approval("sess_a")] == [mine.id]


async def test_overlapping_run_is_skipped(kilo, model, clock):
    release = asyncio.Event()
    model.turns.append([release.wait, *text_turn("ok")])
    await add(kilo, cron="0 * * * *")
    clock.advance(hours=1)
    [first] = await kilo.scheduler.tick()
    clock.advance(hours=1)
    [second] = await kilo.scheduler.tick()
    assert first.status == RunStatus.RUNNING
    assert second.status == RunStatus.SKIPPED and "still in progress" in second.summary
    release.set()
    await kilo.launcher.wait_idle()


async def test_run_continues_origin_session(kilo, model, clock):
    session = await kilo.db.create_session()
    model.turns.append(text_turn("done"))
    await add(kilo, cron="0 * * * *", origin_session_id=session.id)
    clock.advance(hours=1)
    [run] = await kilo.scheduler.tick()
    await kilo.launcher.wait_idle()
    assert run.session_id == session.id
    stored = await kilo.db.get_session(session.id)
    assert "do it" in stored.messages[0].content
    assert (await kilo.schedule_store.get_run(run.id)).summary == "done"


async def test_run_without_session_gets_one_and_keeps_it(kilo, model, clock):
    model.turns.extend([text_turn("one"), text_turn("two")])
    created = await add(kilo, cron="0 * * * *")
    clock.advance(hours=1)
    [first] = await kilo.scheduler.tick()
    await kilo.launcher.wait_idle()
    clock.advance(hours=1)
    [second] = await kilo.scheduler.tick()
    await kilo.launcher.wait_idle()
    assert first.session_id == second.session_id
    assert (await kilo.scheduler.get(created.id)).origin_session_id == first.session_id


async def test_failed_model_marks_run_failed(kilo, model, clock):
    model.turns.append([RuntimeError("boom")])
    await add(kilo, cron="0 * * * *")
    clock.advance(hours=1)
    [run] = await kilo.scheduler.tick()
    await kilo.launcher.wait_idle()
    stored = await kilo.schedule_store.get_run(run.id)
    assert stored.status == RunStatus.FAILED and "boom" in stored.summary


async def test_recover_fails_unfinished_runs(kilo, clock):
    run = Run(id="run_left", schedule_id="s", schedule_name="n", status=RunStatus.RUNNING, scheduled_for=clock())
    await kilo.schedule_store.save_run(run)
    assert await kilo.launcher.recover() == 1
    stored = await kilo.schedule_store.get_run("run_left")
    assert stored.status == RunStatus.FAILED and stored.summary == INTERRUPTED


async def test_background_loop_fires_due_schedule(kilo, model, clock):
    model.turns.append(text_turn("ok"))
    created = await add(kilo, cron="0 * * * *")
    clock.advance(hours=1)
    await kilo.scheduler.start()
    for _ in range(200):
        if await kilo.scheduler.list_runs(created.id):
            break
        await asyncio.sleep(0.01)
    await kilo.scheduler.stop()
    await kilo.launcher.wait_idle()
    assert len(await kilo.scheduler.list_runs(created.id)) == 1
    assert [RunKind.SCHEDULED] == [r.kind for r in await kilo.scheduler.list_runs(created.id)]


async def schedule_tool(kilo, **args):
    return json.loads(await kilo.toolbox.execute("schedule", args))


async def test_tool_create_is_a_proposal_in_its_session(kilo):
    token = current_session_id.set("sess_here")
    try:
        result = await schedule_tool(
            kilo, action="create", name="n", instructions="do it", cron="0 * * * *",
        )
    finally:
        current_session_id.reset(token)
    assert result["status"] == "awaiting_approval"
    assert result["schedule"]["awaiting_approval"] and result["schedule"]["change"] == "create"
    created = await kilo.scheduler.get(result["schedule"]["id"])
    assert not created.approved and created.origin_session_id == "sess_here"


async def test_tool_update_and_resume_are_proposals_pause_is_not(kilo):
    created = await add(kilo, cron="0 * * * *")
    paused = await schedule_tool(kilo, action="pause", id=created.id)
    assert paused["status"] == "success" and paused["schedule"]["enabled"] is False
    resumed = await schedule_tool(kilo, action="resume", id=created.id)
    assert resumed["status"] == "awaiting_approval" and resumed["schedule"]["proposal"]["enabled"] is True
    updated = await schedule_tool(kilo, action="update", id=created.id, name="renamed")
    assert updated["status"] == "awaiting_approval" and updated["schedule"]["name"] == "n"
    assert updated["schedule"]["proposal"]["name"] == "renamed"


async def test_tool_reports_refusals_as_errors(kilo):
    result = await schedule_tool(kilo, action="create", name="n", instructions="x", cron="not cron")
    assert result["status"] == "error" and "Invalid cron" in result["message"]
    assert (await schedule_tool(kilo, action="pause"))["status"] == "error"
    assert (await schedule_tool(kilo, action="create"))["status"] == "error"


async def test_tool_lists_schedules(kilo):
    await add(kilo, cron="0 * * * *")
    await kilo.scheduler.create(spec(cron="0 * * * *"))
    listed = (await schedule_tool(kilo, action="list"))["schedules"]
    assert [s["awaiting_approval"] for s in listed] == [False, True]


async def test_a_session_has_one_schedule_at_a_time(kilo):
    first = await kilo.scheduler.create(spec(cron="0 * * * *", origin_session_id="sess_a"))
    with pytest.raises(ScheduleError, match=first.id):  # proposed only: it holds the session's place
        await kilo.scheduler.create(spec(cron="0 9 * * *", origin_session_id="sess_a"))
    await kilo.scheduler.approve(first.id)
    await kilo.scheduler.pause(first.id)
    with pytest.raises(ScheduleError, match="Update or delete it instead"):
        await kilo.scheduler.create(spec(run_in_minutes=5, origin_session_id="sess_a"))
    await kilo.scheduler.create(spec(cron="0 9 * * *", origin_session_id="sess_b"))  # another session: fine
    await kilo.scheduler.delete(first.id)
    await kilo.scheduler.create(spec(cron="0 9 * * *", origin_session_id="sess_a"))


async def test_a_refused_proposal_frees_the_session(kilo):
    first = await kilo.scheduler.create(spec(cron="0 * * * *", origin_session_id="sess_a"))
    await kilo.scheduler.reject(first.id)
    await kilo.scheduler.create(spec(cron="0 * * * *", origin_session_id="sess_a"))


async def test_a_fired_one_off_frees_the_session(kilo, model, clock):
    session = await kilo.db.create_session()
    model.turns.append(text_turn("ok"))
    once = await add(kilo, run_in_minutes=1, origin_session_id=session.id)
    clock.advance(minutes=1)
    [run] = await kilo.scheduler.tick()
    await kilo.launcher.wait_idle()
    assert await kilo.scheduler.get(once.id) is None
    assert (await kilo.schedule_store.get_run(run.id)).schedule_name == "n"
    await kilo.scheduler.create(spec(cron="0 * * * *", origin_session_id=session.id))
