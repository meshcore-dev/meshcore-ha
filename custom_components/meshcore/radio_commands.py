"""Typed mesh requests: one firmware pending-flag operation at a time."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from typing import Any, Final

from meshcore.events import Event, EventType
from meshcore.packets import BinaryReqType

DEFAULT_MESH_TIMEOUT: Final = 15.0
NEIGHBOUR_PAGE_COUNT: Final = 255
NEIGHBOUR_PAGE_MIN_TIMEOUT: Final = 25.0
SUGGESTED_TIMEOUT_SCALE: Final = 800.0


def _contact_prefix(contact: Any) -> str:
    """Return the six-byte hex prefix the firmware echoes back in replies."""
    key = contact.get("public_key", "") if isinstance(contact, dict) else contact
    return str(key or "")[:12].lower()


def _reply_deadline(
    payload: Any,
    timeout: float | None,
    min_timeout: float,
    max_timeout: float | None,
) -> float:
    """Scale the companion's suggested timeout the way the SDK does."""
    suggested = payload.get("suggested_timeout") if isinstance(payload, dict) else None
    seconds = timeout or (
        suggested / SUGGESTED_TIMEOUT_SCALE if suggested else DEFAULT_MESH_TIMEOUT
    )
    seconds = max(seconds, min_timeout)
    return min(seconds, max_timeout) if max_timeout else seconds


class MeshCommands:
    """Mesh requests for RadioSession, which owns the link they run on.

    Every request here sets a firmware pending flag, so only one may be in
    flight per radio: the lease is held from the send until the reply lands or
    the wait expires.
    """

    _mesh_lease: asyncio.Semaphore
    exchange: Callable[..., Any]
    subscribe: Callable[..., Callable[[], None]]

    def _commands(self) -> Any:
        """Return the live SDK command surface, or refuse when the link is down."""
        raise NotImplementedError

    async def login(
        self,
        contact: Any,
        password: str,
        *,
        timeout: float | None = None,
        min_timeout: float = 0.0,
    ) -> Event | None:
        """Log in to a remote node; None for a refusal, rejection or silence.

        Uses the SDK's raw sender: its public wrapper only adds a deprecation
        warning for the sync helper this replaces.
        """
        commands = self._commands()
        _sent, answer = await self._mesh_request(
            partial(commands._send_login_raw, contact, password),
            (EventType.LOGIN_SUCCESS, EventType.LOGIN_FAILED),
            identity=_contact_prefix(contact),
            allow_unidentified=True,
            timeout=timeout,
            min_timeout=min_timeout,
        )
        if answer is None or answer.type is not EventType.LOGIN_SUCCESS:
            return None
        return answer

    async def req_status(
        self, contact: Any, *, timeout: float | None = None, min_timeout: float = 0.0
    ) -> Event | None:
        """Ask a remote node for its status frame."""
        commands = self._commands()
        _sent, answer = await self._mesh_request(
            partial(
                commands.send_binary_req,
                contact,
                BinaryReqType.STATUS,
                timeout=timeout or 0,
                min_timeout=min_timeout,
            ),
            (EventType.STATUS_RESPONSE,),
            identity=_contact_prefix(contact),
            timeout=timeout,
            min_timeout=min_timeout,
        )
        return answer

    async def req_telemetry(
        self, contact: Any, *, timeout: float | None = None, min_timeout: float = 0.0
    ) -> Event | None:
        """Ask a remote node for its telemetry frame."""
        commands = self._commands()
        _sent, answer = await self._mesh_request(
            partial(
                commands.send_binary_req,
                contact,
                BinaryReqType.TELEMETRY,
                timeout=timeout or 0,
                min_timeout=min_timeout,
            ),
            (EventType.TELEMETRY_RESPONSE,),
            identity=_contact_prefix(contact),
            timeout=timeout,
            min_timeout=min_timeout,
        )
        return answer

    async def req_neighbours(
        self,
        contact: Any,
        offset: int,
        count: int = NEIGHBOUR_PAGE_COUNT,
        *,
        pubkey_prefix_length: int = 4,
        min_timeout: float = 0.0,
    ) -> Event | None:
        """Ask a remote node for one page of its neighbour table."""
        commands = self._commands()
        _sent, answer = await self._mesh_request(
            partial(
                commands.req_neighbours_async,
                contact,
                count=count,
                offset=offset,
                pubkey_prefix_length=pubkey_prefix_length,
                timeout=0,
                min_timeout=min_timeout,
            ),
            (EventType.NEIGHBOURS_RESPONSE,),
            identity=_contact_prefix(contact),
            min_timeout=min_timeout,
        )
        return answer

    async def path_discovery(
        self,
        contact: Any,
        *,
        identity: str | None = None,
        min_timeout: float = 0.0,
        max_timeout: float | None = None,
    ) -> tuple[Any, Event | None]:
        """Ask the mesh to rediscover a contact's path.

        Returns the companion's local reply and the PATH_RESPONSE it produced;
        callers need both to tell a rejected request from an unanswered one.
        """
        commands = self._commands()
        destination = bytes.fromhex(str(contact.get("public_key") or ""))[:32]
        return await self._mesh_request(
            partial(
                commands.send,
                b"\x34\x00" + destination,
                [EventType.MSG_SENT, EventType.ERROR],
            ),
            (EventType.PATH_RESPONSE,),
            identity_key="pubkey_pre",
            identity=identity or _contact_prefix(contact),
            min_timeout=min_timeout,
            max_timeout=max_timeout,
        )

    async def fetch_neighbours(
        self,
        contact: Any,
        *,
        pubkey_prefix_length: int = 4,
        max_pages: int = 32,
        page_cb: Callable[[int], Any] | None = None,
    ) -> dict[str, Any] | None:
        """Page a node's neighbour table under HA-owned bounds.

        Keeps the legacy shape: two consecutive silent pages return what has
        been collected so far. Paging also stops when a page adds nothing, and
        restarts once from the beginning when the node reports a new total.
        ``page_cb`` sees each page number before it is requested and stops the
        scan by returning False.
        """
        first = await self.req_neighbours(
            contact, 0, pubkey_prefix_length=pubkey_prefix_length
        )
        if first is None:
            return None

        result = dict(first.payload)
        result.pop("tag", None)
        result["neighbours"] = list(result.get("neighbours") or [])
        total = result.get("neighbours_count", 0)
        collected = result.get("results_count", 0)
        failures = 0
        restarted = False

        for page in range(1, max_pages):
            if collected >= total:
                break
            if page_cb is not None and page_cb(page) is False:
                break
            answer = await self.req_neighbours(
                contact,
                collected,
                pubkey_prefix_length=pubkey_prefix_length,
                min_timeout=NEIGHBOUR_PAGE_MIN_TIMEOUT,
            )
            if answer is None:
                if failures:
                    break
                failures = 1
                continue

            failures = 0
            payload = answer.payload
            reported_total = payload.get("neighbours_count", total)
            if reported_total != total:
                if restarted:
                    break
                restarted = True
                total = reported_total
                collected = 0
                result["neighbours_count"] = reported_total
                result["results_count"] = 0
                result["neighbours"] = []
                continue

            added = payload.get("results_count", 0)
            if added <= 0:
                break
            collected += added
            result["results_count"] = collected
            result["neighbours"] += payload.get("neighbours") or []

        return result

    async def _mesh_request(
        self,
        send: Callable[[], Any],
        event_types: tuple[EventType, ...],
        *,
        identity: str,
        identity_key: str = "pubkey_prefix",
        allow_unidentified: bool = False,
        timeout: float | None = None,
        min_timeout: float = 0.0,
        max_timeout: float | None = None,
    ) -> tuple[Any, Event | None]:
        """Own one firmware pending-flag request from send to reply.

        The waiter is armed before the send and refined with the tag the
        companion reports, so a reply to another request cannot satisfy it.
        Frames that carry no identity are accepted only for callers that ask
        for it (a short LOGIN_SUCCESS) or when the wire carries no tag at all.
        """
        async with self._mesh_lease:
            answer: asyncio.Future[Event] = asyncio.get_running_loop().create_future()
            expected: dict[str, str | None] = {"tag": None}

            def resolve(event: Event) -> None:
                """Accept the first frame that belongs to this request."""
                if answer.done():
                    return
                attributes = getattr(event, "attributes", None) or {}
                reported = attributes.get(identity_key)
                if reported != identity and not (reported is None and allow_unidentified):
                    return
                tag = attributes.get("tag")
                if expected["tag"] is not None and tag is not None and tag != expected["tag"]:
                    return
                answer.set_result(event)

            unsubscribes = [self.subscribe(event_type, resolve) for event_type in event_types]
            try:
                sent = await self.exchange(send)
                if sent is None or getattr(sent, "type", None) is EventType.ERROR:
                    return sent, None
                payload = getattr(sent, "payload", None) or {}
                ack = payload.get("expected_ack") if isinstance(payload, dict) else None
                expected["tag"] = ack.hex() if isinstance(ack, (bytes, bytearray)) else None
                deadline = _reply_deadline(payload, timeout, min_timeout, max_timeout)
                try:
                    return sent, await asyncio.wait_for(answer, deadline)
                except TimeoutError:
                    return sent, None
            finally:
                for unsubscribe in unsubscribes:
                    unsubscribe()

