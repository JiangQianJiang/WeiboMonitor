from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from uuid import NAMESPACE_URL, uuid4, uuid5


class EventType(StrEnum):
    POST_CREATED = "post.created"
    POST_REPOSTED = "post.reposted"
    POST_UPDATED = "post.updated"
    POST_DELETED = "post.deleted"
    FOLLOWING_ADDED = "following.added"
    FOLLOWING_REMOVED = "following.removed"
    PROFILE_UPDATED = "profile.updated"
    POST_LIKED = "post.liked"
    COMMENT_CREATED = "comment.created"
    POST_PINNED = "post.pinned"
    KEYWORD_MATCHED = "keyword.matched"


TIMELINE_TYPES = frozenset((EventType.POST_CREATED, EventType.POST_REPOSTED))


def utc_timestamp(value: str | None = None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        date = datetime.fromisoformat(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if date.tzinfo is None:
        return None
    return date.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Event:
    schema_version: int
    event_id: str
    type: EventType
    account_id: str
    actor_id: str | None
    subject: dict
    occurred_at: str | None
    observed_at: str
    source: str
    data: dict

    @classmethod
    def detect(
        cls, kind: EventType, account_id: str, subject_type: str, subject_id: str,
        source: str, data: dict, *, occurred_at: str | None = None,
    ) -> "Event":
        stable_kind = "timeline" if kind in TIMELINE_TYPES else kind
        stable = kind in TIMELINE_TYPES or kind in {
            EventType.KEYWORD_MATCHED, EventType.COMMENT_CREATED, EventType.POST_LIKED,
        }
        event_id = uuid5(NAMESPACE_URL, f"weibomonitor:{stable_kind}:{account_id}:{subject_id}") if stable else uuid4()
        return cls(
            1, str(event_id), kind, account_id,
            None if kind == EventType.POST_DELETED else account_id,
            {"type": subject_type, "id": subject_id},
            utc_timestamp(occurred_at),
            datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            source, data,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "Event":
        return cls(**{**payload, "type": EventType(payload["type"])})
