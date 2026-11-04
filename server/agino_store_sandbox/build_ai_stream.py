# SPDX-License-Identifier: GPL-3.0-or-later
"""The output of an agent CLI as the build log sees it (docs/sandbox.md section 10.7).

The CLIs print one JSON event per line (Claude Code `--output-format stream-json`, Codex `exec --json`). Only the
event type and a short text summary reach build.log; request bodies, tool payloads and thinking never do. The
text is guest data: control characters are removed, every field is cut to a fixed length, and a line that is
not JSON is shown as a short text line.

The stream also remembers what decides the failure class `model`: an `api_retry` event whose `error_status` is
401, 402, 429 or 5xx, and the marker `proxy_paused` that the model proxy puts in its 503 answer. Such a signal
counts only while no assistant message has arrived after it (the model answered again), and the job uses it
only when the CLI finally exited with an error.
"""

import codecs
import json

from .build_out import clean_log_text

MAX_LINE = 1 << 20
TEXT_LIMIT = 300
SHORT_LIMIT = 120
MAX_BLOCKS = 20
MODEL_MARKER = "proxy_paused"


def is_model_status(status):
    return isinstance(status, int) and not isinstance(status, bool) and (status in (401, 402, 429)
                                                                       or 500 <= status <= 599)


def _text(value, limit=TEXT_LIMIT):
    """One line of at most `limit` characters from any guest value."""
    if isinstance(value, (dict, list)) or value is None:
        return ""
    text = " ".join(clean_log_text(str(value)).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _label(obj):
    kind = _text(obj.get("type"), 40) or "event"
    sub = _text(obj.get("subtype"), 40)
    return kind + ("/" + sub if sub else "")


def _block_lines(label, blocks):
    """Summaries of the content blocks of an assistant/user message."""
    out = []
    if isinstance(blocks, str):
        return ["%s: %s" % (label, _text(blocks))] if _text(blocks) else []
    if not isinstance(blocks, list):
        return out
    for block in blocks[:MAX_BLOCKS]:
        if not isinstance(block, dict):
            continue
        kind = _text(block.get("type"), 30)
        if kind == "text":
            text = _text(block.get("text"))
            if text:
                out.append("%s: %s" % (label, text))
        elif kind == "tool_use":
            args = block.get("input") if isinstance(block.get("input"), dict) else {}
            hint = next((_text(args[k], SHORT_LIMIT) for k in ("command", "file_path", "path", "url", "pattern")
                         if k in args and _text(args[k], SHORT_LIMIT)), "")
            out.append("%s: tool_use %s%s" % (label, _text(block.get("name"), 40), " " + hint if hint else ""))
        elif kind == "tool_result":
            content = block.get("content")
            if isinstance(content, list):
                content = " ".join(_text(c.get("text"), SHORT_LIMIT) for c in content[:3] if isinstance(c, dict))
            out.append("%s: tool_result %s" % (label, _text(content, SHORT_LIMIT)))
        elif kind:
            out.append("%s: %s" % (label, kind))  # thinking and anything else: the type only, never the content
    return out


def summarize_event(obj):
    """(log lines, retry status or None) for one parsed event."""
    label = _label(obj)
    sub = obj.get("subtype")
    if label == "api_retry" or sub == "api_retry":
        status = obj.get("error_status")
        shown = _text(status, 10)
        line = "%s attempt=%s/%s status=%s error=%s" % (label, _text(obj.get("attempt"), 6),
                                                        _text(obj.get("max_retries"), 6), shown or "-",
                                                        _text(obj.get("error"), 40) or "-")
        return [line], status if is_model_status(status) else None
    kind = obj.get("type")
    message = obj.get("message")
    if kind in ("assistant", "user") and isinstance(message, dict):
        return _block_lines(kind, message.get("content")) or [label], None
    if kind == "result":
        line = "%s is_error=%s turns=%s" % (label, _text(obj.get("is_error"), 6), _text(obj.get("num_turns"), 6))
        text = _text(obj.get("result"))
        return [line + (": " + text if text else "")], None
    item = obj.get("item")
    if isinstance(item, dict):  # Codex: item.started / item.completed
        detail = _text(item.get("text") or item.get("command") or item.get("message"), TEXT_LIMIT)
        return ["%s %s%s" % (label, _text(item.get("type"), 40), ": " + detail if detail else "")], None
    detail = _text(obj.get("message") or obj.get("error") or obj.get("result"), TEXT_LIMIT)
    return [label + (": " + detail if detail else "")], None


class AiStream:
    """Sink for GuestBuild._pump / exec_logged: lines of CLI output in, summaries into the real log sink out.

    `full` is always False: even when build.log hit its size cap the stream keeps being read, because the
    failure classification needs the events."""

    full = False

    def __init__(self, sink):
        self.sink = sink
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buf = ""
        self._skipping = False
        self.retry_statuses = set()
        self.proxy_paused = False
        self.pending_model_error = False
        self.lines = 0

    def note(self, text):
        self.sink.note(text)

    def feed(self, data):
        self._buf += self._decoder.decode(data)
        while True:
            line, sep, rest = self._buf.partition("\n")
            if not sep:
                break
            self._buf = rest
            if self._skipping:
                self._skipping = False  # the tail of an over-long line
            else:
                self._line(line)
        if len(self._buf) > MAX_LINE:  # a line without an end: drop what we have and its tail
            self._buf, self._skipping = "", True
            self.sink.note("[an over-long output line was dropped]")

    def finish(self):
        """The end of the output: a last line without a newline."""
        self._buf += self._decoder.decode(b"", final=True)
        if self._buf and not self._skipping:
            self._line(self._buf)
        self._buf = ""

    @property
    def model_failure(self):
        """True when the last thing the CLI saw from the model service was an error."""
        return self.pending_model_error

    def _line(self, raw):
        self.lines += 1
        if MODEL_MARKER in raw:
            self.proxy_paused = self.pending_model_error = True
        stripped = raw.strip()
        if not stripped:
            return
        obj = None
        if stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
            except (ValueError, RecursionError):
                obj = None
        if isinstance(obj, dict):
            lines, status = summarize_event(obj)
            if status is not None:
                self.retry_statuses.add(status)
                self.pending_model_error = True
            elif obj.get("type") == "assistant":
                self.pending_model_error = False
        else:
            lines = ["text: " + _text(stripped)]
        for line in lines:
            self.sink.note(line)
