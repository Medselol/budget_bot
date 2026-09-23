#!/usr/bin/env python3
"""Launch the bot from its private local configuration, without shell exports."""
import os
import sys
from pathlib import Path


def main():
    if sys.version_info < (3, 10):
        raise SystemExit("Нужен Python 3.10 или новее.")
    root = Path(__file__).resolve().parent
    path = root / ".env"
    if not path.is_file():
        raise SystemExit("Сначала выполни: python3 setup_mac.py")
    env = os.environ.copy()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key not in ("TELEGRAM_BOT_TOKEN", "ALLOWED_USER_IDS", "OWNER_USER_ID", "BOT_DB_PATH", "GOOGLE_SHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON"):
            raise SystemExit("Неверный формат настройки. Повтори: python3 setup_mac.py")
        env[key] = value
    os.chdir(root)
    os.execve(sys.executable, [sys.executable, str(root / "bot.py")], env)


if __name__ == "__main__":
    main()
