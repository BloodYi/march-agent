import asyncio
import unittest
from unittest.mock import patch

import app


class BuildArgvTests(unittest.TestCase):
    def test_prompt_is_attached_to_print_flag(self):
        argv = app._build_argv(
            "default",
            prompt="--dangerously-skip-permissions is not the prompt",
            target_cwd="/tmp/workspace",
        )

        prompt_args = [arg for arg in argv if arg.startswith("-p=")]
        self.assertEqual(prompt_args, ["-p=--dangerously-skip-permissions is not the prompt"])
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("-p", argv)


class StreamParsingTests(unittest.TestCase):
    def test_parses_documented_tool_step(self):
        events = app._parse_ndjson_line(
            '{"event":"step_update","step_update":{"step_type":"tool",'
            '"tool_name":"run_command","tool_info":{"name":"run_command",'
            '"parameters":{"CommandLine":"pwd"}},"state":"DONE"}}'
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['event'], 'tool')
        self.assertEqual(events[0]['tool'], 'run_command')
        self.assertEqual(events[0]['args'], {'CommandLine':'pwd'})
        self.assertEqual(events[0]['status'], 'DONE')

    def test_preserves_final_response_and_usage(self):
        events = app._parse_ndjson_line(
            '{"event":"result","result":{"conversation_id":"cid-1",'
            '"status":"SUCCESS","response":"Hello","usage":{"total_tokens":3}}}'
        )

        self.assertEqual(events, [{
            "event": "result",
            "usage": {"total_tokens": 3},
            "status": "SUCCESS",
            "response": "Hello",
            "conversation_id": "cid-1",
        }])
    def test_parses_thinking_in_step_update(self):
        events = app._parse_ndjson_line(
            '{"event":"step_update","step_update":{"step_type":"agent_response",'
            '"thinking":"Let me analyze this carefully","text_delta":"The answer is 42."}}'
        )

        self.assertEqual([event['event'] for event in events], ['delta','thinking'])
        self.assertEqual(events[0]['delta'], 'The answer is 42.')
        self.assertEqual(events[1]['thinking'], 'Let me analyze this carefully')
        self.assertNotIn('delta', events[1], 'Thinking must not enter the body stream')

    def test_parses_reasoning_content_step(self):
        events = app._parse_ndjson_line(
            '{"reasoning_content":"Step 1: Check inputs"}'
        )

        self.assertEqual(events, [{'event':'thinking','thinking':'Step 1: Check inputs'}])

class PromptResolutionTests(unittest.TestCase):
    def test_resumed_conversation_sends_only_latest_message(self):
        request = app.ChatRequest(
            messages=[
                app.Message(role="user", content="Earlier question"),
                app.Message(role="assistant", content="Earlier answer"),
                app.Message(role="user", content="Latest question"),
            ],
            conversation_id="conversation-1",
        )

        with patch.object(app, "AGY_PERSIST_CONVERSATIONS", True):
            prompt, conversation_id, continue_last, _ = asyncio.run(
                app._resolve_prompt_and_session(request)
            )

        self.assertEqual(prompt, "Latest question")
        self.assertEqual(conversation_id, "conversation-1")
        self.assertFalse(continue_last)

    def test_resumed_conversation_with_thinking_includes_thinking_prefix(self):
        request = app.ChatRequest(
            messages=[
                app.Message(role="user", content="Explain relativity"),
            ],
            conversation_id="conversation-1",
            thinking=True,
        )

        with patch.object(app, "AGY_PERSIST_CONVERSATIONS", True):
            prompt, _, _, _ = asyncio.run(
                app._resolve_prompt_and_session(request)
            )

        self.assertIn("<think>", prompt)
        self.assertIn("Explain relativity", prompt)

    def test_resumed_conversation_includes_attachment_info(self):
        request = app.ChatRequest(
            messages=[
                app.Message(
                    role="user",
                    content="Look at this",
                    attachments=[{"filename": "test.png", "path": "/path/to/test.png", "is_image": True}],
                )
            ],
            conversation_id="conversation-1",
        )

        with patch.object(app, "AGY_PERSIST_CONVERSATIONS", True):
            prompt, _, _, _ = asyncio.run(
                app._resolve_prompt_and_session(request)
            )

        self.assertIn("附带图片: test.png", prompt)
        self.assertIn("/path/to/test.png", prompt)
        self.assertIn("Look at this", prompt)

    def test_rendered_prompt_includes_attachment_info(self):
        request = app.ChatRequest(
            messages=[
                app.Message(
                    role="user",
                    content="Here is a file",
                    attachments=[{"filename": "doc.pdf", "path": "/path/to/doc.pdf", "is_image": False}],
                )
            ],
        )

        with patch.object(app, "AGY_PERSIST_CONVERSATIONS", False):
            prompt, _, _, _ = asyncio.run(
                app._resolve_prompt_and_session(request)
            )

        self.assertIn("附带文件: doc.pdf", prompt)
        self.assertIn("/path/to/doc.pdf", prompt)
        self.assertIn("Here is a file", prompt)


class QuotaParsingTests(unittest.TestCase):
    def test_parses_tab_separated_usage(self):
        text = (
            "Gemini Models\tWeekly Limit Remaining\t93%\t2026-10-11T19:34:12Z\n"
            "Gemini Models\tFive Hour Limit Remaining\t57%\t2026-10-05T17:57:58Z\n"
            "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-10-12T16:04:28Z\n"
            "Claude and GPT models\tFive Hour Limit Remaining\t100%\t2026-10-05T21:04:28Z\n"
        )
        quotas = app._parse_usage_output(text)
        self.assertEqual(len(quotas), 4)
        self.assertEqual(quotas[0]["model"], "Gemini Models")
        self.assertEqual(quotas[0]["type"], "weekly")
        self.assertEqual(quotas[0]["percentage"], 93)
        self.assertEqual(quotas[1]["type"], "five_hour")
        self.assertEqual(quotas[1]["percentage"], 57)
        self.assertIn("消失额度", quotas[1]["label"])

    def test_parses_space_separated_usage(self):
        text = (
            "Quota:\n"
            "Gemini Models          Weekly Limit Remaining     93%   2026-10-11 19:34 UTC\n"
            "Gemini Models          Five Hour Limit Remaining  57%   2026-10-05 17:57 UTC\n"
        )
        quotas = app._parse_usage_output(text)
        self.assertEqual(len(quotas), 2)
        self.assertEqual(quotas[0]["percentage"], 93)
        self.assertEqual(quotas[1]["percentage"], 57)

    def test_parses_credits(self):
        text = "Model credits\nRemaining credits\t42\nUpgrade\thttps://antigravity.google/g1-upgrade\n"
        self.assertEqual(app._parse_credits_output(text), 42)


class AccountManagementTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.test_dir = tempfile.TemporaryDirectory()
        self.accounts_dir = Path(self.test_dir.name) / "accounts"
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        self.token_file = Path(self.test_dir.name) / "token"
        self.patchers = [
            patch.object(app, "ACCOUNTS_DIR", self.accounts_dir),
            patch.object(app, "ACTIVE_TOKEN_FILE", self.token_file),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        self.test_dir.cleanup()

    def test_saves_and_switches_accounts(self):
        import json
        # Save account 1
        acc1_data = {"token": "token-1", "auth_method": "oauth", "email": "user1@example.com"}
        res1 = app._save_new_account(acc1_data, alias="user1", switch_now=True)
        self.assertTrue(res1["ok"])
        self.assertEqual(self.token_file.read_text(), json.dumps(acc1_data, indent=2) + "\n")

        # Save account 2
        acc2_data = {"token": "token-2", "auth_method": "oauth", "email": "user2@example.com"}
        res2 = app._save_new_account(acc2_data, alias="user2", switch_now=False)
        self.assertTrue(res2["ok"])

        # Check list accounts
        accs = app._list_stored_accounts()
        self.assertEqual(len(accs), 2)
        user1 = next(a for a in accs if a["id"] == "user1")
        user2 = next(a for a in accs if a["id"] == "user2")
        self.assertTrue(user1["active"])
        self.assertFalse(user2["active"])

        # Switch to user2
        switch_res = app._switch_to_account("user2")
        self.assertTrue(switch_res["ok"])
        self.assertEqual(json.loads(self.token_file.read_text())["token"], "token-2")

        accs_after = app._list_stored_accounts()
        user2_after = next(a for a in accs_after if a["id"] == "user2")
        self.assertTrue(user2_after["active"])

    def test_deletes_inactive_account_and_blocks_active(self):
        acc1_data = {"token": "token-1", "auth_method": "oauth", "email": "user1@example.com"}
        app._save_new_account(acc1_data, alias="user1", switch_now=True)
        acc2_data = {"token": "token-2", "auth_method": "oauth", "email": "user2@example.com"}
        app._save_new_account(acc2_data, alias="user2", switch_now=False)

        # Cannot delete active
        with self.assertRaises(app.HTTPException) as ctx:
            app._delete_stored_account("user1")
        self.assertEqual(ctx.exception.status_code, 400)

        # Can delete inactive
        res = app._delete_stored_account("user2")
        self.assertTrue(res["ok"])
        self.assertFalse((self.accounts_dir / "user2.json").exists())


if __name__ == "__main__":
    unittest.main()

