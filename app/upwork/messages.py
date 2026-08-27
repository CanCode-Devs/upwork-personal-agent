from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, selectinload

from app.db.models import ChatMessage, MessageDraft, MessageRoom, utcnow
from app.job_display import relative_ago
from app.models import ChatAttachment, ChatMessageView, ChatReplyIntent, ChatThreadCard, ReplyIntent, display_intent_label
from app.upwork.mcp_client import UpworkMcpClient, format_mcp_error, oauth_needs_login
from app.upwork.outcomes import job_id_for_room

logger = logging.getLogger(__name__)

_UNTRUSTED = re.compile(r"</?untrusted_participant_content>\s*", re.IGNORECASE)
_YOU_PREFIX = re.compile(r"^you:\s*", re.IGNORECASE)
_MD_BOLD = re.compile(r"\*\*(.*?)\*\*")
_LONG_BODY = 600


def room_initials(name: str) -> str:
    parts = [part for part in re.split(r"[\s,]+", (name or "").strip()) if part]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return f"{parts[0][0]}{parts[1][0]}".upper()


def avatar_hue(room_id: str) -> int:
    return sum(ord(ch) for ch in room_id) % 360


def _day_label(when: datetime | None, now: datetime) -> str:
    if when is None:
        return ""
    stamp = when.astimezone(now.tzinfo)
    today = now.date()
    day = stamp.date()
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return stamp.strftime("%b %d, %Y").replace(" 0", " ")


def _clean_system_body(text: str) -> str:
    return _MD_BOLD.sub(r"\1", text or "").strip()


def strip_untrusted(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value).strip()
    return _UNTRUSTED.sub("", value).strip()


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _pick(data: dict[str, Any], *names: str) -> Any:
    lower = {k.lower().replace("-", "_"): v for k, v in data.items()}
    for name in names:
        key = name.lower().replace("-", "_")
        if key in lower and lower[key] not in (None, ""):
            return lower[key]
    return None


def parse_epoch(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        stamp = value
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return stamp
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    if number > 1_000_000_000_000:
        number /= 1000.0
    try:
        return datetime.fromtimestamp(number, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def rooms_from_payload(payload: Any) -> tuple[list[dict[str, Any]], str, bool]:
    data = payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        data = payload["data"]
    blob = _as_dict(data)
    raw_rooms = blob.get("rooms")
    if not isinstance(raw_rooms, list):
        raw_rooms = blob.get("items") if isinstance(blob.get("items"), list) else []
    rooms = [item for item in raw_rooms if isinstance(item, dict)]
    cursor = ""
    if isinstance(payload, dict):
        cursor = str(payload.get("next_cursor") or payload.get("cursor") or "")
        more = bool(payload.get("hasMore") or payload.get("has_more"))
    else:
        more = False
    if not cursor:
        cursor = str(blob.get("next_cursor") or "")
    if not more:
        more = bool(blob.get("hasMore") or blob.get("has_more"))
    return rooms, cursor, more


def stories_from_payload(payload: Any) -> list[dict[str, Any]]:
    data = payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        data = payload["data"]
    blob = _as_dict(data)
    stories = blob.get("roomStories") or blob.get("room_stories") or blob.get("messages")
    if isinstance(stories, dict):
        edges = stories.get("edges")
        if isinstance(edges, list):
            nodes: list[dict[str, Any]] = []
            for edge in edges:
                node = _as_dict(edge).get("node") if isinstance(edge, dict) else None
                if isinstance(node, dict):
                    nodes.append(node)
                elif isinstance(edge, dict) and edge.get("id"):
                    nodes.append(edge)
            return nodes
    if isinstance(stories, list):
        return [item for item in stories if isinstance(item, dict)]
    return []


def attachments_from_story(node: dict[str, Any]) -> list[ChatAttachment]:
    found: list[ChatAttachment] = []
    raw = node.get("attachments") or node.get("fileAttachments") or node.get("files") or []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return found
    for item in raw:
        data = _as_dict(item)
        name = strip_untrusted(_pick(data, "file_name", "filename", "name", "title") or "")
        url = str(_pick(data, "url", "downloadUrl", "href") or "")
        if name or url:
            att: ChatAttachment = {"name": name or "file"}
            if url:
                att["url"] = url
            found.append(att)
    return found


def is_system_story(node: dict[str, Any], body: str) -> bool:
    verb = str(node.get("actionVerb") or "").strip().lower()
    lower = (body or "").lower()
    if verb in {"ended", "created", "hired", "changed"}:
        return True
    if verb.startswith("lost") or "requested payment" in verb:
        return True
    if lower.startswith("system event:"):
        return True
    if lower in {"contract ended", "offer withdrawn"}:
        return True
    return False


def classify_sender(
    node: dict[str, Any],
    own_story_id: str,
    own_bodies: set[str],
    client_story_id: str = "",
) -> str:
    body = strip_untrusted(_pick(node, "message", "body", "text") or "")
    if is_system_story(node, body):
        return "system"
    story_id = str(_pick(node, "id", "story_id") or "")
    if own_story_id and story_id == own_story_id:
        return "you"
    if client_story_id and story_id == client_story_id:
        return "client"
    if body and body in own_bodies:
        return "you"
    for key in ("isOwn", "is_own", "ownMessage", "isCurrentUser"):
        if node.get(key) is True:
            return "you"
    user = _as_dict(node.get("user") or node.get("userInfo") or node.get("sender"))
    if user.get("isCurrentUser") or user.get("self") or user.get("isOwn"):
        return "you"
    verb = str(node.get("actionVerb") or "").strip().lower()
    if verb == "sent":
        return "you"
    return ""


def latest_story_hint(room: dict[str, Any]) -> tuple[str, str, str]:
    latest = _as_dict(room.get("latestStory") or room.get("latest_story"))
    story_id = str(latest.get("id") or "")
    raw = strip_untrusted(latest.get("message") or "")
    if _YOU_PREFIX.match(raw):
        return story_id, _YOU_PREFIX.sub("", raw).strip(), "you"
    match = re.match(r"^([^:]{1,80}):\s*(.*)$", raw, re.S)
    if match:
        speaker = match.group(1).strip()
        rest = match.group(2).strip()
        if speaker.lower() != "you" and rest:
            return story_id, rest, "client"
    return "", "", ""


def fill_sender_gaps(senders: list[str]) -> list[str]:
    out = list(senders)
    you_at = [index for index, sender in enumerate(out) if sender == "you"]
    for start, end in zip(you_at, you_at[1:]):
        previous = "you"
        for index in range(start + 1, end):
            current = out[index]
            if current == "system":
                continue
            if current in {"you", "client"}:
                previous = current
                continue
            previous = "client" if previous == "you" else "you"
            out[index] = previous
    return [sender or "client" for sender in out]


def latest_own_hint(room: dict[str, Any]) -> tuple[str, str]:
    story_id, body, who = latest_story_hint(room)
    if who == "you":
        return story_id, body
    return "", ""


def room_context(room: dict[str, Any]) -> tuple[str, str | None]:
    room_type = str(_pick(room, "roomType", "room_type", "type") or "").lower()
    contract_id = _pick(room, "contractId", "contract_id")
    proposal_id = _pick(room, "proposalId", "proposal_id")
    job_id = _pick(room, "jobPostingId", "job_posting_id", "jobId")
    if contract_id:
        return "contract", str(contract_id)
    if proposal_id:
        return "proposal", str(proposal_id)
    if job_id:
        return "job", str(job_id)
    if room_type in {"interview", "one_on_one", "group", "proposal", "contract"}:
        return room_type, None
    return room_type or "other", None


def first_message_blocked(detail: str) -> bool:
    lower = (detail or "").lower()
    return (
        "client must contact" in lower
        or "client needs to message" in lower
        or "cannot initiate a proposal room" in lower
        or "send the first message" in lower
        or "no room exists" in lower
    )


def upsert_room(db: Session, room: dict[str, Any]) -> MessageRoom:
    room_id = str(_pick(room, "id", "room_id") or "")
    if not room_id:
        raise ValueError("room missing id")
    row = db.query(MessageRoom).filter(MessageRoom.room_id == room_id).one_or_none()
    if row is None:
        row = MessageRoom(room_id=room_id)
        db.add(row)
    title = strip_untrusted(_pick(room, "roomName", "room_name", "title", "name") or "")
    context_type, context_id = room_context(room)
    latest = _as_dict(room.get("latestStory") or room.get("latest_story"))
    snippet = strip_untrusted(latest.get("message") or row.snippet)
    snippet = _YOU_PREFIX.sub("You: ", snippet)
    unread = _pick(room, "numUnread", "num_unread", "unread")
    try:
        unread_n = int(unread or 0)
    except (TypeError, ValueError):
        unread_n = 0
    last_at = parse_epoch(_pick(room, "lastActivity", "last_activity") or latest.get("created") or latest.get("createdDateTime"))
    row.title = title or row.title or room_id
    row.counterpart = title.split(",")[0].strip() if title else row.counterpart
    row.context_type = context_type or row.context_type
    if context_id:
        row.context_id = context_id
    row.snippet = snippet[:2000]
    row.last_message_at = last_at or row.last_message_at
    row.unread = unread_n
    row.raw_json = json.dumps(room, default=str)
    row.synced_at = utcnow()
    return row


def upsert_stories(
    db: Session,
    room: MessageRoom,
    stories: list[dict[str, Any]],
    own_story_id: str,
    own_body: str,
    client_story_id: str = "",
) -> None:
    own_bodies = {own_body} if own_body else set()
    existing = {
        item.upwork_message_id: item
        for item in db.query(ChatMessage).filter(ChatMessage.room_pk == room.id).all()
    }
    pending: list[tuple[dict[str, Any], str, datetime | None, str]] = []
    for node in stories:
        story_id = str(_pick(node, "id", "story_id") or "")
        if not story_id:
            continue
        body = strip_untrusted(_pick(node, "message", "body", "text") or "")
        sent_at = parse_epoch(_pick(node, "createdDateTime", "created_date_time", "created", "updatedDateTime"))
        sender = classify_sender(node, own_story_id, own_bodies, client_story_id)
        pending.append((node, story_id, sent_at, sender))
    pending.sort(key=lambda item: (item[2] or datetime(1970, 1, 1, tzinfo=UTC), item[1]))
    filled = fill_sender_gaps([item[3] for item in pending])
    for (node, story_id, sent_at, _), sender in zip(pending, filled):
        body = strip_untrusted(_pick(node, "message", "body", "text") or "")
        row = existing.get(story_id)
        if row is None:
            row = ChatMessage(room_pk=room.id, upwork_message_id=story_id)
            db.add(row)
            existing[story_id] = row
        row.sender = sender
        row.body = body
        row.sent_at = sent_at
        extra = dict(node)
        extra["attachments"] = attachments_from_story(node)
        row.raw_json = json.dumps(extra, default=str)


async def sync_messages(mcp: UpworkMcpClient, db: Session, *, max_rooms: int = 25) -> int:
    listed = await mcp.list_message_rooms(max_rooms=max_rooms)
    count = 0
    for payload in listed:
        try:
            row = upsert_room(db, payload)
            db.flush()
            story_id, body, who = latest_story_hint(payload)
            own_id = story_id if who == "you" else ""
            own_body = body if who == "you" else ""
            client_id = story_id if who == "client" else ""
            stories = await mcp.list_room_messages(row.room_id, limit=40)
            upsert_stories(db, row, stories, own_id, own_body, client_id)
            count += 1
        except Exception:
            logger.exception("failed to sync room")
            continue
    db.commit()
    return count


def related_job_id(db: Session, room: MessageRoom) -> int | None:
    return job_id_for_room(db, room)


def message_attachments(row: ChatMessage) -> list[ChatAttachment]:
    try:
        parsed = json.loads(row.raw_json or "{}")
    except json.JSONDecodeError:
        parsed = {}
    raw = parsed.get("attachments") if isinstance(parsed, dict) else []
    if not isinstance(raw, list):
        return []
    out: list[ChatAttachment] = []
    for item in raw:
        data = _as_dict(item)
        name = str(data.get("name") or "file")
        att: ChatAttachment = {"name": name}
        if data.get("url"):
            att["url"] = str(data["url"])
        out.append(att)
    return out


def thread_card(room: MessageRoom, *, related: int | None = None) -> ChatThreadCard:
    name = room.counterpart or room.title
    return {
        "room_id": room.room_id,
        "title": room.title,
        "counterpart": name,
        "snippet": _clean_system_body(room.snippet),
        "last_ago": relative_ago(room.last_message_at) if room.last_message_at else "",
        "unread": room.unread,
        "context_type": room.context_type,
        "related_job_id": related,
        "send_status": room.send_status,
        "send_error": room.send_error or "",
        "can_send": bool(room.room_id) and room.send_status != "sending",
        "client_first_required": False,
        "suggested_text": room.draft.suggested_text if room.draft else "",
        "suggested_intents": draft_intents(room.draft),
        "initials": room_initials(name),
        "avatar_hue": avatar_hue(room.room_id),
    }


def message_views(rows: list[ChatMessage]) -> list[ChatMessageView]:
    ordered = sorted(rows, key=lambda item: (item.sent_at or datetime(1970, 1, 1, tzinfo=UTC), item.id))
    now = datetime.now().astimezone()
    views: list[ChatMessageView] = []
    last_day = ""
    last_sender = ""
    last_at: datetime | None = None
    for item in ordered:
        body = _clean_system_body(item.body) if item.sender == "system" else item.body
        day = _day_label(item.sent_at, now)
        show_day = bool(day) and day != last_day
        grouped = False
        if (
            not show_day
            and last_sender == item.sender
            and item.sent_at is not None
            and last_at is not None
        ):
            delta = abs((item.sent_at - last_at).total_seconds())
            grouped = delta <= 300
        views.append(
            {
                "story_id": item.upwork_message_id,
                "sender": item.sender,
                "body": body,
                "sent_at": item.sent_at,
                "sent_ago": relative_ago(item.sent_at) if item.sent_at else "",
                "attachments": message_attachments(item),
                "day_label": day,
                "show_day": show_day,
                "grouped": grouped,
                "is_long": len(body or "") > _LONG_BODY,
                "show_time": True,
            }
        )
        last_day = day
        last_sender = item.sender
        last_at = item.sent_at
    for index, view in enumerate(views):
        nxt = views[index + 1] if index + 1 < len(views) else None
        view["show_time"] = not bool(nxt and nxt.get("grouped"))
    return views


def draft_intents(draft: MessageDraft | None) -> list[ChatReplyIntent]:
    if draft is None:
        return []
    try:
        raw = json.loads(draft.suggested_intents or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    out: list[ChatReplyIntent] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        kind = str(item.get("kind") or "").strip()
        if not text or not kind:
            continue
        label = display_intent_label(kind, str(item.get("label") or ""))
        out.append({"kind": kind, "label": label, "text": text})
    return out


def save_suggested_draft(
    db: Session,
    room: MessageRoom,
    text: str,
    intents: list[ReplyIntent] | list[ChatReplyIntent] | None = None,
) -> MessageDraft:
    draft = db.query(MessageDraft).filter(MessageDraft.room_pk == room.id).one_or_none()
    if draft is None:
        draft = MessageDraft(room_pk=room.id)
        db.add(draft)
    draft.suggested_text = text
    payload: list[dict[str, str]] = []
    for item in intents or []:
        if isinstance(item, ReplyIntent):
            payload.append({"kind": item.kind.value, "label": item.label, "text": item.text})
        else:
            payload.append({"kind": item["kind"], "label": item["label"], "text": item["text"]})
    draft.suggested_intents = json.dumps(payload, ensure_ascii=False)
    draft.created_at = utcnow()
    return draft


async def refresh_room(mcp: UpworkMcpClient, db: Session, room: MessageRoom) -> None:
    try:
        stories = await mcp.list_room_messages(room.room_id, limit=40)
    except Exception as exc:
        detail = format_mcp_error(exc)
        if oauth_needs_login(detail):
            raise
        return
    hint_room: dict[str, Any] = {}
    try:
        hint_room = json.loads(room.raw_json or "{}")
    except json.JSONDecodeError:
        hint_room = {}
    if not isinstance(hint_room, dict):
        hint_room = {}
    story_id, body, who = latest_story_hint(hint_room)
    own_id = story_id if who == "you" else ""
    own_body = body if who == "you" else ""
    client_id = story_id if who == "client" else ""
    upsert_stories(db, room, stories, own_id, own_body, client_id)
    if stories:
        newest = max(stories, key=lambda item: str(item.get("createdDateTime") or ""))
        room.snippet = strip_untrusted(newest.get("message") or room.snippet)[:2000]
        room.last_message_at = parse_epoch(newest.get("createdDateTime")) or room.last_message_at
    room.synced_at = utcnow()
    db.add(room)
    db.commit()


def load_rooms(db: Session) -> list[MessageRoom]:
    return (
        db.query(MessageRoom)
        .options(selectinload(MessageRoom.draft))
        .order_by(MessageRoom.last_message_at.desc().nullslast(), MessageRoom.id.desc())
        .all()
    )
