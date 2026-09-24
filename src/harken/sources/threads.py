"""Threads posts via Meta's keyword-search API with conversation context."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from harken.models import ConversationPost, Mention
from harken.sources.base import FetchPage, Source

_API = "https://graph.threads.net/keyword_search"
_GRAPH = "https://graph.threads.net"
_BASIC_FIELDS = "id,text,username,permalink,timestamp"
_RELATION_FIELDS = _BASIC_FIELDS + ",has_replies,is_reply,is_reply_owned_by_me,root_post,replied_to"
_REPLY_FIELDS = (
    "id,text,username,permalink,timestamp,has_replies,is_reply,"
    "is_reply_owned_by_me,root_post,replied_to"
)


class ThreadsSource(Source):
    name = "threads"
    label = "Threads"
    needs_config = True

    def __init__(self, access_token: str | None = None, **options):
        super().__init__(**options)
        self.access_token = access_token

    def fetch(self, query: str, limit: int = 50) -> list[Mention]:
        return self.fetch_page(query, limit=limit).mentions

    def fetch_page(
        self,
        query: str,
        limit: int = 50,
        *,
        cursor: str | None = None,
        since: datetime | None = None,
    ) -> FetchPage:
        if not self.access_token:
            raise RuntimeError("Threads requires HARKEN_THREADS_ACCESS_TOKEN")

        params = {
            "q": query,
            "search_type": "RECENT",
            "search_mode": "KEYWORD",
            "fields": _RELATION_FIELDS,
            "limit": min(limit, 50),
        }
        if cursor:
            params["after"] = cursor
        if since:
            params["since"] = _rfc3339(since)

        # Keep the OAuth token out of the URL so it cannot leak into logs.
        headers = {"Authorization": f"Bearer {self.access_token}"}
        with self._client(headers=headers) as client:
            response = client.get(_API, params=params)
            # Older/limited keyword-search surfaces may reject reply-only
            # relationship fields. Keep keyword discovery working, then ask
            # the media-object endpoint for relation metadata per result.
            relation_fallback = response.status_code in {400, 403}
            if relation_fallback:
                params["fields"] = _BASIC_FIELDS
                response = client.get(_API, params=params)
            response.raise_for_status()
            data = response.json()

            mentions: list[Mention] = []
            for post in data.get("data", []):
                post_id = str(post.get("id") or "").strip()

                # Keyword search can return HTTP 200 while omitting relationship
                # fields. Ask the media-object endpoint once for canonical
                # relation metadata before deciding whether the hit is safe to
                # classify.
                needs_relation_detail = (
                    relation_fallback
                    or "is_reply" not in post
                    or (post.get("is_reply") is True and not _relation_id(post.get("root_post")))
                )
                if needs_relation_detail and post_id:
                    detail = _fetch_reply_detail(client, post_id)
                    if detail:
                        post = {**post, **detail}

                # Meta currently sometimes returns is_reply=true without the
                # documented root_post/replied_to IDs, even with
                # threads_read_replies. Never treat such an isolated reply as
                # the source lead: its author may be only a participant in a
                # different person's commercial-intent conversation.
                if "is_reply" not in post:
                    continue
                if post.get("is_reply") is True and not _relation_id(post.get("root_post")):
                    continue

                mention = _mention_with_context(client, query, post)
                if mention is not None:
                    mentions.append(mention)

        paging = data.get("paging") or {}
        cursors = paging.get("cursors") or {}
        return FetchPage(mentions, cursors.get("after"))


def _mention_with_context(
    client: httpx.Client,
    query: str,
    post: dict,
) -> Mention | None:
    post_id = str(post.get("id") or "").strip()
    if not post_id:
        return None

    root_id = _relation_id(post.get("root_post")) if post.get("is_reply") else post_id
    if not root_id:
        return _mention_from_post(query, post)

    # A keyword hit on a reply should be evaluated as the source conversation,
    # not as an isolated reply. Fetch the root and the full flattened reply list.
    if post.get("is_reply"):
        root = _fetch_thread(client, root_id)
        if root is None:
            return _mention_from_post(query, post)
        replies = _fetch_conversation(client, root_id)
        conversation = _conversation_tree(root, replies, matched_id=post_id, matched_post=post)
        mention = _mention_from_post(query, root)
        if mention is not None:
            mention.conversation = conversation
        return mention

    mention = _mention_from_post(query, post)
    if mention is not None and post.get("has_replies"):
        replies = _fetch_conversation(client, post_id)
        if replies:
            mention.conversation = _conversation_tree(
                post,
                replies,
                matched_id=post_id,
                matched_post=post,
            )
    return mention


def _fetch_reply_detail(client: httpx.Client, thread_id: str) -> dict | None:
    response = client.get(
        f"{_GRAPH}/{thread_id}",
        params={"fields": _REPLY_FIELDS},
    )
    if response.status_code in {400, 403, 404}:
        return None
    response.raise_for_status()
    value = response.json()
    return value if isinstance(value, dict) else None


def _fetch_thread(client: httpx.Client, thread_id: str) -> dict | None:
    response = client.get(
        f"{_GRAPH}/{thread_id}",
        params={"fields": _BASIC_FIELDS + ",has_replies"},
    )
    if response.status_code in {400, 403, 404}:
        return None
    response.raise_for_status()
    value = response.json()
    return value if isinstance(value, dict) else None


def _fetch_conversation(client: httpx.Client, root_id: str) -> list[dict]:
    response = client.get(
        f"{_GRAPH}/{root_id}/conversation",
        params={
            "fields": _REPLY_FIELDS,
            "reverse": "false",
            "limit": 50,
        },
    )
    # Missing threads_read_replies or an unavailable conversation must not
    # destroy keyword discovery. The CLI separately reports token scope health.
    if response.status_code in {400, 403, 404}:
        return []
    response.raise_for_status()
    value = response.json()
    data = value.get("data", []) if isinstance(value, dict) else []
    return [item for item in data if isinstance(item, dict)]


def _conversation_tree(
    root: dict,
    replies: list[dict],
    *,
    matched_id: str,
    matched_post: dict,
) -> list[ConversationPost]:
    root_id = str(root.get("id") or "").strip()
    if not root_id:
        return []

    by_id: dict[str, dict] = {root_id: root}
    for item in replies:
        item_id = str(item.get("id") or "").strip()
        if item_id:
            by_id[item_id] = item

    # If Meta did not return the matched reply in /conversation, preserve it so
    # the report still shows why the root conversation matched the keyword.
    if matched_id and matched_id not in by_id:
        by_id[matched_id] = matched_post

    depth_cache = {root_id: 0}

    def depth_for(item_id: str, seen: set[str] | None = None) -> int:
        if item_id in depth_cache:
            return depth_cache[item_id]
        seen = set() if seen is None else set(seen)
        if item_id in seen:
            return 1
        seen.add(item_id)
        item = by_id.get(item_id) or {}
        parent_id = _relation_id(item.get("replied_to"))
        if not parent_id or parent_id == item_id:
            depth = 1
        elif parent_id == root_id:
            depth = 1
        elif parent_id in by_id:
            depth = min(depth_for(parent_id, seen) + 1, 8)
        else:
            depth = 1
        depth_cache[item_id] = depth
        return depth

    ordered = [root]
    ordered.extend(
        sorted(
            (item for item_id, item in by_id.items() if item_id != root_id),
            key=lambda item: _parse_datetime(item.get("timestamp")),
        )
    )

    conversation: list[ConversationPost] = []
    for item in ordered:
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            continue
        conversation.append(
            ConversationPost(
                id=item_id,
                author=item.get("username"),
                text=item.get("text", ""),
                url=item.get("permalink"),
                created_at=_parse_datetime(item.get("timestamp")),
                reply_to_id=_relation_id(item.get("replied_to")),
                depth=depth_for(item_id),
                matched=item_id == matched_id,
            )
        )
    return conversation


def _mention_from_post(query: str, post: dict) -> Mention | None:
    post_id = str(post.get("id") or "").strip()
    if not post_id:
        return None
    return Mention(
        source="threads",
        query=query,
        author=post.get("username"),
        text=post.get("text", ""),
        url=post.get("permalink"),
        created_at=_parse_datetime(post.get("timestamp")),
    )


def _relation_id(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    identifier = str(value.get("id") or "").strip()
    return identifier or None


def _parse_datetime(value: str | None) -> datetime:
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
