#!/usr/bin/env python3
"""One-time private setup. Never prints or packages the Telegram token."""
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_USER_ID = "243705540"


def main():
    if sys.version_info < (3, 10):
        raise SystemExit("Нужен Python 3.10 или новее. Проверь: python3 --version")
    path = ROOT / ".env"
    previous = {}
    if path.exists():
        answer = input("Настройка уже существует. Заменить токен и ID? [д/Н]: ").strip().lower()
        if answer not in ("д", "да", "y", "yes"):
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key in ("GOOGLE_SHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON", "BOT_DB_PATH"):
                previous[key] = value

    print("Вставь НОВЫЙ токен после замены в BotFather. Символы при вводе скрыты.")
    token = getpass.getpass("Токен: ").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{30,}", token):
        raise SystemExit("Неверный формат токена. Запусти настройку ещё раз.")
    user_id = input(f"Твой Telegram ID [{DEFAULT_USER_ID}]: ").strip() or DEFAULT_USER_ID
    if not re.fullmatch(r"[0-9]{3,20}", user_id):
        raise SystemExit("Telegram ID должен состоять только из цифр.")

    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=15) as response:
            answer = json.load(response)
        if not answer.get("ok"):
            raise ValueError("Telegram отклонил токен")
    except (urllib.error.URLError, ValueError, OSError):
        raise SystemExit("Не удалось проверить токен. Проверь новый токен и доступ к Telegram, затем повтори настройку.") from None

    content = (
        f"TELEGRAM_BOT_TOKEN={token}\n"
        f"ALLOWED_USER_IDS={user_id}\n"
        f"OWNER_USER_ID={user_id}\n"
        f"BOT_DB_PATH={previous.get('BOT_DB_PATH', './data/ledger.sqlite3')}\n" +
        "".join(f"{key}={previous[key]}\n" for key in ("GOOGLE_SHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON") if previous.get(key))
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(content)
        os.chmod(path, 0o600)
    except Exception:
        raise SystemExit("Не удалось сохранить настройки.") from None
    print(f"Настроено: @{answer['result']['username']}; доступ для Telegram ID {user_id}.")
    print("Теперь запусти: python3 run_mac.py")


if __name__ == "__main__":
    main()
