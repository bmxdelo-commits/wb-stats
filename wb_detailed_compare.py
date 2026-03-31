#!/usr/bin/env python3
"""
Построчное сравнение API vs XLSX по каждому артикулу.
Показывает где именно расхождение.
"""
import os, sys, time, json, requests
from datetime import datetime, timedelta, timezone
from collections import defaultdict

STATS_HOST = "https://statistics-api.wildberries.ru"
WB_TOKEN = os.getenv("WB_TOKEN", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
MSK = timezone(timedelta(hours=3))

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


def send_telegram(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print(text)
        return
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

    # Загрузка XLSX данных
    with open("xlsx_data.json", "r") as f:
        xlsx_raw = json.load(f)
    xlsx = {int(k): v for k, v in xlsx_raw.items()}

    # API: заказы
    print("Загружаю заказы (flag=0)...")
    date_from_api = DATE_FROM - timedelta(days=3)  # чуть раньше для надёжности
    url = f"{STATS_HOST}/api/v1/supplier/orders"
    headers = {"Authorization": f"Bearer {WB_TOKEN}"}
    params = {"dateFrom": date_from_api.isoformat(), "flag": 0}
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    orders = resp.json()
    print(f"Всего строк: {len(orders)}")

    print("Жду 62 сек...")
    time.sleep(62)

    # API: продажи
    print("Загружаю продажи (flag=0)...")
    url2 = f"{STATS_HOST}/api/v1/supplier/sales"
    resp2 = requests.get(url2, headers=headers, params=params, timeout=30)
    resp2.raise_for_status()
    sales = resp2.json()
    print(f"Всего строк: {len(sales)}")

    # Фильтр 24-30 марта и группировка по nmId
    api_orders = defaultdict(lambda: {"rows": 0, "priceWithDisc": 0.0, "name": ""})
    api_sales = defaultdict(lambda: {"rows": 0, "forPay": 0.0})

    for o in orders:
        dt = parse_date(o.get("date"))
        if dt and DATE_FROM.date() <= dt.date() <= DATE_TO.date():
            nm = o.get("nmId")
            if nm:
                api_orders[nm]["rows"] += 1
                api_orders[nm]["priceWithDisc"] += o.get("priceWithDisc", 0)
                api_orders[nm]["name"] = o.get("subject", "")[:25]

    for s in sales:
        dt = parse_date(s.get("date"))
        if dt and DATE_FROM.date() <= dt.date() <= DATE_TO.date():
            nm = s.get("nmId")
            if nm:
                api_sales[nm]["rows"] += 1
                api_sales[nm]["forPay"] += s.get("forPay", 0)

    # Сравнение
    all_nms = sorted(set(list(xlsx.keys()) + list(api_orders.keys())))

    mismatches = []
    only_xlsx = []
    only_api = []
    matches = 0

    for nm in all_nms:
        x = xlsx.get(nm)
        a = api_orders.get(nm)

        x_qty = x["orders_qty"] if x else 0
        a_qty = a["rows"] if a else 0

        if x_qty == 0 and a_qty == 0:
            continue

        if x_qty > 0 and a_qty == 0:
            only_xlsx.append((nm, x))
        elif x_qty == 0 and a_qty > 0:
            only_api.append((nm, a))
        elif x_qty != a_qty:
            mismatches.append((nm, x_qty, a_qty, x, a))
        else:
            matches += 1

    # Формируем сообщение
    msg = f"<b>📋 Детальное сравнение API vs XLSX</b>\n"
    msg += f"<b>Период: 24-30 марта 2026</b>\n\n"

    total_api = sum(a["rows"] for a in api_orders.values())
    total_xlsx = sum(x["orders_qty"] for x in xlsx.values())
    msg += f"API строк (заказы): {total_api}\n"
    msg += f"XLSX штук (заказы): {total_xlsx}\n"
    msg += f"Разница: {total_xlsx - total_api} шт\n\n"

    msg += f"✅ Совпадений: {matches} артикулов\n"
    msg += f"⚠️ Расхождений: {len(mismatches)} артикулов\n"
    msg += f"📦 Только в XLSX: {len(only_xlsx)} артикулов\n"
    msg += f"🔵 Только в API: {len(only_api)} артикулов\n\n"

    if mismatches:
        msg += "<b>⚠️ РАСХОЖДЕНИЯ (XLSX шт ≠ API строк):</b>\n"
        mismatches.sort(key=lambda x: x[1] - x[2], reverse=True)
        for nm, x_qty, a_qty, x, a in mismatches[:15]:
            name = (x["name"] if x else a["name"])[:25]
            diff = x_qty - a_qty
            msg += f"  {nm} ({name})\n"
            msg += f"    XLSX: {x_qty} шт → API: {a_qty} строк (разн: {diff:+d})\n"

    if only_xlsx:
        msg += f"\n<b>📦 ТОЛЬКО В XLSX ({len(only_xlsx)}):</b>\n"
        for nm, x in only_xlsx[:10]:
            msg += f"  {nm}: {x['orders_qty']} шт, {x['orders_sum']:,.0f} ₽\n"

    if only_api:
        msg += f"\n<b>🔵 ТОЛЬКО В API ({len(only_api)}):</b>\n"
        for nm, a in only_api[:10]:
            msg += f"  {nm}: {a['rows']} строк\n"

    msg += "\n<b>ВЫВОД:</b>\n"
    msg += f"Если разница = XLSX больше API на ~{total_xlsx - total_api} шт,\n"
    msg += "то это заказы по 2+ штуки в одной позиции.\n"
    msg += "API даёт 1 строку, XLSX считает штуки."

    print(msg)
    send_telegram(msg)
    print("\n✅ Отправлено")


if __name__ == "__main__":
    main()
