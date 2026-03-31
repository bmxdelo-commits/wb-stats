#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WB API Debug — сравнение данных API с ожидаемыми.
Отправляет результат в Telegram.

Запуск: python3 wb_api_debug.py
Env: WB_TOKEN, TG_BOT_TOKEN, TG_CHAT_ID
"""

import os
import sys
import requests
from datetime import datetime, timedelta, timezone
from collections import defaultdict

STATS_HOST = "https://statistics-api.wildberries.ru"
WB_TOKEN = os.getenv("WB_TOKEN", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
MSK = timezone(timedelta(hours=3))


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    if "Z" in s or ("+" in s[10:]) or (len(s) > 19 and s[19] == "-"):
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    else:
        dt = datetime.fromisoformat(s).replace(tzinfo=MSK)
    return dt.astimezone(MSK)


def fetch(endpoint, date_from):
    url = f"{STATS_HOST}/api/v1/supplier/{endpoint}"
    headers = {"Authorization": f"Bearer {WB_TOKEN}"}
    params = {"dateFrom": date_from.isoformat(), "flag": 0}
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def send_telegram(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("TG не настроен, вывожу в консоль:")
        print(text)
        return
    requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
        data={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=15,
    )


def main():
    if not WB_TOKEN:
        print("WB_TOKEN не задан")
        sys.exit(1)

    # Вчерашний день
    yesterday = (datetime.now(MSK) - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    date_from = yesterday - timedelta(days=7)  # берём неделю для контекста

    print(f"Загружаю данные с {date_from.date()} ...")
    orders = fetch("orders", date_from)
    print(f"Заказов всего: {len(orders)}")

    # Ждём 62 сек (лимит WB API — 1 запрос в минуту на этот эндпоинт)
    import time
    print("Жду 62 сек (лимит API)...")
    time.sleep(62)

    sales = fetch("sales", date_from)
    print(f"Продаж всего: {len(sales)}")

    # Фильтр на вчера
    day_orders = []
    for o in orders:
        dt = parse_date(o.get("date"))
        if dt and dt.date() == yesterday.date():
            day_orders.append(o)

    day_sales = []
    for s in sales:
        dt = parse_date(s.get("date"))
        if dt and dt.date() == yesterday.date():
            day_sales.append(s)

    # Считаем все варианты цен
    sum_totalPrice = sum(o.get("totalPrice", 0) for o in day_orders)
    sum_priceWithDisc = sum(o.get("priceWithDisc", 0) for o in day_orders)
    sum_finishedPrice = sum(o.get("finishedPrice", 0) for o in day_orders)
    sum_forPay_orders = sum(o.get("forPay", 0) for o in day_orders)
    sum_forPay_sales = sum(s.get("forPay", 0) for s in day_sales)

    # По артикулам (топ-5 по выручке)
    by_sku = defaultdict(lambda: {"qty": 0, "sum": 0.0, "name": ""})
    for o in day_orders:
        nm = o.get("nmId")
        if nm:
            by_sku[nm]["qty"] += 1
            by_sku[nm]["sum"] += o.get("priceWithDisc", 0)
            by_sku[nm]["name"] = o.get("subject", "") or o.get("supplierArticle", "")

    top5 = sorted(by_sku.items(), key=lambda x: x[1]["sum"], reverse=True)[:5]

    # 7-дневный тренд
    trend_lines = []
    for i in range(6, -1, -1):
        d = (yesterday - timedelta(days=i)).date()
        cnt = sum(1 for o in orders if parse_date(o.get("date")) and parse_date(o.get("date")).date() == d)
        trend_lines.append(f"  {d.strftime('%d.%m')}: {cnt} зак.")

    # Формируем сообщение
    date_str = yesterday.strftime("%d.%m.%Y")
    msg = f"""<b>🔍 WB API Debug — {date_str}</b>

<b>ЗАКАЗЫ (orders endpoint, flag=0):</b>
  Количество: <b>{len(day_orders)}</b>
  totalPrice: {sum_totalPrice:,.0f} ₽
  priceWithDisc: <b>{sum_priceWithDisc:,.0f} ₽</b>
  finishedPrice: {sum_finishedPrice:,.0f} ₽
  forPay (из заказов): {sum_forPay_orders:,.0f} ₽

<b>ПРОДАЖИ (sales endpoint):</b>
  Количество: <b>{len(day_sales)}</b>
  forPay (из продаж): <b>{sum_forPay_sales:,.0f} ₽</b>

<b>СРАВНИ С КАБИНЕТОМ WB:</b>
  "Заказано шт" = {len(day_orders)}
  "Сумма минус комиссия" = priceWithDisc = {sum_priceWithDisc:,.0f} ₽
  "Выкупили шт" = {len(day_sales)}
  "К перечислению" = forPay(sales) = {sum_forPay_sales:,.0f} ₽

<b>ТОП-5 артикулов:</b>"""

    for nm, data in top5:
        msg += f"\n  {data['name'][:25]}: {data['qty']} шт, {data['sum']:,.0f} ₽"

    msg += f"\n\n<b>ТРЕНД (7 дней, заказы):</b>\n"
    msg += "\n".join(trend_lines)

    msg += "\n\n<i>Открой кабинет WB → Аналитика → Отчёт за "
    msg += f"{date_str} и сравни цифры выше</i>"

    print(msg)
    send_telegram(msg)
    print("\n✅ Отправлено в Telegram")


if __name__ == "__main__":
    main()
