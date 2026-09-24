"""Mirror the SQLite ledger to two owner-only tabs in a Google spreadsheet.

SQLite remains the source of truth; a failed export is retried without losing records.
Requires google-auth[requests] only when GOOGLE_SHEET_ID is configured.
"""
import logging
import json
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from bot import CURRENCIES, rows_for, summary

HEADERS = ["ID", "Дата", "Telegram ID", "Пользователь", "Операция", "Объект", "Этап", "Тип затрат", "Источник", "Счёт", "Откуда", "Куда", "Валюта", "Сумма", "Комментарий", "Валюта зачисления", "Зачислено", "Курс UAH за USD"]
TOTAL_HEADERS = ["Telegram ID", "Пользователь", "Валюта", "Приход", "Расход", "Изменение денег", "Доступ"]
GUIDE = [
    ["УЧЁТ СТРОЙКИ", "Как пользоваться таблицей"],
    ["Операции", "Подробный журнал всех приходов, расходов и переводов. Фильтруйте строки по пользователю, объекту, валюте или дате."],
    ["Итоги", "Суммы отдельно в UAH и USD. Обмен не входит в приход и расход, но влияет на изменение денег каждой валюты."],
    ["Важно", "Добавляйте и исправляйте операции в Telegram-боте. Таблица автоматически обновляется из бота."],
]
TAB_NAMES = ("Инструкция", "Итоги", "Операции")
COLORS = {
    "header": {"red": 0.10, "green": 0.31, "blue": 0.27},
    "header_text": {"red": 1, "green": 1, "blue": 1},
    "stripe_a": {"red": 1, "green": 1, "blue": 1},
    "stripe_b": {"red": 0.93, "green": 0.97, "blue": 0.95},
    "total": {"red": 0.84, "green": 0.93, "blue": 0.88},
    "guide": {"red": 0.89, "green": 0.95, "blue": 0.92},
}


def amount(kop):
    return float(Decimal(kop) / 100)


def snapshot(db):
    rows = rows_for(db)
    users = db.execute("SELECT id,name,active FROM users ORDER BY id").fetchall()
    operations = [HEADERS]
    for r in rows:
        operations.append([r["id"], r["occurred_on"], r["user_id"], r["user_name"] or "", r["kind"], r["project"] or "", r["category"] or "", r["cost_type"] or "", r["source"] or "", r["account"] or "", r["from_account"] or "", r["to_account"] or "", r["currency"], amount(r["amount_kop"]), r["comment"], r["target_currency"] or "", amount(r["target_amount_kop"]) if r["target_currency"] else "", r["exchange_rate"] or ""])
    totals = [TOTAL_HEADERS]
    for u in users:
        per_user = summary(r for r in rows if r["user_id"] == u["id"])
        for currency in CURRENCIES:
            s = per_user[currency]
            totals.append([u["id"], u["name"], currency, amount(s["financing"] + s["sales"] + s["refunds"]), amount(s["expense"]), amount(s["cash_net"]), "Активен" if u["active"] else "Отключён"])
    overall = summary(rows)
    for currency in CURRENCIES:
        s = overall[currency]
        totals.append(["", "ВСЕ ПОЛЬЗОВАТЕЛИ", currency, amount(s["financing"] + s["sales"] + s["refunds"]), amount(s["expense"]), amount(s["cash_net"]), ""])
    return operations, totals


class SheetsSync:
    def __init__(self, spreadsheet_id, session):
        self.base = "https://sheets.googleapis.com/v4/spreadsheets/" + quote(spreadsheet_id, safe="")
        self.session = session
        self.synced_version = None

    def request(self, method, suffix, body=None):
        response = self.session.request(method, self.base + suffix, json=body, timeout=25)
        response.raise_for_status()
        return response.json()

    def _metadata(self):
        return self.request("GET", "?fields=sheets(properties(sheetId,title,gridProperties),basicFilter,bandedRanges)").get("sheets", [])

    def _ensure_tabs(self):
        sheets = self._metadata()
        # Turn the untouched default worksheet into a short guide instead of leaving a confusing blank tab.
        for sheet in sheets:
            title = sheet.get("properties", {}).get("title")
            if title in ("Лист1", "Sheet1") and "Инструкция" not in {s.get("properties", {}).get("title") for s in sheets}:
                self.request("POST", ":batchUpdate", {"requests": [{"updateSheetProperties": {
                    "properties": {"sheetId": sheet["properties"]["sheetId"], "title": "Инструкция"}, "fields": "title"
                }}]})
                sheet["properties"]["title"] = "Инструкция"
                break
        names = {s.get("properties", {}).get("title") for s in sheets}
        missing = set(TAB_NAMES) - names
        if missing:
            self.request("POST", ":batchUpdate", {"requests": [{"addSheet": {"properties": {"title": name}}} for name in sorted(missing)]})
        return {s["properties"]["title"]: s["properties"]["sheetId"] for s in self._metadata()}

    @staticmethod
    def _dimension(sheet_id, start, end, width):
        return {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": start, "endIndex": end},
            "properties": {"pixelSize": width}, "fields": "pixelSize"
        }}

    def _format_tabs(self, ids, operation_count, total_count):
        requests = []
        # Rebuild filters and banding idempotently on every snapshot refresh.
        metadata = {s["properties"]["title"]: s for s in self._metadata()}
        for title in TAB_NAMES:
            sheet = metadata[title]
            sid = ids[title]
            if sheet.get("basicFilter"):
                requests.append({"clearBasicFilter": {"sheetId": sid}})
            for band in sheet.get("bandedRanges", []):
                if "bandedRangeId" in band:
                    requests.append({"deleteBanding": {"bandedRangeId": band["bandedRangeId"]}})
            requests.append({"updateSheetProperties": {
                "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1, "hideGridlines": True},
                               "tabColor": COLORS["header"]},
                "fields": "gridProperties.frozenRowCount,gridProperties.hideGridlines,tabColor"
            }})

        op_id, total_id, guide_id = ids["Операции"], ids["Итоги"], ids["Инструкция"]
        requests.extend([
            {"setBasicFilter": {"filter": {"range": {"sheetId": op_id, "startRowIndex": 0,
                                                           "endRowIndex": max(1, operation_count),
                                                           "startColumnIndex": 0, "endColumnIndex": len(HEADERS)}}}},
            {"addBanding": {"bandedRange": {"range": {"sheetId": op_id, "startRowIndex": 0,
                                                           "endRowIndex": max(2, operation_count),
                                                           "startColumnIndex": 0, "endColumnIndex": len(HEADERS)},
                                "rowProperties": {"headerColor": COLORS["header"], "firstBandColor": COLORS["stripe_a"],
                                                  "secondBandColor": COLORS["stripe_b"]}}}},
            {"repeatCell": {"range": {"sheetId": op_id, "startRowIndex": 0, "endRowIndex": 1},
                             "cell": {"userEnteredFormat": {"backgroundColor": COLORS["header"],
                                                              "textFormat": {"bold": True, "foregroundColor": COLORS["header_text"]},
                                                              "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"}},
                             "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,wrapStrategy)"}},
            {"repeatCell": {"range": {"sheetId": op_id, "startRowIndex": 1, "endRowIndex": max(2, operation_count),
                                       "startColumnIndex": 13, "endColumnIndex": 14},
                             "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}}},
                             "fields": "userEnteredFormat.numberFormat"}},
            {"repeatCell": {"range": {"sheetId": op_id, "startRowIndex": 1, "endRowIndex": max(2, operation_count),
                                       "startColumnIndex": 14, "endColumnIndex": 15},
                             "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                             "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}},
            {"setBasicFilter": {"filter": {"range": {"sheetId": total_id, "startRowIndex": 0,
                                                           "endRowIndex": max(1, total_count),
                                                           "startColumnIndex": 0, "endColumnIndex": len(TOTAL_HEADERS)}}}},
            {"addBanding": {"bandedRange": {"range": {"sheetId": total_id, "startRowIndex": 0,
                                                           "endRowIndex": max(2, total_count),
                                                           "startColumnIndex": 0, "endColumnIndex": len(TOTAL_HEADERS)},
                                "rowProperties": {"headerColor": COLORS["header"], "firstBandColor": COLORS["stripe_a"],
                                                  "secondBandColor": COLORS["stripe_b"]}}}},
            {"repeatCell": {"range": {"sheetId": total_id, "startRowIndex": 0, "endRowIndex": 1},
                             "cell": {"userEnteredFormat": {"backgroundColor": COLORS["header"],
                                                              "textFormat": {"bold": True, "foregroundColor": COLORS["header_text"]},
                                                              "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"}},
                             "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,wrapStrategy)"}},
            {"repeatCell": {"range": {"sheetId": total_id, "startRowIndex": 1, "endRowIndex": max(1, total_count),
                                       "startColumnIndex": 3, "endColumnIndex": 6},
                             "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}}},
                             "fields": "userEnteredFormat.numberFormat"}},
            {"repeatCell": {"range": {"sheetId": total_id, "startRowIndex": max(1, total_count - 2),
                                       "endRowIndex": max(1, total_count), "startColumnIndex": 0,
                                       "endColumnIndex": len(TOTAL_HEADERS)},
                             "cell": {"userEnteredFormat": {"backgroundColor": COLORS["total"],
                                                              "textFormat": {"bold": True}}},
                             "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
            {"repeatCell": {"range": {"sheetId": guide_id, "startRowIndex": 0, "endRowIndex": 1,
                                       "startColumnIndex": 0, "endColumnIndex": 2},
                             "cell": {"userEnteredFormat": {"backgroundColor": COLORS["header"],
                                                              "textFormat": {"bold": True, "foregroundColor": COLORS["header_text"], "fontSize": 13},
                                                              "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"}},
                             "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,wrapStrategy)"}},
            {"repeatCell": {"range": {"sheetId": guide_id, "startRowIndex": 1, "endRowIndex": len(GUIDE)},
                             "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "MIDDLE"}},
                             "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}},
            self._dimension(op_id, 0, 1, 65), self._dimension(op_id, 1, 2, 110),
            self._dimension(op_id, 2, 3, 105), self._dimension(op_id, 3, 4, 145),
            self._dimension(op_id, 4, 5, 105), self._dimension(op_id, 5, 12, 135),
            self._dimension(op_id, 12, 14, 90), self._dimension(op_id, 14, 15, 260),
            self._dimension(total_id, 0, 1, 115), self._dimension(total_id, 1, 2, 190),
            self._dimension(total_id, 2, 3, 85), self._dimension(total_id, 3, 6, 155),
            self._dimension(total_id, 6, 7, 105), self._dimension(guide_id, 0, 1, 150),
            self._dimension(guide_id, 1, 2, 650),
        ])
        self.request("POST", ":batchUpdate", {"requests": requests})

    def __call__(self, db):
        revision = db.execute("SELECT value FROM settings WHERE key='ledger_revision'").fetchone()
        version = (db.execute("SELECT COALESCE(MAX(id),0) FROM operations").fetchone()[0],
                   tuple(tuple(u) for u in db.execute("SELECT id,name,active FROM users ORDER BY id")),
                   int(revision[0]) if revision else 0)
        if version == self.synced_version:
            return True
        try:
            ids = self._ensure_tabs()
            operations, totals = snapshot(db)
            self.request("POST", "/values:batchClear", {"ranges": ["'Операции'!A:R", "'Итоги'!A:G", "'Инструкция'!A:B"]})
            self.request("POST", "/values:batchUpdate", {"valueInputOption": "RAW", "data": [
                {"range": "'Операции'!A1", "values": operations},
                {"range": "'Итоги'!A1", "values": totals},
                {"range": "'Инструкция'!A1", "values": GUIDE},
            ]})
            self._format_tabs(ids, len(operations), len(totals))
            self.synced_version = version
            logging.info("Google Таблица обновлена: %d операций", len(operations) - 1)
            return True
        except Exception:
            logging.exception("Не удалось обновить Google Таблицу; повторим позже")
            return False


def build_sync(spreadsheet_id, credentials_config):
    if not spreadsheet_id or not credentials_config:
        raise SystemExit("Для Google Таблицы задай GOOGLE_SHEET_ID и GOOGLE_SERVICE_ACCOUNT_JSON.")
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
    except ImportError:
        raise SystemExit("Установи библиотеку: python3.15 -m pip install 'google-auth[requests]'") from None
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    # On a hosted service, keep the service-account JSON in its secret variables
    # instead of committing a credentials file to the source repository.
    if credentials_config.lstrip().startswith("{"):
        credentials = service_account.Credentials.from_service_account_info(
            json.loads(credentials_config), scopes=scopes
        )
    else:
        credentials_file = Path(credentials_config)
        if not credentials_file.is_file():
            raise SystemExit("Файл ключа Google не найден. Проверь GOOGLE_SERVICE_ACCOUNT_JSON.")
        credentials = service_account.Credentials.from_service_account_file(
            str(credentials_file), scopes=scopes
        )
    return SheetsSync(spreadsheet_id, AuthorizedSession(credentials))
