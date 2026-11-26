from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from loguru import logger

from core.events import Event, EventType, TIMELINE_TYPES, utc_timestamp
from monitor.weibo import (
    COMMENTS_PATH,
    FOLLOWING_PATH,
    LIKES_PATH,
    MOBILE_TIMELINE_PATH,
    PROFILE_PATH,
    STATUS_PATH,
    TIMELINE_PATH,
    PageResult,
    identifier,
    object_or_empty,
    pinned_marker,
    post_url,
)

POST_CHANGE_TYPES = frozenset((EventType.POST_UPDATED, EventType.POST_DELETED))
FOLLOWING_TYPES = frozenset((EventType.FOLLOWING_ADDED, EventType.FOLLOWING_REMOVED))
TIMELINE_EVENT_TYPES = TIMELINE_TYPES | {EventType.KEYWORD_MATCHED}


@dataclass(frozen=True)
class EventOptions:
    types: frozenset[EventType]
    keywords: tuple[str, ...]
    comment_post_ids: tuple[str, ...]
    following_page_limit: int
    activity_page_limit: int

    @classmethod
    def from_account(cls, account):
        event_names = account.get("events")
        if "events" in account and not isinstance(event_names, list):
            raise ValueError("accounts[].events 必须是事件类型列表")
        try:
            types = TIMELINE_TYPES if event_names is None else frozenset(map(EventType, event_names))
        except (TypeError, ValueError):
            raise ValueError("accounts[].events 包含未知事件类型") from None

        keywords = account.get("keywords", [])
        if not isinstance(keywords, list) or not all(isinstance(v, str) and v.strip() for v in keywords):
            raise ValueError("accounts[].keywords 必须是非空字符串的列表")
        post_ids = account.get("comment_post_ids", [])
        if not isinstance(post_ids, list) or not all(
            identifier(v).isascii() and identifier(v).isdigit() for v in post_ids
        ):
            raise ValueError("accounts[].comment_post_ids 必须是数字微博 ID 列表")
        if EventType.KEYWORD_MATCHED in types and not keywords:
            raise ValueError("启用 keyword.matched 时需要配置 keywords")
        if EventType.COMMENT_CREATED in types and not post_ids:
            raise ValueError("启用 comment.created 时需要配置 comment_post_ids")

        following_limit = account.get("following_page_limit", 20)
        activity_limit = account.get("activity_page_limit", 2)
        for name, value, maximum in (
            ("following_page_limit", following_limit, 50),
            ("activity_page_limit", activity_limit, 5),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"accounts[].{name} 必须是 1 到 {maximum} 的整数")
        return cls(
            types, tuple(dict.fromkeys(v.strip() for v in keywords)),
            tuple(dict.fromkeys(map(str, post_ids))), following_limit, activity_limit,
        )


def recent_ids(current, previous, limit):
    return list(dict.fromkeys([*current, *previous]))[:limit]


def reposted(post):
    return isinstance(post.get("retweeted_status"), dict)


def content_snapshot(post, text):
    page = object_or_empty(post.get("page_info"))
    pictures = post.get("pic_ids")
    if not isinstance(pictures, list):
        pictures = list(object_or_empty(post.get("pic_infos")))
    return {
        "text": text.replace("\r\n", "\n"),
        "pictures": list(map(str, pictures)),
        "media_id": identifier(page.get("object_id") or page.get("page_id")),
    }


class EventMonitor:
    def __init__(self, uid, options, client, repository, viewer):
        self.uid = uid
        self.options = options
        self.client = client
        self.repository = repository
        self.viewer = viewer

    @staticmethod
    def viewer_key(cookie):
        return sha256((cookie or "").encode()).hexdigest()

    def event(self, kind, subject_type, subject_id, source, data, occurred_at=None):
        return Event.detect(
            kind, self.uid, subject_type, subject_id, source, data, occurred_at=occurred_at,
        )

    @property
    def watches_timeline(self):
        return bool(self.options.types & TIMELINE_EVENT_TYPES)

    @property
    def watches_activity(self):
        return bool(self.options.types - TIMELINE_EVENT_TYPES)

    async def poll(self, *, timeline, activity):
        timeline = timeline and self.watches_timeline
        activity = activity and self.watches_activity
        post_changes_stream = f"post_changes:{self.viewer}"
        pin_stream = f"pin:{self.viewer}"
        post_changes = activity and bool(self.options.types & POST_CHANGE_TYPES)
        pin = activity and EventType.POST_PINNED in self.options.types
        # The revisions belong to the state before the network request.
        timeline_state = None
        if timeline:
            revision, previous = await self.repository.get_event_snapshot(self.uid, "timeline")
            old_id = previous.get("cursor") or self.repository.get_latest_id(self.uid)
            timeline_state = revision, previous, old_id
        changes_state = await self.repository.get_event_snapshot(self.uid, post_changes_stream) if post_changes else None
        pin_state = await self.repository.get_event_snapshot(self.uid, pin_stream) if pin else None
        page = await self.client.get_weibo_list(self.uid) if timeline or post_changes or pin else []
        if timeline:
            await self.scan_timeline(page, *timeline_state)
        if post_changes and (page or (isinstance(page, PageResult) and page.complete)):
            await self._scan_post_changes(page, post_changes_stream, *changes_state)
        if pin:
            await self._scan_pin(page, pin_stream, *pin_state)
        if activity:
            await self.scan_activity()

    async def scan_timeline(self, page, revision, previous, old_id):
        if not page:
            return
        raw_ids = list(map(str, getattr(page, "raw_ids", [post["id"] for post in page])))
        metadata = getattr(page, "metadata", {})
        pins = {post_id for post_id in raw_ids if pinned_marker(metadata.get(post_id, {}))}
        timestamps = {str(post["id"]): utc_timestamp(post.get("time")) for post in page}
        ordinary = [post for post in page if str(post["id"]) not in pins]
        newest = max(
            ordinary or page, key=lambda post: timestamps[str(post["id"])] or "",
        )
        current = previous["cursor"] if not ordinary and previous.get("cursor") else str(newest["id"])
        previous_time = previous.get("cursor_time") or timestamps.get(str(old_id))
        high_water_time = max(timestamps.get(current) or "", previous_time or "") or None
        seen = previous.get("seen", [])
        pending = set(previous.get("pending_keywords", []))
        watch_keywords = EventType.KEYWORD_MATCHED in self.options.types
        if not watch_keywords:
            pending.clear()
        elif getattr(page, "complete", True):
            pending.intersection_update(raw_ids)
        snapshot = {
            "cursor": current, "seen": recent_ids((str(post["id"]) for post in page), seen, 200),
            "cursor_time": high_water_time,
        }
        events = []
        if old_id:
            legacy_pin = revision == 0 and str(old_id) in pins
            if str(old_id) in raw_ids and (legacy_pin or str(old_id) not in pins):
                end = raw_ids.index(str(old_id))
            else:
                end = next((index for index, post_id in enumerate(raw_ids) if post_id in seen and post_id not in pins), len(raw_ids))
                if end == len(raw_ids):
                    logger.warning(f"{self.uid}: old_id {old_id} not found on first page, checking all {len(page)} items")
            candidate_ids = set(raw_ids[:end]) - pins
            for post in reversed(page):
                post_id = str(post["id"])
                raw = metadata.get(post_id, {})
                posted_at = timestamps[post_id]
                older = posted_at and previous_time and posted_at < previous_time and not legacy_pin
                newer = posted_at and previous_time and posted_at > previous_time and not legacy_pin
                is_new = post_id not in seen and not older and (post_id in candidate_ids or newer)
                if is_new:
                    event = self._new_post_event(post, raw)
                    if event:
                        events.append(event)
                if watch_keywords and (is_new or post_id in pending):
                    match, retry = await self._keyword_event(post, raw)
                    if match:
                        events.append(match)
                    if retry:
                        pending.add(post_id)
                    else:
                        pending.discard(post_id)
        if pending:
            snapshot["pending_keywords"] = sorted(pending)
        if snapshot == previous and not events:
            return
        baseline = None
        if not old_id:
            baseline = (current, newest["screen_name"])
        await self.repository.save_event_scan(
            self.uid, "timeline", revision, snapshot, events, legacy_baseline=baseline,
        )

    def _new_post_event(self, post, raw):
        post_id = str(post["id"])
        kind = EventType.POST_REPOSTED if reposted(raw) else EventType.POST_CREATED
        if kind not in self.options.types:
            return None
        data = {"post": {**post, "id": post_id}, "url": post_url(self.uid, post_id)}
        if kind == EventType.POST_REPOSTED:
            original = raw["retweeted_status"]
            data["original_post_id"] = identifier(original.get("idstr") or original.get("id")) or None
        return self.event(kind, "post", post_id, TIMELINE_PATH, data, post.get("time"))

    async def _keyword_event(self, post, raw):
        post_id = str(post["id"])
        text = post["text"]
        if raw.get("isLongText"):
            text = await self.client.get_long_text(raw.get("mblogid") or post_id)
            if text is None:
                return None, True
        matched = [word for word in self.options.keywords if word.casefold() in text.casefold()]
        if not matched:
            return None, False
        data = {
            "post": {**post, "id": post_id, "text": text},
            "url": post_url(self.uid, post_id), "keywords": matched,
        }
        return self.event(EventType.KEYWORD_MATCHED, "post", post_id, TIMELINE_PATH, data, post.get("time")), False

    async def scan_activity(self):
        types = self.options.types
        if EventType.PROFILE_UPDATED in types:
            await self._scan_profile()
        if types & FOLLOWING_TYPES:
            await self._scan_following()
        if EventType.POST_LIKED in types:
            await self._scan_likes()
        if EventType.COMMENT_CREATED in types:
            for post_id in self.options.comment_post_ids:
                await self._scan_comments(post_id)

    async def _scan_post_changes(self, page, stream, revision, previous):
        metadata = getattr(page, "metadata", {})
        visible_ids = set(getattr(page, "raw_ids", [post["id"] for post in page]))
        posts = {
            post_id: before for post_id, before in previous.get("posts", {}).items()
            if post_id in visible_ids
        }
        events = []
        watch_updates = EventType.POST_UPDATED in self.options.types
        watch_deletions = EventType.POST_DELETED in self.options.types
        for post in page:
            post_id = post["id"]
            raw = metadata.get(post_id, {})
            before = previous.get("posts", {}).get(post_id, {})
            visibility = object_or_empty(raw.get("visible")).get("type")
            current = {"public": visibility in (0, 7, "0", "7")}
            if watch_updates:
                trigger = [post["text"], raw.get("edit_count"), bool(raw.get("isLongText"))]
                text = post["text"]
                if trigger == before.get("trigger") and (not raw.get("isLongText") or raw.get("edit_count") is not None):
                    text = object_or_empty(before.get("content")).get("text")
                elif raw.get("isLongText"):
                    text = await self.client.get_long_text(raw.get("mblogid") or post_id)
                if text is not None:
                    current.update(trigger=trigger, content=content_snapshot(raw, text))
                if "content" not in current:
                    current.update({key: before[key] for key in ("content", "trigger") if key in before})
                if "content" in before and "content" in current and before["content"] != current["content"]:
                    events.append(self.event(
                        EventType.POST_UPDATED, "post", post_id, TIMELINE_PATH,
                        {"before": before["content"], "after": current["content"], "url": post_url(self.uid, post_id)},
                        raw.get("edit_at"),
                    ))
            posts[post_id] = current

        candidates = {
            post_id: counts for post_id, counts in previous.get("candidates", {}).items()
            if post_id not in visible_ids
        }
        if watch_deletions and getattr(page, "complete", True):
            for post_id, before in previous.get("posts", {}).items():
                if post_id not in visible_ids and before.get("public"):
                    candidates.setdefault(post_id, {"checks": 0, "missing": 0})
            for post_id in list(candidates)[:3]:
                counts = candidates.pop(post_id)
                result = await self.client.get_visibility(post_id)
                missing = counts["missing"] + 1 if result == "not_found" else 0
                if missing >= 2:
                    events.append(self.event(
                        EventType.POST_DELETED, "post", post_id, STATUS_PATH,
                        {"confirmations": missing, "url": post_url(self.uid, post_id)},
                    ))
                elif result in ("not_found", "unknown") and counts["checks"] < 4:
                    candidates[post_id] = {"checks": counts["checks"] + 1, "missing": missing}
        elif not watch_deletions:
            candidates.clear()
        else:
            # A malformed page can update known posts but cannot establish absence.
            posts = {**previous.get("posts", {}), **posts}
        await self.repository.save_event_scan(
            self.uid, stream, revision, {"posts": posts, "candidates": dict(list(candidates.items())[:40])}, events,
        )

    async def _scan_profile(self):
        stream = f"profile:{self.viewer}"
        revision, previous = await self.repository.get_event_snapshot(self.uid, stream)
        profile = await self.client.get_profile(self.uid)
        if profile is None:
            return
        changes = [field for field in profile if previous.get(field) != profile[field]]
        events = []
        if revision and changes:
            events.append(self.event(
                EventType.PROFILE_UPDATED, "user", self.uid, PROFILE_PATH,
                {"before": previous, "after": profile, "fields": changes, "url": f"https://weibo.com/u/{self.uid}"},
            ))
        await self.repository.save_event_scan(self.uid, stream, revision, profile, events)

    async def _scan_pin(self, page, stream, revision, previous):
        metadata = getattr(page, "metadata", {})
        flags = {post_id: pinned_marker(raw) for post_id, raw in metadata.items()} if getattr(page, "complete", True) else {}
        pins = [post_id for post_id, flag in flags.items() if flag]
        desktop_unpinned = bool(flags) and all(flag is False for flag in flags.values())
        source = TIMELINE_PATH
        if len(pins) == 1:
            current = pins[0]
        elif desktop_unpinned and previous.get("source") != MOBILE_TIMELINE_PATH:
            current = None
        else:
            known, current = await self.client.get_mobile_pin(self.uid)
            if known:
                source = MOBILE_TIMELINE_PATH
            elif desktop_unpinned:
                current = None
            else:
                return
        old = previous.get("post_id")
        events = []
        if revision and old != current:
            events.append(self.event(
                EventType.POST_PINNED, "user", self.uid, source,
                {"previous_post_id": old, "post_id": current,
                 "url": post_url(self.uid, current) if current else f"https://weibo.com/u/{self.uid}"},
            ))
        await self.repository.save_event_scan(self.uid, stream, revision, {"post_id": current, "source": source}, events)

    async def _scan_following(self):
        stream = f"following:{self.viewer}"
        revision, previous = await self.repository.get_event_snapshot(self.uid, stream)
        current = await self.client.get_following(self.uid, self.options.following_page_limit)
        if current is None:
            if previous.get("missing"):
                await self.repository.save_event_scan(self.uid, stream, revision, {**previous, "missing": []}, [])
            return
        before = previous.get("users", {})
        missing = before.keys() - current.keys()
        removed = missing & set(previous.get("missing", []))
        events = []
        if revision:
            for kind, differences, records in (
                (EventType.FOLLOWING_ADDED, current.keys() - before.keys(), current),
                (EventType.FOLLOWING_REMOVED, removed, before),
            ):
                if kind in self.options.types:
                    for uid in sorted(differences):
                        events.append(self.event(
                            kind, "user", uid, FOLLOWING_PATH,
                            {"user": records[uid], "url": f"https://weibo.com/u/{uid}"},
                        ))
        confirmed = {**current, **{uid: before[uid] for uid in sorted(missing - removed)}}
        await self.repository.save_event_scan(
            self.uid, stream, revision, {"users": confirmed, "missing": sorted(missing - removed)}, events,
        )

    async def _scan_likes(self):
        stream = f"likes:{self.viewer}"
        revision, previous = await self.repository.get_event_snapshot(self.uid, stream)
        rows = await self.client.get_likes(self.uid, self.options.activity_page_limit)
        if not rows:
            return
        ids = [post["id"] for post in rows]
        old_window = set(previous.get("window", []))
        seen = previous.get("seen", [])
        end = next((index for index, post_id in enumerate(ids) if post_id in old_window), 0)
        events = [
            self.event(EventType.POST_LIKED, "post", post["id"], LIKES_PATH, {"post": post, "url": post["url"]})
            for post in reversed(rows[:end]) if post["id"] not in seen
        ]
        await self.repository.save_event_scan(
            self.uid, stream, revision, {"window": ids, "seen": recent_ids(ids, seen, 2000)}, events,
        )

    async def _scan_comments(self, post_id):
        stream = f"comments:{self.viewer}:{post_id}"
        revision, previous = await self.repository.get_event_snapshot(self.uid, stream)
        since = previous.get("since") or datetime.now(UTC).isoformat()
        comments = await self.client.get_comments(post_id, self.options.activity_page_limit)
        if comments is None:
            return
        seen = set(previous.get("seen", []))
        new_comments = {}
        for comment in comments:
            raw_time = utc_timestamp(comment["time"])
            if comment["author_id"] != self.uid or not raw_time:
                continue
            if datetime.fromisoformat(raw_time) >= datetime.fromisoformat(since):
                new_comments[comment["id"]] = comment
        events = []
        if revision:
            for comment_id, comment in new_comments.items():
                if comment_id not in seen:
                    events.append(self.event(
                        EventType.COMMENT_CREATED, "comment", comment_id, COMMENTS_PATH,
                        {"post_id": post_id, "comment": comment, "url": post_url("", post_id)}, comment["time"],
                    ))
        await self.repository.save_event_scan(
            self.uid, stream, revision,
            {"since": since, "seen": recent_ids(new_comments, previous.get("seen", []), 2000)}, events,
        )
