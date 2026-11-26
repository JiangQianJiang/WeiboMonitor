import asyncio
from urllib.parse import urlencode

import aiohttp
from loguru import logger

DESKTOP = "https://weibo.com"
TIMELINE_ORIGIN = "https://www.weibo.com"
MOBILE = "https://m.weibo.cn"
TIMELINE_PATH = "/ajax/statuses/mymblog"
PROFILE_PATH = "/ajax/profile/info"
FOLLOWING_PATH = "/ajax/friendships/friends"
LIKES_PATH = "/ajax/statuses/likelist"
COMMENTS_PATH = "/ajax/statuses/buildComments"
STATUS_PATH = "/statuses/show"
MOBILE_TIMELINE_PATH = "/api/container/getIndex"


def object_or_empty(value):
    return value if isinstance(value, dict) else {}


def text_or_empty(value):
    return value if isinstance(value, str) else ""


def identifier(value):
    return str(value) if type(value) in (int, str) and value else ""


def platform_integer(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 20:
        return int(value)
    return None


def pinned_marker(post):
    for key in ("isTop", "is_top"):
        if post.get(key) in (0, 1, "0", "1") and post.get(key) is not None:
            return post[key] in (1, "1")
    if post.get("mblogtype") in (2, "2") or object_or_empty(post.get("title")).get("text") == "置顶":
        return True
    return None


class PageResult(list):
    """A usable timeline page with raw IDs for boundary detection."""

    def __init__(self, items, raw_ids=None, metadata=None, *, complete=True):
        super().__init__(items)
        self.raw_ids = raw_ids if raw_ids is not None else [str(item["id"]) for item in items]
        self.metadata = metadata if metadata is not None else {}
        self.complete = complete


class WeiboMonitor:
    def __init__(self, session: aiohttp.ClientSession, cookie: str = ""):
        self.session = session
        self.headers = {
            "Referer": "https://weibo.com/",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        if cookie:
            self.headers["Cookie"] = cookie

    async def _get(self, path, params, *, mobile=False):
        if mobile:
            origin = MOBILE
            headers = {"Referer": MOBILE + "/", "Accept": "application/json"}
        else:
            origin = TIMELINE_ORIGIN if path == TIMELINE_PATH else DESKTOP
            headers = self.headers
        try:
            async with self.session.get(
                f"{origin}{path}?{urlencode(params)}", headers=headers, timeout=5,
                allow_redirects=False,
            ) as response:
                response.raise_for_status()
                if not 200 <= response.status < 300:
                    logger.warning(f"微博接口重定向 {path} (HTTP {response.status})")
                    return None
                payload = await response.json()
                return payload if isinstance(payload, dict) else None
        except aiohttp.ClientResponseError as error:
            logger.warning(f"微博接口响应错误 {path} (HTTP {error.status})")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            logger.warning(f"微博接口请求失败 {path} ({type(error).__name__})")
        return None

    async def get_weibo_list(self, uid):
        payload = await self._get(TIMELINE_PATH, {"uid": uid, "page": 1, "feature": 0})
        if payload is None or payload.get("ok", 1) != 1:
            return []
        rows = object_or_empty(payload.get("data")).get("list")
        if not isinstance(rows, list):
            return []
        raw_ids, result, metadata = [], [], {}
        for row in rows:
            post = object_or_empty(row)
            post_id = identifier(post.get("id"))
            if not post_id:
                continue
            raw_ids.append(post_id)
            metadata[post_id] = post
            if not isinstance(post.get("text_raw"), str):
                logger.warning(f"跳过缺少有效 text_raw 的微博条目 {uid}")
                continue
            result.append({
                "text": post["text_raw"],
                "id": post_id,
                "time": post.get("created_at", ""),
                "screen_name": text_or_empty(object_or_empty(post.get("user")).get("screen_name")),
                "source": text_or_empty(post.get("source")),
                "region_name": text_or_empty(post.get("region_name")),
            })
        return PageResult(result, raw_ids, metadata, complete=len(metadata) == len(rows))

    async def get_profile(self, uid):
        payload = await self._get(PROFILE_PATH, {"uid": uid})
        if not payload or payload.get("ok") != 1:
            return None
        user = object_or_empty(object_or_empty(payload.get("data")).get("user"))
        if identifier(user.get("idstr") or user.get("id")) != uid:
            return None
        fields = {
            "screen_name": user.get("screen_name"),
            "avatar": user.get("avatar_hd") or user.get("avatar_large") or user.get("profile_image_url"),
            "description": user.get("description"),
        }
        if not all(isinstance(value, str) for value in fields.values()):
            return None
        fields["avatar"] = fields["avatar"].split("?", 1)[0].split("#", 1)[0]
        return fields

    async def get_following(self, uid, page_limit):
        """Return a complete, unfiltered collection or None."""
        users, cursors = {}, set()
        expected_total = None
        for page in range(1, page_limit + 1):
            payload = await self._get(FOLLOWING_PATH, {"uid": uid, "page": page})
            if not payload or payload.get("ok") != 1:
                return None
            rows = payload.get("users")
            total = platform_integer(payload.get("total_number"))
            cursor = platform_integer(payload.get("next_cursor"))
            filtered = payload.get("has_filtered_attentions")
            if (not isinstance(rows, list) or total is None or total < 0 or
                    cursor is None or cursor < 0 or filtered not in (False, 0)):
                return None
            if expected_total is None:
                expected_total = total
            if expected_total != total:
                return None
            for row in rows:
                row = object_or_empty(row)
                row_id = identifier(row.get("idstr") or row.get("id"))
                if not row_id or row_id in users:
                    return None
                users[row_id] = {"id": row_id, "screen_name": text_or_empty(row.get("screen_name"))}
            if cursor == 0:
                return users if len(users) == total else None
            if not rows or cursor in cursors or len(users) >= total:
                return None
            cursors.add(cursor)
        return None

    async def get_likes(self, uid, page_limit):
        rows = []
        seen_ids = set()
        for page in range(1, page_limit + 1):
            payload = await self._get(LIKES_PATH, {"uid": uid, "relate": "fans", "page": page})
            if not payload or payload.get("ok") != 1:
                return None
            batch = object_or_empty(payload.get("data")).get("list")
            if not isinstance(batch, list):
                return None
            if not batch:
                break
            for raw in batch:
                item = object_or_empty(raw)
                post_id = identifier(item.get("idstr") or item.get("id"))
                if not post_id or post_id in seen_ids:
                    return None
                seen_ids.add(post_id)
                user = object_or_empty(item.get("user"))
                author_id = identifier(user.get("idstr") or user.get("id"))
                rows.append({
                    "id": post_id,
                    "author_id": author_id,
                    "text": text_or_empty(item.get("text_raw")),
                    "url": post_url(author_id, post_id),
                })
        return rows

    async def get_comments(self, post_id, page_limit):
        rows, cursors = [], set()
        params = {"id": post_id, "is_reload": 1, "is_show_bulletin": 2, "count": 20}
        for _ in range(page_limit):
            payload = await self._get(COMMENTS_PATH, params)
            if not payload or payload.get("ok") != 1 or not isinstance(payload.get("data"), list):
                return None
            for raw in payload["data"]:
                item = object_or_empty(raw)
                user = object_or_empty(item.get("user"))
                comment_id = identifier(item.get("idstr") or item.get("id"))
                author_id = identifier(user.get("idstr") or user.get("id"))
                if not comment_id or not author_id:
                    return None
                rows.append({
                    "id": comment_id, "author_id": author_id, "time": item.get("created_at"),
                    "text": text_or_empty(item.get("text_raw") or item.get("text")),
                })
            cursor = platform_integer(payload.get("max_id"))
            if not cursor:
                break
            if cursor in cursors or not payload["data"]:
                return None
            cursors.add(cursor)
            params["max_id"] = cursor
        return rows

    async def get_long_text(self, post_id):
        payload = await self._get("/ajax/statuses/longtext", {"id": post_id})
        if not payload or payload.get("ok") != 1:
            return None
        text = object_or_empty(payload.get("data")).get("longTextContent_raw")
        return text if isinstance(text, str) else None

    async def get_visibility(self, post_id):
        payload = await self._get(STATUS_PATH, {"id": post_id}, mobile=True)
        if not payload:
            return "unknown"
        if payload.get("ok") == 1 and isinstance(payload.get("data"), dict):
            return "visible"
        error = identifier(payload.get("errno"))
        message = payload.get("msg")
        if error == "20112" or message == "暂无查看权限":
            return "restricted"
        if error == "20101" or message == "该微博不存在":
            return "not_found"
        return "unknown"

    async def get_mobile_pin(self, uid):
        """Use the mobile timeline's explicit pin card when the PC shape is ambiguous."""
        payload = await self._get(MOBILE_TIMELINE_PATH, {
            "type": "uid", "value": uid, "containerid": f"107603{uid}",
        }, mobile=True)
        if not payload or payload.get("ok") != 1:
            return False, None
        cards = object_or_empty(payload.get("data")).get("cards")
        if not isinstance(cards, list):
            return False, None
        owned_posts = []
        for card in cards:
            if not isinstance(card, dict):
                return False, None
            if "mblog" not in card and card.get("card_type") not in (9, "9"):
                continue
            post = object_or_empty(card.get("mblog"))
            author = object_or_empty(post.get("user"))
            author_id = identifier(author.get("idstr") or author.get("id"))
            if not author_id or not identifier(post.get("id")):
                return False, None
            if author_id == uid:
                owned_posts.append(post)
        if not owned_posts:
            return False, None
        pins = [identifier(post.get("id")) for post in owned_posts if pinned_marker(post)]
        return (len(pins) <= 1, pins[0] if len(pins) == 1 else None)


def post_url(author_id, post_id):
    return f"https://weibo.com/{author_id}/{post_id}" if author_id else f"https://weibo.com/detail/{post_id}"
