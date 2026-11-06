import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

import ccmax_feishu_bot as bot


OPEN_ID = "ou_test_allowed_123"
OTHER_OPEN_ID = "ou_test_other_456"
FAKE_KEY = "sk-test-FAKE-DO-NOT-USE-0123456789"


class FakeFeishu:
    def __init__(self):
        self.texts = []
        self.files = []

    def send_text(self, open_id, text, receive_id_type="open_id"):
        self.texts.append((open_id, text, receive_id_type))

    def send_markdown_file(self, open_id, path, receive_id_type="open_id"):
        self.files.append((open_id, Path(path), receive_id_type))


class FakeWorkbench:
    def __init__(self, screen=None, passthrough=None, error=None):
        self.calls = []
        self.screen = screen if screen is not None else {
            "status": "COMPLETED",
            "items": [{"id": "s1", "title": "截图项", "status": "PASS", "actual": "ok"}],
            "budget": {"requests": 3},
        }
        self.passthrough = passthrough if passthrough is not None else {
            "status": "COMPLETED",
            "items": [{"id": "p1", "title": "透传项", "status": "PASS", "actual": "ok"}],
            "budget": {"requests_attempted": 2},
        }
        self.error = error

    def run_screenshot(self, job):
        self.calls.append(("screenshot", job))
        if self.error:
            raise self.error
        return self.screen

    def run_passthrough(self, job):
        self.calls.append(("passthrough", job))
        if self.error:
            raise self.error
        return self.passthrough


def config(**overrides):
    values = dict(
        app_id="cli_fake",
        app_secret="fake-secret",
        allowed_open_ids=frozenset({OPEN_ID}),
        web_url="http://127.0.0.1:8765",
        start_workers=False,
    )
    values.update(overrides)
    return bot.BotConfig(**values)


class BotTestBase(unittest.TestCase):
    def setUp(self):
        self.feishu = FakeFeishu()
        self.workbench = FakeWorkbench()
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.report_dir = Path(self._temp_dir.name)
        self.service = bot.BotService(
            config(), self.feishu, self.workbench, report_dir=self.report_dir
        )

    def tearDown(self):
        self.service.close()

    def command(self, message_id="m-command-1", open_id=OPEN_ID, text=None, chat_type="p2p"):
        return self.service.handle_message(
            open_id,
            message_id,
            text or f"测试 https://api.anthropic.com|{FAKE_KEY}|claude-test-model",
            chat_type,
        )


class CommandParsingTests(unittest.TestCase):
    def test_parse_both_supported_prefixes(self):
        self.assertEqual(
            bot.parse_command("测试 https://api.anthropic.com|key|model"),
            ("https://api.anthropic.com", "key", "model"),
        )
        self.assertEqual(
            bot.parse_command("/test https://api.anthropic.com|key|model"),
            ("https://api.anthropic.com", "key", "model"),
        )

    def test_parse_rejects_wrong_field_count(self):
        with self.assertRaises(bot.CommandError):
            bot.parse_command("测试 https://example.com|key")

    def test_parse_ignores_unrelated_text(self):
        self.assertIsNone(bot.parse_command("你好"))

    def test_url_validation_rejects_credentials_query_and_fragment(self):
        for value in (
            "https://user:pass@example.com",
            "https://example.com?token=x",
            "https://example.com#part",
        ):
            with self.subTest(value=value), self.assertRaises(bot.CommandError):
                bot.validate_base_url(value, allow_private=True)

    def test_key_and_model_validation(self):
        with self.assertRaises(bot.CommandError):
            bot.validate_key("has\x01control")
        with self.assertRaises(bot.CommandError):
            bot.validate_model("bad model!")


class ConfirmationGateTests(BotTestBase):
    def test_unconfirmed_command_never_calls_workbench(self):
        self.assertTrue(self.command())
        self.assertEqual(self.workbench.calls, [])
        self.assertEqual(len(self.service.pending), 1)
        self.assertTrue(any("尚未调用付费接口" in text for _, text, _ in self.feishu.texts))

    def test_confirm_runs_screenshot_then_passthrough_with_exact_budgets(self):
        self.command()
        token = next(iter(self.service.pending))
        self.service.handle_message(OPEN_ID, "m-confirm-1", f"确认 {token}")
        self.assertEqual(len(self.service.jobs.queue), 1)
        self.service._run_job(self.service.jobs.get_nowait())
        self.service.jobs.task_done()
        self.assertEqual([name for name, _ in self.workbench.calls], ["screenshot", "passthrough"])
        self.assertIn("screenshot", self.workbench.calls[0][0])
        self.assertIn("passthrough", self.workbench.calls[1][0])

    def test_wrong_user_cannot_consume_token(self):
        # Both users are allowlisted, so this specifically verifies token ownership
        # rather than merely relying on the sender allowlist.
        self.service = bot.BotService(
            config(allowed_open_ids=frozenset({OPEN_ID, OTHER_OPEN_ID})),
            self.feishu,
            self.workbench,
            report_dir=self.report_dir,
        )
        with patch.object(
            self.service,
            "_queue_reply",
            side_effect=lambda open_id, text: self.feishu.send_text(open_id, text),
        ):
            self.command()
            token = next(iter(self.service.pending))
            self.service.handle_message(OTHER_OPEN_ID, "m-other-1", f"确认 {token}")
        self.assertIn(token, self.service.pending)
        self.assertEqual(self.service.jobs.qsize(), 0)
        self.assertEqual(self.workbench.calls, [])
        self.assertTrue(any("不属于当前用户" in text for _, text, _ in self.feishu.texts))

    def test_full_mock_job_never_leaks_fake_key(self):
        self.workbench.screen = {
            "status": "COMPLETED",
            "items": [{"id": "s1", "title": "回显检查", "status": "FAIL",
                       "actual": f"server echoed {FAKE_KEY}"}],
            "budget": {"requests": 1},
        }
        with patch.object(
            self.service,
            "_queue_reply",
            side_effect=lambda open_id, text: self.feishu.send_text(open_id, text),
        ):
            self.command()
            token = next(iter(self.service.pending))
            self.service.handle_message(OPEN_ID, "m-confirm-full", f"确认 {token}")
            job = self.service.jobs.get_nowait()
            self.service._run_job(job)
            self.service.jobs.task_done()
        replies = "\n".join(text for _, text, _ in self.feishu.texts)
        self.assertNotIn(FAKE_KEY, replies)
        self.assertTrue(self.feishu.files)
        artifacts = list(self.report_dir.glob("feishu-combined-*"))
        self.assertGreaterEqual(len(artifacts), 2)
        for path in [path for _, path, _ in self.feishu.files] + artifacts:
            self.assertNotIn(FAKE_KEY, path.read_text(encoding="utf-8"))

    def test_duplicate_message_is_ignored(self):
        self.command("duplicate-id")
        first_pending = len(self.service.pending)
        self.command("duplicate-id")
        self.assertEqual(len(self.service.pending), first_pending)

    def test_unauthorized_and_group_messages_are_rejected(self):
        self.assertFalse(self.command("unauth", OTHER_OPEN_ID))
        self.assertFalse(self.command("group", OPEN_ID, "测试 https://api.anthropic.com|key|model", chat_type="group"))
        self.assertEqual(self.workbench.calls, [])


class GroupChatTests(BotTestBase):
    CHAT_ID = "oc_test_allowed_chat_123"

    def setUp(self):
        super().setUp()
        self.service.config = config(allowed_chat_ids=frozenset({self.CHAT_ID}),
                                     bot_open_id=OPEN_ID)

    def group_command(self, message_id="group-command-1", sender=OPEN_ID, chat=None,
                      mentioned=True, text=None):
        return self.service.handle_message(
            sender, message_id,
            text or f"测试 https://api.anthropic.com|{FAKE_KEY}|claude-test-model",
            "group", chat or self.CHAT_ID, mentioned,
        )

    def test_group_requires_allowlisted_chat_user_and_bot_mention(self):
        self.assertFalse(self.group_command(chat="oc_other_group_123"))
        self.assertFalse(self.group_command(mentioned=False))
        self.assertFalse(self.group_command(sender=OTHER_OPEN_ID))
        self.assertEqual(self.service.pending, {})

    def test_group_confirm_is_bound_to_originating_user_and_chat(self):
        self.assertTrue(self.group_command())
        token = next(iter(self.service.pending))
        self.service.handle_message(OPEN_ID, "wrong-chat-confirm", f"确认 {token}",
                                   "group", "oc_other_group_123", True)
        self.service.handle_message(OTHER_OPEN_ID, "wrong-user-confirm", f"确认 {token}",
                                   "group", self.CHAT_ID, True)
        self.assertIn(token, self.service.pending)
        self.service.handle_message(OPEN_ID, "right-confirm", f"确认 {token}",
                                   "group", self.CHAT_ID, True)
        job = self.service.jobs.get_nowait()
        self.assertEqual(job.chat_id, self.CHAT_ID)
        self.assertEqual(job.open_id, OPEN_ID)

    def test_group_run_and_report_are_sent_to_chat_target(self):
        self.group_command()
        token = next(iter(self.service.pending))
        self.service.handle_message(OPEN_ID, "confirm-group-run", f"确认 {token}",
                                   "group", self.CHAT_ID, True)
        job = self.service.jobs.get_nowait()
        self.service._run_job(job)
        self.assertTrue(all(t[0] == self.CHAT_ID and t[2] == "chat_id" for t in self.feishu.texts))
        self.assertTrue(self.feishu.files)
        self.assertEqual(self.feishu.files[0][0], self.CHAT_ID)
        self.assertEqual(self.feishu.files[0][2], "chat_id")


class FeishuEventParsingTests(unittest.TestCase):
    def test_extracts_group_chat_and_verifies_structured_bot_mention(self):
        mention = SimpleNamespace(
            id=SimpleNamespace(open_id=OPEN_ID), key="@_user_1", name="CCMax Bot"
        )
        message = SimpleNamespace(
            content='{"text":"@_user_1 测试 https://api.anthropic.com|k|model"}',
            message_id="evt-group-1", chat_type="group", chat_id="oc_test_allowed_chat_123",
            message_type="text", mentions=[mention],
        )
        data = SimpleNamespace(event=SimpleNamespace(
            message=message, sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=OTHER_OPEN_ID))
        ))
        fields = bot._event_fields(data, OPEN_ID)
        self.assertEqual(fields, (
            OTHER_OPEN_ID, "evt-group-1", "测试 https://api.anthropic.com|k|model",
            "group", "oc_test_allowed_chat_123", True,
        ))

    def test_ignores_non_text_events(self):
        data = SimpleNamespace(event=SimpleNamespace(
            message=SimpleNamespace(message_type="image"),
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=OPEN_ID)),
        ))
        self.assertIsNone(bot._event_fields(data, OPEN_ID))


class LocalWorkbenchClientTests(unittest.TestCase):
    def test_payloads_have_exact_authorization_and_no_redirects(self):
        calls = []

        class Response:
            status_code = 200
            def json(self):
                return {"status": "COMPLETED", "items": []}

        class Session:
            trust_env = True
            def post(self, *args, **kwargs):
                calls.append((args, kwargs))
                return Response()

        client = bot.LocalWorkbenchClient(config(), Session())
        job = bot.TestJob(OPEN_ID, "https://api.anthropic.com", FAKE_KEY,
                          "claude-test-model", "a" * 32, "2026-01-01T00:00:00+00:00")
        client.run_screenshot(job)
        client.run_passthrough(job)
        self.assertFalse(client.session.trust_env)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]["json"]["authorized_max_requests"], 56)
        self.assertEqual(calls[1][1]["json"]["authorized_max_requests"], 10)
        self.assertTrue(calls[0][1]["json"]["allow_paid_requests"])
        self.assertTrue(calls[1][1]["json"]["allow_paid_requests"])
        self.assertFalse(calls[0][1]["allow_redirects"])
        self.assertFalse(calls[1][1]["allow_redirects"])
        self.assertEqual(calls[0][1]["json"]["protocol"], "anthropic-messages")

    def test_http_error_redacts_fake_key(self):
        class Response:
            status_code = 500

            def json(self):
                return {"error": f"upstream rejected api_key={FAKE_KEY}"}

        class Session:
            trust_env = True

            def post(self, *args, **kwargs):
                return Response()

        client = bot.LocalWorkbenchClient(config(), Session())
        job = bot.TestJob(OPEN_ID, "https://api.anthropic.com", FAKE_KEY,
                          "claude-test-model", "c" * 32, "2026-01-01T00:00:00+00:00")
        with self.assertRaises(bot.BotError) as raised:
            client.run_screenshot(job)
        message = str(raised.exception)
        self.assertNotIn(FAKE_KEY, message)
        self.assertIn("[REDACTED]", message)


class ReportingTests(unittest.TestCase):
    def make_job(self):
        return bot.TestJob(OPEN_ID, "https://api.anthropic.com", FAKE_KEY,
                           "claude-test-model", "b" * 32, "2026-01-01T00:00:00+00:00")

    def test_all_pass_conclusion(self):
        screen = {"items": [{"id": "1", "title": "a", "status": "PASS"}], "budget": {"requests": 1}}
        passthrough = {"items": [{"id": "2", "title": "b", "status": "PASS"}], "budget": {"requests_attempted": 1}}
        report, markdown = bot.aggregate_report(screen, passthrough, self.make_job(), 1.2)
        self.assertEqual(report["conclusion"], "NO_ANOMALY_IN_EXECUTED_CASES")
        self.assertNotIn(FAKE_KEY, markdown)

    def test_fail_observed_and_inconclusive_conclusions(self):
        for status, expected in (
            ("FAIL", "OBSERVED_ANOMALIES"),
            ("OBSERVED", "INCONCLUSIVE"),
            ("NOT_OBSERVABLE", "INCONCLUSIVE"),
        ):
            with self.subTest(status=status):
                screen = {"items": [{"id": "1", "title": "a", "status": status}],
                          "budget": {"requests": 1}}
                passthrough = {"items": [], "budget": {"requests_attempted": 0}}
                report, _ = bot.aggregate_report(screen, passthrough, self.make_job(), 1)
                self.assertEqual(report["conclusion"], expected)

    def test_report_redacts_fake_key(self):
        screen = {"items": [{"id": "1", "title": "a", "status": "FAIL",
                              "actual": f"server echoed {FAKE_KEY}"}], "budget": {"requests": 1}}
        passthrough = {"items": [], "budget": {"requests_attempted": 0}}
        report, markdown = bot.aggregate_report(screen, passthrough, self.make_job(), 1)
        encoded = __import__("json").dumps(report, ensure_ascii=False)
        self.assertNotIn(FAKE_KEY, encoded)
        self.assertNotIn(FAKE_KEY, markdown)
        self.assertIn("[REDACTED]", encoded)


class EventQueueTests(BotTestBase):
    def test_event_queue_does_not_touch_network(self):
        with patch.object(self.service, "handle_message") as handler:
            self.service.enqueue_message(OPEN_ID, "event-1", "帮助", "p2p")
            self.assertEqual(self.service.events.qsize(), 1)
            handler.assert_not_called()
        self.service.events.get_nowait()
        self.service.events.task_done()


class ConfigTests(unittest.TestCase):
    def test_group_allowlist_requires_bot_open_id(self):
        env = {
            "FEISHU_APP_ID": "cli_fake",
            "FEISHU_APP_SECRET": "fake-secret",
            "FEISHU_ALLOWED_OPEN_IDS": OPEN_ID,
            "FEISHU_ALLOWED_CHAT_IDS": "oc_test_group_123",
        }
        with patch.dict("os.environ", env, clear=True):
            with self.assertRaises(bot.BotError):
                bot.BotConfig.from_env()
        env["FEISHU_BOT_OPEN_ID"] = OPEN_ID
        with patch.dict("os.environ", env, clear=True):
            self.assertEqual(bot.BotConfig.from_env().allowed_chat_ids, frozenset({"oc_test_group_123"}))

    def test_timeout_and_beta_validation(self):
        base = {
            "FEISHU_APP_ID": "cli_fake",
            "FEISHU_APP_SECRET": "secret",
            "FEISHU_ALLOWED_OPEN_IDS": OPEN_ID,
        }
        with patch.dict("os.environ", {**base, "CCMAX_FEISHU_HTTP_TIMEOUT": "121"}, clear=True):
            with self.assertRaises(bot.BotError):
                bot.BotConfig.from_env()
        with patch.dict("os.environ", {**base, "CCMAX_CLASSIFIER_BETA": "auto-mode-classifier-*"}, clear=True):
            with self.assertRaises(bot.BotError):
                bot.BotConfig.from_env()


if __name__ == "__main__":
    unittest.main()
