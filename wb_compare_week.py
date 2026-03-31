#!/usr/bin/env python3
"""
Сравнение API данных за неделю с XLSX кабинета.
Отправляет результат в Telegram.
"""
import os, sys, time, requests
from datetime import datetime, timedelta, timezone
from collections import defaultdict

STATS_HOST = "https://statistics-api.wildberries.ru"
WB_TOKEN = os.getenv("WB_TOKEN", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
MSK = timezone(timedelta(hours=3))

# XLSX итоги за 24-30 марта 2026
XLSX = {
    "period": "24.03 — 30.03.2026",
    "orders_qty": 293,
    "orders_sum": 570335.67,
    "sales_qty": 197,
    "for_pay": 383700.09,
}

DATE_FROM = datetime(2026, 3, 24, tzinfo=MSK)
DATE_TO = datetime(2026, 3, 30, tzinfo=MSK)


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
    # flag=1 = строгий фильтр по дате
    params = {"dateFrom": date_from.isoformat(), "flag": 1}
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def send_telegram(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("TG не настроен, вывожу в консоль")
        print(text)
        return
    requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
        data={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=15,
    )


def in_range(dt):
    return dt and DATE_FROM.date() <= dt.date() <= DATE_TO.date()


def main():
    if not WB_TOKEN:
        print("WB_TOKEN не задан")
        sys.exit(1)

    print("Загружаю заказы (flag=1)...")
    orders = fetch("orders", DATE_FROM)
    print(f"Всего: {len(orders)}")

    print("Жду 62 сек (лимит API)...")
    time.sleep(62)

    print("Загружаю продажи (flag=1)...")
    sales = fetch("sales", DATE_FROM)
    print(f"Всего: {len(sales)}")

    # Фильтр 24-30 марта
    week_orders = [o for o in orders if in_range(parse_date(o.get("date")))]
    week_sales = [s for s in sales if in_range(parse_date(s.get("date")))]

    # Считаем ВСЕ поля
    api = {
        "orders_qty": len(week_orders),
        "totalPrice": sum(o.get("totalPrice", 0) for o in week_orders),
        "priceWithDisc": sum(o.get("priceWithDisc", 0) for o in week_orders),
        "finishedPrice": sum(o.get("finishedPrice", 0) for o in week_orders),
        "forPay_orders": sum(o.get("forPay", 0) for o in week_orders),
        "sales_qty": len(week_sales),
        "forPay_sales": sum(s.get("forPay", 0) for s in week_sales),
    }

    # Сравнение
    def diff(api_val, xlsx_val):
        d = api_val - xlsx_val
        pct = (d / xlsx_val * 100) if xlsx_val else 0
        mark = "✅" if abs(pct) < 1 else "⚠️" if abs(pct) < 5 else "❌"
        return f"{mark} {api_val:,.0f}  (XLSX: {xlsx_val:,.0f}, разница: {d:+,.0f} / {pct:+.1f}%)"

    msg = f"""<b>📊 API vs XLSX — {XLSX['period']}</b>

<b>ЗАКАЗЫ (шт):</b>
  {diff(api['orders_qty'], XLSX['orders_qty'])}

<b>"Сумма минус комиссия WB" — какое поле совпадает:</b>
  totalPrice:    {diff(api['totalPrice'], XLSX['orders_sum'])}
  priceWithDisc: {diff(api['priceWithDisc'], XLSX['orders_sum'])}
  finishedPrice: {diff(api['finishedPrice'], XLSX['orders_sum'])}
  forPay(orders):{diff(api['forPay_orders'], XLSX['orders_sum'])}

<b>ВЫКУПИЛИ (шт):</b>
  {diff(api['sales_qty'], XLSX['sales_qty'])}

<b>"К перечислению за товар":</b>
  forPay(sales): {diff(api['forPay_sales'], XLSX['for_pay'])}

<b>ВЫВОД:</b>"""

    # Автоматический вывод
    best_field = None
    best_diff = float('inf')
    for field, val in [("totalPrice", api["totalPrice"]),
                        ("priceWithDisc", api["priceWithDisc"]),
                        ("finishedPrice", api["finishedPrice"])]:
        d = abs(val - XLSX["orders_sum"])
        if d < best_diff:
            best_diff = d
            best_field = field

    msg += f'\n  Ближайшее к XLSX "Сумма минус комиссия": <b>{best_field}</b>'
    msg += f"\n  flag=1 использован (строгий фильтр по дате)"

    print(msg)
    send_telegram(msg)
    print("\n✅ Отправлено")


if __name__ == "__main__":
    main()
