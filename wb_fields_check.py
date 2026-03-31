#!/usr/bin/env python3
"""
Показывает ВСЕ поля одного заказа из WB API.
Ищем поле с количеством товара.
"""
import os, sys, requests, json
from datetime import datetime, timedelta, timezone

STATS_HOST = "https://statistics-api.wildberries.ru"
WB_TOKEN = os.getenv("WB_TOKEN", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
MSK = timezone(timedelta(hours=3))


def send_telegram(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print(text)
        return
    # Telegram limit 4096 chars, split if needed
    for i in range(0, len(text), 4000):
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT_ID, "text": text[i:i+4000], "parse_mode": "HTML"},
            timeout=15,
        )


def main():
    if not WB_TOKEN:
        print("WB_TOKEN не задан")
        sys.exit(1)

    date_from = datetime.now(MSK) - timedelta(days=7)
    url = f"{STATS_HOST}/api/v1/supplier/orders"
    headers = {"Authorization": f"Bearer {WB_TOKEN}"}
    params = {"dateFrom": date_from.isoformat(), "flag": 0}

    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    orders = resp.json()

    if not orders:
        send_telegram("Нет заказов")
        return

    # Все поля первого заказа
    sample = orders[0]
    msg = "<b>🔎 ВСЕ ПОЛЯ ЗАКАЗА (1-й из API)</b>\n\n"
    for key, val in sorted(sample.items()):
        msg += f"<b>{key}</b>: {val}\n"

    # Ищем поля похожие на количество
    msg += "\n<b>🔍 ПОЛЯ С ЧИСЛАМИ &gt; 0:</b>\n"
    for key, val in sorted(sample.items()):
        if isinstance(val, (int, float)) and val > 0:
            msg += f"  {key}: {val}\n"

    # Проверяем есть ли дубли по srid/orderId
    msg += f"\n<b>📊 СТАТИСТИКА ({len(orders)} заказов):</b>\n"

    # Уникальные ключи
    srids = set()
    order_ids = set()
    for o in orders:
        if o.get("srid"):
            srids.add(o["srid"])
        if o.get("orderId"):
            order_ids.add(o["orderId"])

    msg += f"  Уникальных srid: {len(srids)}\n"
    msg += f"  Уникальных orderId: {len(order_ids)}\n"
    msg += f"  Всего строк: {len(orders)}\n"

    if len(orders) != len(srids):
        msg += f"  ⚠️ Есть дубли по srid! ({len(orders) - len(srids)} дублей)\n"

    # Есть ли orderId с несколькими строками?
    from collections import Counter
    oid_counts = Counter(o.get("orderId") for o in orders)
    multi = {oid: cnt for oid, cnt in oid_counts.items() if cnt > 1}
    if multi:
        msg += f"\n<b>⚠️ orderId с несколькими строками ({len(multi)}):</b>\n"
        for oid, cnt in list(multi.items())[:5]:
            msg += f"  orderId {oid}: {cnt} строк\n"

    print(msg)
    send_telegram(msg)
    print("\n✅ Отправлено")


if __name__ == "__main__":
    main()
