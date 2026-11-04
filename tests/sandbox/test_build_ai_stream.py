# SPDX-License-Identifier: GPL-3.0-or-later
"""The agent output stream: log summaries and the model-failure signals."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_helpers  # noqa: E402,F401 - puts server/ on sys.path

from agino_store_sandbox import build_ai_stream as S  # noqa: E402


class Sink:
    full = False

    def __init__(self):
        self.lines = []

    def note(self, text):
        self.lines.append(text)


def event(**fields):
    return (json.dumps(fields) + "\n").encode()


class StreamCase(unittest.TestCase):
    def setUp(self):
        self.sink = Sink()
        self.stream = S.AiStream(self.sink)

    def feed(self, *chunks):
        for chunk in chunks:
            self.stream.feed(chunk)
        self.stream.finish()
        return self.sink.lines


class SummaryTests(StreamCase):
    def test_event_types_and_text_only(self):
        lines = self.feed(
            event(type="system", subtype="init", model="agino", tools=["Bash", "Read"], api_key="SECRETKEY"),
            event(type="assistant", message={"content": [{"type": "text", "text": "I will build it"},
                                                         {"type": "tool_use", "name": "Bash",
                                                          "input": {"command": "make -j8", "env": {"X": "BODY"}}}]}),
            event(type="user", message={"content": [{"type": "tool_result", "content": "ok\nline2"}]}),
            event(type="result", subtype="success", is_error=False, num_turns=7, result="done"))
        self.assertEqual(lines, ["system/init", "assistant: I will build it", "assistant: tool_use Bash make -j8",
                                 "user: tool_result ok line2", "result/success is_error=False turns=7: done"])

    def test_nothing_else_reaches_the_log(self):
        lines = self.feed(event(type="system", subtype="init", api_key="SECRETKEY", request={"body": "BODYTEXT"}),
                          event(type="assistant", message={"content": [
                              {"type": "thinking", "thinking": "PRIVATETHOUGHT"},
                              {"type": "tool_use", "name": "Write", "input": {"content": "FILEBODY"}}]}))
        text = "\n".join(lines)
        for secret in ("SECRETKEY", "BODYTEXT", "PRIVATETHOUGHT", "FILEBODY"):
            self.assertNotIn(secret, text)
        self.assertIn("assistant: thinking", text)
        self.assertIn("tool_use Write", text)

    def test_text_is_cut_cleaned_and_one_line(self):
        long = "x" * 1000
        lines = self.feed(event(type="assistant", message={"content": [
            {"type": "text", "text": "red \x1b[31mtext\x1b[0m\x00 a\r\nb‮" + long}]}))
        self.assertEqual(len(lines), 1)
        for bad in ("\x1b", "\x00", "\r", "\n", "‮"):
            self.assertNotIn(bad, lines[0])
        self.assertLessEqual(len(lines[0]), len("assistant: ") + S.TEXT_LIMIT)
        self.assertTrue(lines[0].endswith("…"))

    def test_lines_that_are_not_json_are_short_text(self):
        lines = self.feed(b"Error: something broke\x1b[0m\n", b"{not json\n", b"[1, 2]\n", b'"str"\n', b"\n   \n")
        self.assertEqual(lines, ["text: Error: something broke[0m", "text: {not json", "text: [1, 2]",
                                 'text: "str"'])

    def test_codex_style_events(self):
        lines = self.feed(event(type="item.completed", item={"type": "agent_message", "text": "hello"}),
                          event(type="item.started", item={"type": "command_execution", "command": "ls"}),
                          event(type="turn.completed", usage={"input_tokens": 5}),
                          event(type="error", message="boom"))
        self.assertEqual(lines, ["item.completed agent_message: hello", "item.started command_execution: ls",
                                 "turn.completed", "error: boom"])

    def test_odd_shapes_do_not_crash(self):
        lines = self.feed(event(type=5), event(type={"a": 1}), event(), event(type="assistant", message=5),
                          event(type="assistant", message={"content": [1, None, {"type": 7}, {"type": "text"}]}),
                          event(type="user", message={"content": {"x": 1}}),
                          event(type="assistant", message={"content": [{"type": "text", "text": "t"}] * 50}))
        self.assertGreaterEqual(len(lines), 5)
        self.assertLessEqual(sum(l == "assistant: t" for l in lines), S.MAX_BLOCKS)

    def test_deeply_nested_json_is_just_text(self):
        lines = self.feed(b'{"a":' * 100000 + b"\n", b"[" * 100000 + b"\n")
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.startswith("text: ") for line in lines))


class ChunkingTests(StreamCase):
    def test_lines_split_across_chunks_and_multibyte_characters(self):
        data = event(type="assistant", message={"content": [{"type": "text", "text": "grüße"}]})
        pieces = [data[i:i + 5] for i in range(0, len(data), 5)]
        self.assertEqual(self.feed(*pieces), ["assistant: grüße"])

    def test_a_last_line_without_a_newline_is_flushed_by_finish(self):
        self.stream.feed(b'{"type": "system", "subtype": "init"}')
        self.assertEqual(self.sink.lines, [])
        self.stream.finish()
        self.assertEqual(self.sink.lines, ["system/init"])

    def test_an_endless_line_is_dropped_with_its_tail(self):
        self.stream.feed(b"a" * (S.MAX_LINE + 10))
        self.stream.feed(b"b" * 50 + b"\n")
        self.stream.feed(event(type="system", subtype="init"))
        self.stream.finish()
        self.assertEqual(self.sink.lines, ["[an over-long output line was dropped]", "system/init"])

    def test_full_is_never_set(self):
        self.assertFalse(self.stream.full)


class ModelSignalTests(StreamCase):
    def retry(self, status, **extra):
        return event(type="system", subtype="api_retry", attempt=1, max_retries=5, error_status=status,
                     error="rate_limit", **extra)

    def test_model_statuses(self):
        for status in (401, 402, 429, 500, 502, 503, 529, 599):
            stream = S.AiStream(Sink())
            stream.feed(self.retry(status))
            self.assertTrue(stream.model_failure, status)
            self.assertEqual(stream.retry_statuses, {status})

    def test_other_statuses_are_not_model_failures(self):
        for status in (None, 400, 403, 404, 408, 413, 499, 600, "429", True, 4.5):
            stream = S.AiStream(Sink())
            stream.feed(self.retry(status))
            self.assertFalse(stream.model_failure, status)

    def test_the_retry_line_in_the_log_has_the_status_only(self):
        lines = self.feed(self.retry(529, request_body={"messages": "BODYTEXT"}))
        self.assertEqual(lines, ["system/api_retry attempt=1/5 status=529 error=rate_limit"])

    def test_an_assistant_message_after_a_retry_clears_the_signal(self):
        self.stream.feed(self.retry(429))
        self.assertTrue(self.stream.model_failure)
        self.stream.feed(event(type="assistant", message={"content": [{"type": "text", "text": "back"}]}))
        self.assertFalse(self.stream.model_failure)
        self.assertEqual(self.stream.retry_statuses, {429})
        self.stream.feed(self.retry(503))
        self.assertTrue(self.stream.model_failure)

    def test_the_proxy_paused_marker_in_any_output(self):
        self.stream.feed(b'{"error": {"type": "proxy_paused"}}\n')
        self.assertTrue(self.stream.model_failure)
        self.assertTrue(self.stream.proxy_paused)
        other = S.AiStream(Sink())
        other.feed(b"API Error: 503 proxy_paused\n")
        self.assertTrue(other.model_failure)

    def test_the_marker_is_found_even_in_an_over_long_summary_or_bad_json(self):
        self.stream.feed(b"{broken proxy_paused\n")
        self.assertTrue(self.stream.model_failure)

    def test_plain_output_has_no_signal(self):
        self.feed(event(type="assistant", message={"content": [{"type": "text", "text": "hi"}]}))
        self.assertFalse(self.stream.model_failure)
        self.assertEqual(self.stream.retry_statuses, set())

    def test_the_signal_survives_a_full_log(self):
        class FullSink(Sink):
            full = True

            def note(self, text):
                pass
        stream = S.AiStream(FullSink())
        stream.feed(self.retry(503))
        self.assertTrue(stream.model_failure)


if __name__ == "__main__":
    unittest.main()
