"""Rich message sending, with a safe fallback.

Telethon speaks MTProto. `sendRichMessage` (Bot API 10.1, June 2026) only
exists on the HTTP Bot API, so there is no Telethon method for it. A bot token
is valid on both transports at once, so the approach here is:

    1. Build Rich HTML and POST it to api.telegram.org/bot<token>/sendRichMessage.
    2. If Telegram rejects it (older server, unsupported chat, malformed block),
       downgrade the same content to classic HTML and send it through Telethon.

The fallback matters. Rich messages are new enough that assuming they always
land would make the bot silently useless on any chat that does not support
them. Every user-facing send in this project goes through `send_rich`, so the
degradation is handled in exactly one place.

Rich HTML supports a wider tag set than classic Telegram HTML: headings,
tables, lists, <details>, <mark>, <hr> and friends. `to_classic_html` strips
those down to the handful of tags MTProto accepts, keeping the text readable
rather than dumping raw markup on the user.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any, Iterable, Sequence

import httpx
from telethon.tl.custom import Button

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"

# Telegram caps rich messages far above the classic 4096 limit.
RICH_TEXT_LIMIT = 32768
CLASSIC_TEXT_LIMIT = 4096


def esc(value: Any) -> str:
    """Escape a value for inclusion in message HTML."""

    return html.escape("" if value is None else str(value), quote=False)


# ---------------------------------------------------------------------------
# Rich HTML builder
# ---------------------------------------------------------------------------


class RichDoc:
    """Small builder for Rich HTML documents.

    Using a builder rather than f-strings scattered through the handlers keeps
    the markup valid and makes the fallback path predictable.
    """

    def __init__(self) -> None:
        self._parts: list[str] = []

    # -- block level ------------------------------------------------------

    def heading(self, text: str, level: int = 3) -> "RichDoc":
        level = max(1, min(6, level))
        self._parts.append(f"<h{level}>{esc(text)}</h{level}>")
        return self

    def para(self, html_text: str) -> "RichDoc":
        self._parts.append(f"<p>{html_text}</p>")
        return self

    def text(self, plain: str) -> "RichDoc":
        return self.para(esc(plain))

    def divider(self) -> "RichDoc":
        self._parts.append("<hr>")
        return self

    def bullets(self, items: Iterable[str]) -> "RichDoc":
        rows = "".join(f"<li>{item}</li>" for item in items)
        if rows:
            self._parts.append(f"<ul>{rows}</ul>")
        return self

    def numbered(self, items: Iterable[str]) -> "RichDoc":
        rows = "".join(f"<li>{item}</li>" for item in items)
        if rows:
            self._parts.append(f"<ol>{rows}</ol>")
        return self

    def quote(self, html_text: str) -> "RichDoc":
        self._parts.append(f"<blockquote>{html_text}</blockquote>")
        return self

    def details(self, summary: str, html_text: str) -> "RichDoc":
        self._parts.append(
            f"<details><summary>{esc(summary)}</summary>{html_text}</details>"
        )
        return self

    def code(self, body: str, language: str = "") -> "RichDoc":
        cls = f' class="language-{esc(language)}"' if language else ""
        self._parts.append(f"<pre><code{cls}>{esc(body)}</code></pre>")
        return self

    def table(
        self,
        headers: Sequence[str],
        rows: Sequence[Sequence[str]],
        bordered: bool = True,
        striped: bool = True,
    ) -> "RichDoc":
        if not rows:
            return self
        attrs = ""
        if bordered:
            attrs += " bordered"
        if striped:
            attrs += " striped"
        head = "".join(f"<th>{esc(h)}</th>" for h in headers)
        body = "".join(
            "<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>"
            for row in rows
        )
        self._parts.append(
            f"<table{attrs}><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        )
        return self

    def raw(self, html_text: str) -> "RichDoc":
        self._parts.append(html_text)
        return self

    # -- output -----------------------------------------------------------

    def to_html(self) -> str:
        return "".join(self._parts)

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.to_html()


# -- inline helpers ---------------------------------------------------------


def b(text: str) -> str:
    return f"<b>{esc(text)}</b>"


def i(text: str) -> str:
    return f"<i>{esc(text)}</i>"


def code(text: str) -> str:
    return f"<code>{esc(text)}</code>"


def mark(text: str) -> str:
    return f"<mark>{esc(text)}</mark>"


def link(text: str, url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{esc(text)}</a>'


# ---------------------------------------------------------------------------
# Rich HTML -> classic HTML downgrade
# ---------------------------------------------------------------------------

_LIST_ITEM_RE = re.compile(r"<li>(.*?)</li>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _rows_to_text(table_html: str) -> str:
    """Flatten a table into aligned-ish plain lines for the classic fallback."""

    lines: list[str] = []
    for row in re.findall(r"<tr>(.*?)</tr>", table_html, re.S):
        cells = [
            _TAG_RE.sub("", cell).strip()
            for cell in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S)
        ]
        cells = [c for c in cells if c]
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def to_classic_html(rich_html: str) -> str:
    """Reduce Rich HTML to the tag set MTProto/classic Bot API accepts.

    Classic Telegram HTML allows b, i, u, s, code, pre, a, blockquote and
    spoiler. Everything else is converted to text or newlines so the message
    still reads correctly instead of leaking tags.
    """

    out = rich_html

    # Tables become plain aligned lines before any other tag stripping.
    out = re.sub(
        r"<table[^>]*>(.*?)</table>",
        lambda m: "\n" + _rows_to_text(m.group(1)) + "\n",
        out,
        flags=re.S,
    )

    # Headings become bold lines.
    out = re.sub(r"<h[1-6][^>]*>(.*?)</h[1-6]>", r"\n<b>\1</b>\n", out, flags=re.S)

    # List items become bullets. Ordered lists lose their numbering, which is
    # an acceptable trade for a fallback that should rarely fire.
    out = re.sub(
        r"<ol[^>]*>(.*?)</ol>",
        lambda m: "\n" + _LIST_ITEM_RE.sub(r"• \1\n", m.group(1)),
        out,
        flags=re.S,
    )
    out = re.sub(
        r"<ul[^>]*>(.*?)</ul>",
        lambda m: "\n" + _LIST_ITEM_RE.sub(r"• \1\n", m.group(1)),
        out,
        flags=re.S,
    )

    # <details> keeps its summary as a bold lead-in.
    out = re.sub(
        r"<details[^>]*><summary>(.*?)</summary>(.*?)</details>",
        r"\n<b>\1</b>\n\2\n",
        out,
        flags=re.S,
    )

    out = re.sub(r"</?p[^>]*>", "\n", out)
    out = re.sub(r"<hr\s*/?>", "\n------------------------------\n", out)
    out = re.sub(r"<br\s*/?>", "\n", out)
    out = re.sub(r"</?(mark|sub|sup|summary|details|thead|tbody|tr|th|td|li|ul|ol)[^>]*>", "", out)

    # Collapse the whitespace the substitutions above introduce.
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n\n[message truncated]"


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def _button_payload(btn: Any) -> dict[str, Any] | None:
    """Translate one Telethon button into its Bot API equivalent.

    Telethon has moved this around between versions. On 1.45 a
    `KeyboardInlineButton` keeps its text at the top level and its payload on a
    nested `type` object (`InlineButtonTypeCallback.data`,
    `InlineButtonTypeUrl.url`). Older builds put `data`/`url` directly on the
    button, and `Button.inline(...)` sometimes wraps the real thing in
    `.button`. All three layouts are handled, because getting this wrong means
    silently dropping every keyboard.
    """

    inner = getattr(btn, "button", btn)

    text = getattr(inner, "text", None)
    if not text:
        return None

    # Look on the button itself first, then on its nested type object.
    sources = [inner]
    type_obj = getattr(inner, "type", None)
    if type_obj is not None and not isinstance(type_obj, str):
        sources.append(type_obj)

    for source in sources:
        url = getattr(source, "url", None)
        if url:
            return {"text": text, "url": url}

    for source in sources:
        data = getattr(source, "data", None)
        if data is not None:
            payload = data.decode() if isinstance(data, (bytes, bytearray)) else str(data)
            return {"text": text, "callback_data": payload}

    return None


def _buttons_to_markup(buttons: Any) -> dict[str, Any] | None:
    """Convert Telethon Button objects into Bot API inline keyboard JSON.

    Only the button kinds this bot actually uses are translated: callback
    buttons and URL buttons.
    """

    if not buttons:
        return None

    rows_in = buttons if isinstance(buttons, (list, tuple)) else [buttons]
    if rows_in and not isinstance(rows_in[0], (list, tuple)):
        rows_in = [rows_in]

    keyboard: list[list[dict[str, Any]]] = []
    for row in rows_in:
        out_row: list[dict[str, Any]] = []
        for btn in row:
            payload = _button_payload(btn)
            if payload:
                out_row.append(payload)
        if out_row:
            keyboard.append(out_row)

    return {"inline_keyboard": keyboard} if keyboard else None


class RichSender:
    """Sends rich messages, falling back to classic HTML when unavailable."""

    def __init__(self, bot_token: str, timeout: int = 30) -> None:
        self._token = bot_token
        self._client = httpx.AsyncClient(timeout=timeout)
        # Flips to False after the first definitive "method not supported"
        # response so the bot stops paying for a doomed round trip every time.
        self.rich_supported = True

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{API_ROOT}/bot{self._token}/{method}"
        response = await self._client.post(url, json=payload)
        try:
            return response.json()
        except ValueError:
            return {"ok": False, "description": f"HTTP {response.status_code}"}

    async def send(
        self,
        client: Any,
        chat_id: Any,
        doc: RichDoc | str,
        buttons: Any = None,
        reply_to: int | None = None,
        silent: bool = False,
    ) -> Any:
        """Send `doc` to `chat_id`, preferring sendRichMessage."""

        rich_html = doc.to_html() if isinstance(doc, RichDoc) else str(doc)

        if self.rich_supported:
            payload: dict[str, Any] = {
                "chat_id": _chat_id_value(chat_id),
                "rich_message": {"html": _truncate(rich_html, RICH_TEXT_LIMIT)},
            }
            markup = _buttons_to_markup(buttons)
            if markup:
                payload["reply_markup"] = markup
            if reply_to:
                payload["reply_parameters"] = {"message_id": reply_to}
            if silent:
                payload["disable_notification"] = True

            try:
                result = await self._post("sendRichMessage", payload)
                if result.get("ok"):
                    return result.get("result")

                description = str(result.get("description", ""))
                if _is_unsupported(description):
                    log.warning(
                        "sendRichMessage unavailable (%s). Using classic HTML from now on.",
                        description,
                    )
                    self.rich_supported = False
                else:
                    log.warning("sendRichMessage rejected the payload: %s", description)
            except httpx.HTTPError as exc:
                log.warning("sendRichMessage transport error: %s", exc)

        return await self.send_classic(
            client, chat_id, rich_html, buttons=buttons, reply_to=reply_to, silent=silent
        )

    async def send_classic(
        self,
        client: Any,
        chat_id: Any,
        rich_html: str,
        buttons: Any = None,
        reply_to: int | None = None,
        silent: bool = False,
    ) -> Any:
        """Send the downgraded version through Telethon."""

        text = _truncate(to_classic_html(rich_html), CLASSIC_TEXT_LIMIT)
        return await client.send_message(
            chat_id,
            text,
            parse_mode="html",
            buttons=buttons,
            reply_to=reply_to,
            silent=silent,
            link_preview=False,
        )

    async def edit(
        self,
        client: Any,
        chat_id: Any,
        message_id: int,
        doc: RichDoc | str,
        buttons: Any = None,
    ) -> Any:
        """Edit an existing message, keeping rich formatting when possible."""

        rich_html = doc.to_html() if isinstance(doc, RichDoc) else str(doc)

        if self.rich_supported:
            payload: dict[str, Any] = {
                "chat_id": _chat_id_value(chat_id),
                "message_id": message_id,
                "rich_message": {"html": _truncate(rich_html, RICH_TEXT_LIMIT)},
            }
            # An edit that drops the keyboard has to say so explicitly: an
            # absent reply_markup leaves the old buttons in place, which would
            # strand a stale keyboard on top of the new content.
            payload["reply_markup"] = _buttons_to_markup(buttons) or {"inline_keyboard": []}
            try:
                result = await self._post("editMessageText", payload)
                if result.get("ok"):
                    return result.get("result")
                description = str(result.get("description", ""))
                if "message is not modified" in description.lower():
                    return None
                if _is_unsupported(description):
                    self.rich_supported = False
            except httpx.HTTPError as exc:
                log.warning("editMessageText transport error: %s", exc)

        try:
            return await client.edit_message(
                chat_id,
                message_id,
                _truncate(to_classic_html(rich_html), CLASSIC_TEXT_LIMIT),
                parse_mode="html",
                buttons=buttons,
                link_preview=False,
            )
        except Exception as exc:  # noqa: BLE001 - editing is always best effort
            log.debug("Classic edit failed: %s", exc)
            return None


def _chat_id_value(chat_id: Any) -> Any:
    """Normalise whatever Telethon handed us into a Bot API chat_id."""

    if isinstance(chat_id, (int, str)):
        return chat_id
    for attr in ("user_id", "chat_id", "channel_id", "id"):
        value = getattr(chat_id, attr, None)
        if value is not None:
            return value
    return chat_id


def _is_unsupported(description: str) -> bool:
    lowered = description.lower()
    return any(
        marker in lowered
        for marker in (
            "method not found",
            "not supported",
            "unknown method",
            "unsupported",
        )
    )


__all__ = [
    "RichDoc",
    "RichSender",
    "Button",
    "b",
    "i",
    "code",
    "mark",
    "link",
    "esc",
    "to_classic_html",
]
