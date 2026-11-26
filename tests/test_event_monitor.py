import json
from asyncio import Event as AsyncEvent, create_task, gather, wait_for
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.app import App
from core.events import Event, EventType
from monitor.events import EventOptions
from monitor.weibo import PageResult
from state.repository import MAX_DELIVERY_ATTEMPTS, StateRepository


def post(post_id, text=None):
    return {
        "id": post_id,
        "text": text if text is not None else f"微博 {post_id}",
        "time": "Wed Nov 25 12:00:00 +0800 2026",
        "screen_name": "Weibo User",
        "source": "Web",
        "region_name": "",
    }


def timeline(items, **metadata):
    raw = {item["id"]: {"visible": {"type": 0}, "isTop": False} for item in items}
    for post_id, extra in metadata.items():
        raw[post_id].update(extra)
    return PageResult(items, metadata=raw)


async def make_app(tmp_path, client, results=None):
    app = App()
    app.config = {"notification": {"telegram_template": "{text}"}}
    app.monitor = client
    app.repository = StateRepository()
    app.repository._db_path = tmp_path / "events.db"
    await app.repository.initialize()
    app.notifer = AsyncMock()
    app.notifer.send_message.return_value = results or {"telegram": (True, None)}
    return app


async def stored_events(repo):
    async with repo._connect() as db:
        async with db.execute("SELECT payload FROM events ORDER BY rowid") as cursor:
            return [json.loads(payload) for (payload,) in await cursor.fetchall()]


async def test_all_event_types_are_persisted_and_delivered(tmp_path):
    initial = timeline([post("4"), post("3"), post("2"), post("1")], **{"4": {"isTop": True}})
    changed = timeline(
        [post("3"), post("6"), post("5", "New keyword"), post("4"), post("1", "edited")],
        **{"3": {"isTop": True}, "6": {"retweeted_status": {"id": 999}}},
    )
    like = {"id": "18", "text": "", "url": "https://weibo.com/detail/18"}
    newer_like = {"id": "19", "text": "Liked", "url": "https://weibo.com/detail/19"}
    profile = {"screen_name": "Before", "avatar": "https://example.com/avatar", "description": ""}
    following = {"12": {"id": "12", "screen_name": "Removed"}}
    new_following = {"13": {"id": "13", "screen_name": "Added"}}
    comment_time = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    comment = {"id": "61", "author_id": "123", "text": "Hi", "time": comment_time}
    client = AsyncMock()
    client.get_weibo_list.side_effect = [initial, changed, changed]
    client.get_profile.side_effect = [profile, {**profile, "screen_name": "After"}, {**profile, "screen_name": "After"}]
    client.get_following.side_effect = [following, new_following, new_following]
    client.get_likes.side_effect = [[like], [newer_like, like], [newer_like, like]]
    client.get_comments.side_effect = [[], [comment, {**comment, "id": "62", "author_id": "7"}], [comment]]
    client.get_visibility.return_value = "not_found"
    app = await make_app(tmp_path, client)
    account = {
        "weiboid": "123", "events": list(EventType), "keywords": ["KEYWORD"], "comment_post_ids": ["99"],
    }

    await app._check_single(account)
    assert await stored_events(app.repository) == []
    await app._check_single(account)
    first_changes = {event["type"] for event in await stored_events(app.repository)}
    assert "post.deleted" not in first_changes
    assert "following.removed" not in first_changes
    await app._check_single(account)
    events = await stored_events(app.repository)

    schema = json.loads((Path(__file__).resolve().parents[1] / "core" / "event.schema.json").read_text())
    assert {event["type"] for event in events} == set(schema["properties"]["type"]["enum"]) == set(EventType)
    assert len(events) == len(EventType)
    for event in events:
        assert set(event) == set(schema["required"])
        assert event["account_id"] == "123"
        assert event["schema_version"] == 1
        assert event["observed_at"].endswith("Z")
    by_type = {event["type"]: event for event in events}
    assert by_type["post.reposted"]["data"]["original_post_id"] == "999"
    assert by_type["post.created"]["occurred_at"] == "2026-11-25T04:00:00.000Z"
    assert by_type["post.updated"]["data"]["after"]["text"] == "edited"
    assert by_type["post.deleted"]["data"]["confirmations"] == 2
    assert by_type["post.deleted"]["actor_id"] is None
    assert by_type["post.liked"]["occurred_at"] is None
    assert by_type["comment.created"]["subject"]["id"] == "61"
    assert by_type["post.pinned"]["data"]["previous_post_id"] == "4"
    assert by_type["post.pinned"]["data"]["post_id"] == "3"
    assert by_type["keyword.matched"]["data"]["keywords"] == ["KEYWORD"]
    assert app.notifer.send_message.await_count == 11
    assert app.repository.get_latest_id("123") == "6"


async def test_inconclusive_results_break_missing_streaks(tmp_path):
    client = AsyncMock()
    client.get_weibo_list.side_effect = [timeline([post("2")]), PageResult([]), [], PageResult([]), PageResult([]), PageResult([])]
    client.get_visibility.side_effect = ["not_found", "unknown", "not_found", "not_found"]
    client.get_following.side_effect = [
        {"12": {"id": "12", "screen_name": "Followed"}}, {}, None, {}, {}, {},
    ]
    app = await make_app(tmp_path, client)
    account = {"weiboid": "123", "events": ["post.deleted", "following.removed"]}

    for _ in range(4):
        await app._check_single(account)
        assert await stored_events(app.repository) == []
    await app._check_single(account)
    assert [event["type"] for event in await stored_events(app.repository)] == ["following.removed"]
    await app._check_single(account)
    assert [event["type"] for event in await stored_events(app.repository)] == ["following.removed", "post.deleted"]


async def test_event_outbox_retries_after_restart_and_timeline_failure(tmp_path):
    client = AsyncMock()
    client.get_weibo_list.side_effect = [[post("1")], [post("2"), post("1")]]
    app = await make_app(tmp_path, client, {"telegram": (False, "synthetic error")})
    account = {"weiboid": "123"}
    await app._check_single(account)
    await app._check_single(account)
    [first] = await stored_events(app.repository)
    assert app.repository.get_latest_id("123") == "1"

    restarted_client = AsyncMock()
    restarted_client.get_weibo_list.return_value = []
    restarted = await make_app(tmp_path, restarted_client)
    await restarted._check_single(account)
    assert restarted.repository.get_latest_id("123") == "2"
    assert await stored_events(restarted.repository) == [first]
    assert restarted.notifer.send_message.await_count == 1
    assert await restarted.repository.claim_pending_event("123") is None


async def test_older_post_moved_to_top_does_not_trigger_post_created(tmp_path):
    client = AsyncMock()
    observed = post("2")
    historical = {**post("1"), "time": "Tue Nov 24 12:00:00 +0800 2026"}
    newest = {**post("3"), "time": "Thu Nov 26 12:00:00 +0800 2026"}
    client.get_weibo_list.side_effect = [[observed], [historical, newest, observed]]
    app = await make_app(tmp_path, client)
    account = {"weiboid": "123"}

    await app._check_single(account)
    await app._check_single(account)

    [event] = await stored_events(app.repository)
    assert event["subject"]["id"] == "3"
    assert app.repository.get_latest_id("123") == "3"


async def test_event_scan_revision_and_delivery_claim_are_exclusive(tmp_path):
    app = await make_app(tmp_path, AsyncMock())
    client = app.monitor
    await app.repository.set_latest_id("123", "1")
    client.get_weibo_list.return_value = [post("2"), post("1")]
    app.config["notification"].update(enable_telegram=False, enable_serverchan=False)
    await app._check_single({"weiboid": "123"})
    await app.repository.save_event_scan("123", "timeline", 0, {"cursor": "stale"}, [])
    _, snapshot = await app.repository.get_event_snapshot("123", "timeline")
    assert snapshot["cursor"] == "2"

    claimed = await gather(*(app.repository.claim_pending_event("123") for _ in range(2)))
    assert sum(item is not None for item in claimed) == 1
    event, token = next(item for item in claimed if item is not None)
    success = {"telegram": (True, None)}
    assert not await app.repository.finish_event_delivery(event, "wrong-token", success)
    assert await app.repository.finish_event_delivery(event, token, success)


@pytest.mark.parametrize("options", [
    {"events": "post.created"},
    {"events": ["unknown"]},
    {"events": ["keyword.matched"]},
    {"events": ["comment.created"], "comment_post_ids": ["abc"]},
    {"following_page_limit": 0},
])
def test_invalid_event_options_are_rejected(options):
    with pytest.raises(ValueError):
        EventOptions.from_account({"weiboid": "123", **options})


async def test_permanently_failed_delivery_releases_queue_after_restart(tmp_path):
    account = {"weiboid": "123"}
    client = AsyncMock()
    client.get_weibo_list.side_effect = [[post("1")], [post("2", "拒收"), post("1")]]
    app = await make_app(tmp_path, client, {"telegram": (False, "BadRequest")})
    await app._check_single(account)
    await app._check_single(account)

    client = AsyncMock()
    client.get_weibo_list.return_value = [post("3", "可发送")]
    restarted = await make_app(tmp_path, client)
    sent = []

    async def deliver(serverchan, telegram, title):
        text = telegram()
        if text == "拒收":
            return {"telegram": (False, "BadRequest")}
        sent.append(text)
        return {"telegram": (True, None)}

    restarted.notifer.send_message.side_effect = deliver
    for _ in range(MAX_DELIVERY_ATTEMPTS - 1):
        await restarted._check_single(account)

    assert sent == ["可发送"]
    assert restarted.repository.get_latest_id("123") == "3"
    async with restarted.repository._connect() as db:
        async with db.execute(
            "SELECT delivery_attempts, failed_at IS NOT NULL, delivered_at IS NOT NULL FROM events ORDER BY rowid"
        ) as cursor:
            assert await cursor.fetchall() == [(MAX_DELIVERY_ATTEMPTS, 1, 0), (1, 0, 1)]
    assert await restarted.repository.claim_pending_event("123") is None


async def test_slow_activity_does_not_skip_any_accounts_timeline(tmp_path):
    client = AsyncMock()
    app = await make_app(tmp_path, client)
    app.config["accounts"] = [
        {"weiboid": "123", "events": ["post.created", "following.added"]},
        {"weiboid": "456"},
    ]
    slow_started, allow_finish, slow_finished, timelines_ran = (
        AsyncEvent(), AsyncEvent(), AsyncEvent(), AsyncEvent()
    )
    timeline_counts = {"123": 0, "456": 0}

    async def follow(*args):
        slow_started.set()
        await allow_finish.wait()
        slow_finished.set()

    async def load_timeline(uid):
        if slow_started.is_set() and not allow_finish.is_set():
            timeline_counts[uid] += 1
            if all(count >= 2 for count in timeline_counts.values()):
                timelines_ran.set()
        return []

    client.get_following.side_effect = follow
    client.get_weibo_list.side_effect = load_timeline
    app._schedule_jobs()
    app.scheduler.start(paused=True)
    app.scheduler.reschedule_job("activity:123", trigger="interval", seconds=0.03)
    for uid in timeline_counts:
        app.scheduler.reschedule_job(f"timeline:{uid}", trigger="interval", seconds=0.02)
    app.scheduler.resume()
    try:
        await wait_for(slow_started.wait(), timeout=2)
        await wait_for(timelines_ran.wait(), timeout=2)
        assert client.get_following.await_count == 1
    finally:
        app.scheduler.pause()
        allow_finish.set()
        if slow_started.is_set():
            await wait_for(slow_finished.wait(), timeout=2)
        await app.stop()


async def test_stale_concurrent_timeline_cannot_revert_an_edit(tmp_path):
    account = {"weiboid": "123", "events": ["post.updated"]}
    old_page = timeline([post("1", "旧正文")])
    client = AsyncMock()
    client.get_weibo_list.return_value = old_page
    current = await make_app(tmp_path, client)
    await current._check_single(account)

    request_started, release_old_page = AsyncEvent(), AsyncEvent()

    async def delayed_old_page(uid):
        request_started.set()
        await release_old_page.wait()
        return old_page

    slow_client = AsyncMock()
    slow_client.get_weibo_list.side_effect = delayed_old_page
    delayed = await make_app(tmp_path, slow_client)
    delayed_poll = create_task(delayed._check_single(account))
    try:
        await wait_for(request_started.wait(), timeout=2)
        client.get_weibo_list.return_value = timeline([post("1", "新正文")])
        await current._check_single(account)
    finally:
        release_old_page.set()
        await wait_for(delayed_poll, timeout=2)

    [edit] = await stored_events(current.repository)
    assert edit["type"] == "post.updated"
    assert edit["data"]["before"]["text"] == "旧正文"
    assert edit["data"]["after"]["text"] == "新正文"
    _, snapshot = await current.repository.get_event_snapshot(
        "123", f"post_changes:{current._event_monitor(account).viewer}",
    )
    assert snapshot["posts"]["1"]["content"]["text"] == "新正文"


async def test_legacy_pinned_cursor_does_not_replay_unchanged_history(tmp_path):
    account = {"weiboid": "123"}
    pin = {**post("1"), "time": "Tue Nov 24 12:00:00 +0800 2026"}
    ordinary = post("2")
    client = AsyncMock()
    client.get_weibo_list.return_value = timeline([pin, ordinary], **{"1": {"isTop": True}})
    app = await make_app(tmp_path, client)
    await app.repository.set_latest_id("123", "1")

    await app._check_single(account)
    assert await stored_events(app.repository) == []

    newer = {**post("3"), "time": "Thu Nov 26 12:00:00 +0800 2026"}
    client.get_weibo_list.return_value = timeline([pin, newer, ordinary], **{"1": {"isTop": True}})
    await app._check_single(account)
    [event] = await stored_events(app.repository)
    assert event["subject"]["id"] == "3"


async def test_initial_page_with_only_pinned_post_sets_timeline_baseline(tmp_path):
    client = AsyncMock()
    client.get_weibo_list.side_effect = [
        timeline([post("1")], **{"1": {"isTop": True}}),
        timeline([post("2")]),
    ]
    app = await make_app(tmp_path, client)
    account = {"weiboid": "123"}
    await app._check_single(account)
    assert app.repository.get_latest_id("123") == "1"
    assert await stored_events(app.repository) == []

    await app._check_single(account)
    [event] = await stored_events(app.repository)
    assert event["subject"]["id"] == "2"


async def test_long_text_keyword_retry_survives_restart_without_replaying_post(tmp_path):
    client = AsyncMock()
    account = {"weiboid": "123", "events": ["post.created", "keyword.matched"], "keywords": ["命中"]}
    page = timeline([post("2", "摘要"), post("1")], **{"2": {"isLongText": True, "mblogid": "long-id"}})
    client.get_weibo_list.side_effect = [timeline([post("1")]), page]
    client.get_long_text.return_value = None
    app = await make_app(tmp_path, client)
    await app._check_single(account)
    await app._check_single(account)
    assert [event["type"] for event in await stored_events(app.repository)] == ["post.created"]
    assert app.notifer.send_message.await_count == 1

    client = AsyncMock()
    client.get_weibo_list.return_value = page
    client.get_long_text.return_value = "全文中才有命中"
    restarted = await make_app(tmp_path, client)
    await restarted._check_single(account)
    await restarted._check_single(account)
    created, matched = await stored_events(restarted.repository)
    assert created["type"] == "post.created"
    assert matched["type"] == "keyword.matched"
    assert matched["data"]["post"]["text"] == "全文中才有命中"
    client.get_long_text.assert_awaited_once_with("long-id")


async def test_pin_changes_survive_confirmed_source_switches(tmp_path):
    client = AsyncMock()
    client.get_weibo_list.side_effect = [
        timeline([post("1")], **{"1": {"isTop": True}}),
        PageResult([post("1")], metadata={"1": {}}),
        timeline([post("2")], **{"2": {"isTop": True}}),
    ]
    client.get_mobile_pin.return_value = True, None
    app = await make_app(tmp_path, client)
    account = {"weiboid": "123", "events": ["post.pinned"]}
    for _ in range(3):
        await app._check_single(account)

    events = await stored_events(app.repository)
    assert [(e["data"]["previous_post_id"], e["data"]["post_id"]) for e in events] == [("1", None), (None, "2")]
    assert [event["source"] for event in events] == ["/api/container/getIndex", "/ajax/statuses/mymblog"]


async def test_incomplete_timeline_never_establishes_unpin_by_itself(tmp_path):
    client = AsyncMock()
    incomplete = PageResult([post("2")], metadata={"2": {"isTop": False}}, complete=False)
    client.get_weibo_list.side_effect = [
        timeline([post("1")], **{"1": {"isTop": True}}),
        incomplete,
        timeline([post("2")]),
    ]
    client.get_mobile_pin.return_value = False, None
    app = await make_app(tmp_path, client)
    account = {"weiboid": "123", "events": ["post.pinned"]}
    await app._check_single(account)
    await app._check_single(account)
    assert await stored_events(app.repository) == []

    await app._check_single(account)
    [event] = await stored_events(app.repository)
    assert event["data"]["post_id"] is None


async def test_existing_queue_is_migrated_and_claimed_without_returning(tmp_path, monkeypatch):
    repo = StateRepository()
    repo._db_path = tmp_path / "previous.db"
    event = Event.detect(
        EventType.POST_LIKED, "123", "post", "2", "/ajax/statuses/likelist",
        {"post": post("2"), "url": "https://weibo.com/detail/2"},
    )
    async with repo._connect() as db:
        await db.execute(
            """
            CREATE TABLE events (
                event_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                type TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                delivered_at TEXT,
                delivery_token TEXT,
                lease_until TEXT
            )
            """
        )
        await db.execute(
            "INSERT INTO events (event_id, account_id, type, observed_at, payload) VALUES (?, ?, ?, ?, ?)",
            (event.event_id, event.account_id, event.type, event.observed_at, json.dumps(event.to_dict())),
        )
        await db.commit()

    statements = []
    connect = repo._connect

    @asynccontextmanager
    async def traced_connection():
        async with connect() as db:
            await db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(repo, "_connect", traced_connection)
    await repo.initialize()
    claimed, token = await repo.claim_pending_event("123")
    assert claimed == event
    assert await repo.finish_event_delivery(claimed, token, {"telegram": (True, None)})
    assert await repo.claim_pending_event("123") is None
    assert all("RETURNING" not in sql.upper() for sql in statements)
