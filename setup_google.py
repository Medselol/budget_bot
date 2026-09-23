#!/usr/bin/env python3
"""One-time Google Sheets setup without pasting a private key into Terminal."""
import json
import os
import re
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def private_write(path, content):
    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".google-setup-", delete=False) as out:
        temporary = Path(out.name)
        try:
            os.chmod(temporary, 0o600)
            out.write(content)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def main():
    env_file = ROOT / ".env"
    if not env_file.is_file():
        raise SystemExit("Сначала настрой Telegram бот: python3.15 setup_mac.py")
    link = input("Ссылка на созданную Google Таблицу: ").strip()
    match = re.fullmatch(r"https://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)(?:[/?#].*)?", link)
    if not match:
        raise SystemExit("Нужна ссылка вида https://docs.google.com/spreadsheets/d/ID/edit")
    raw_path = input("Путь к скачанному JSON ключу (можно перетащить файл сюда): ").strip().strip("'\"")
    source = Path(raw_path.replace("\\ ", " ")).expanduser()
    try:
        data = source.read_bytes()
        credentials = json.loads(data)
    except (OSError, ValueError):
        raise SystemExit("Не удалось прочитать JSON ключ. Проверь путь к файлу.") from None
    if credentials.get("type") != "service_account" or not all(credentials.get(key) for key in ("client_email", "private_key", "token_uri")):
        raise SystemExit("Выбери JSON ключ сервисного аккаунта, созданный в Google Cloud.")
    target = ROOT / "google-service-account.json"
    private_write(target, data)
    old = env_file.read_text(encoding="utf-8").splitlines()
    old = [line for line in old if not line.startswith(("GOOGLE_SHEET_ID=", "GOOGLE_SERVICE_ACCOUNT_JSON="))]
    old.extend([f"GOOGLE_SHEET_ID={match[1]}", "GOOGLE_SERVICE_ACCOUNT_JSON=./google-service-account.json"])
    private_write(env_file, ("\n".join(old) + "\n").encode("utf-8"))
    print("Настроено. Добавь в доступ к таблице адрес:", credentials["client_email"])
    print("После этого запускай бота через .venv/bin/python run_mac.py")


if __name__ == "__main__":
    main()
