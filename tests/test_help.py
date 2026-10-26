import asyncio
import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

import main
from main import _help_text, _split_telegram_text, help_command, start


class HelpCommandTests(unittest.TestCase):
# ----- verify help documents every registered command -----
    def test_help_documents_every_registered_command(self):
        text = _help_text()
        commands = (
            "start", "help", "cancel", "clear", "prev", "day", "bank", "month",
            "history", "editfood", "deletefood", "weight", "weighthistory", "goal",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertIn(f"/{command}", text)
        self.assertNotIn("/today", text)
        syntax_tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8"))
        registered = {
            call.args[0].value
            for call in ast.walk(syntax_tree)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "CommandHandler"
            and call.args
            and isinstance(call.args[0], ast.Constant)
        }
        self.assertEqual(set(commands), registered)
        for detail in (
            "Confirm, Edit, or Cancel", "50/50", "three calendar days",
            "seven-day rolling average", "Green =", "one combined clarification question",
            "Clear Logged History", "Clear Everything", "permanent",
            "This only views data", "This changes the logging date", "Yes, Delete",
        ):
            with self.subTest(detail=detail):
                self.assertIn(detail, text)

# ----- verify start is short and directs users to help -----
    def test_start_is_short_and_directs_users_to_help(self):
        sent = []

        class Message:
# ----- capture text replies sent by the handler -----
            async def reply_text(self, text):
                sent.append(text)

        asyncio.run(start(SimpleNamespace(message=Message()), None))
        self.assertEqual(len(sent), 1)
        self.assertIn("/help", sent[0])
        self.assertLess(len(sent[0]), 200)

# ----- verify long help is split without loss or oversized chunks -----
    def test_long_help_is_split_without_loss_or_oversized_chunks(self):
        text = ("Paragraph one.\n\n" * 500) + ("x" * 5000)
        chunks = _split_telegram_text(text, limit=4096)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 4096 for chunk in chunks))
        self.assertEqual("".join(chunks), text)

# ----- verify help handler sends each chunk -----
    def test_help_handler_sends_each_chunk(self):
        sent = []

        class Message:
# ----- capture text replies sent by the handler -----
            async def reply_text(self, text):
                sent.append(text)

        asyncio.run(help_command(SimpleNamespace(message=Message()), None))
        self.assertEqual("".join(sent), _help_text())
        self.assertTrue(all(len(part) <= 4096 for part in sent))


if __name__ == "__main__":
    unittest.main()
