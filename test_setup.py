import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import setup_mac
import setup_google


class SetupTest(unittest.TestCase):
    def test_google_setup_keeps_telegram_secret_and_sets_private_key_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / ".env"
            config.write_text("TELEGRAM_BOT_TOKEN=fake-private-token\nALLOWED_USER_IDS=243705540\n")
            source = root / "downloaded key.json"
            source.write_text(json.dumps({"type": "service_account", "client_email": "test@example.iam.gserviceaccount.com", "private_key": "dummy", "token_uri": "https://oauth2.googleapis.com/token"}))
            with patch.object(setup_google, "ROOT", root), \
                 patch("builtins.input", side_effect=["https://docs.google.com/spreadsheets/d/example-id/edit", str(source)]), \
                 patch("sys.stdout", new_callable=io.StringIO) as printed:
                setup_google.main()
            self.assertIn("TELEGRAM_BOT_TOKEN=fake-private-token", config.read_text())
            self.assertIn("GOOGLE_SHEET_ID=example-id", config.read_text())
            self.assertNotIn("fake-private-token", printed.getvalue())
            self.assertEqual(os.stat(root / "google-service-account.json").st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(config).st_mode & 0o777, 0o600)

    def test_private_configuration_and_prefilled_id(self):
        fake_token = "123456:" + "A" * 35
        fake_reply = io.BytesIO(json.dumps({"ok": True, "result": {"username": "ExampleBuildBot"}}).encode())
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(setup_mac, "ROOT", Path(folder)), \
                 patch.object(setup_mac.getpass, "getpass", return_value=fake_token), \
                 patch("builtins.input", return_value=""), \
                 patch.object(setup_mac.urllib.request, "urlopen", return_value=fake_reply), \
                 patch("sys.stdout", new_callable=io.StringIO) as printed:
                setup_mac.main()
            config = Path(folder) / ".env"
            self.assertIn("ALLOWED_USER_IDS=243705540", config.read_text())
            self.assertIn("TELEGRAM_BOT_TOKEN=" + fake_token, config.read_text())
            self.assertEqual(os.stat(config).st_mode & 0o777, 0o600)
            self.assertNotIn(fake_token, printed.getvalue())

            with config.open("a", encoding="utf-8") as out:
                out.write("GOOGLE_SHEET_ID=test-sheet\nGOOGLE_SERVICE_ACCOUNT_JSON=./google-service-account.json\n")
            fake_reply2 = io.BytesIO(json.dumps({"ok": True, "result": {"username": "ExampleBuildBot"}}).encode())
            with patch.object(setup_mac, "ROOT", Path(folder)), \
                 patch.object(setup_mac.getpass, "getpass", return_value=fake_token), \
                 patch("builtins.input", side_effect=["да", ""]), \
                 patch.object(setup_mac.urllib.request, "urlopen", return_value=fake_reply2), \
                 patch("sys.stdout", new_callable=io.StringIO):
                setup_mac.main()
            self.assertIn("GOOGLE_SHEET_ID=test-sheet", config.read_text())
            self.assertIn("GOOGLE_SERVICE_ACCOUNT_JSON=./google-service-account.json", config.read_text())


if __name__ == "__main__":
    unittest.main()
