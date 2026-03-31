#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ⚠️ DEPRECATED — НЕ ИСПОЛЬЗОВАТЬ!
# Этот скрипт на старом Statistics API (считает строки, а не штуки).
# Актуальный скрипт: wb_report_html.py (Sales Funnel API).

import matplotlib
matplotlib.use("Agg")

import os
import asyncio
import requests
from datetime import datetime, timedelta, timezone
from io import BytesIO
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np

# ===================== CONSTANTS =====================

STATS_HOST = "https://statistics-api.wildberries.ru"
ANALYT_HOST = "https://seller-analytics-api.wildberries.ru"

WB_TOKEN = os.getenv("WB_TOKEN", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

MSK = timezone(timedelta(hours=3))

MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря"
}

COLORS = {
    "header_bg": "#1C3F6E",
    "kpi_blue": "#2563EB",
    "kpi_green": "#16A34A",
    "kpi_red": "#DC2626",
    "kpi_yellow": "#D97706",
    "table_header": "#1C3F6E",
    "table_alt": "#F8FAFC",
    "critical_bg": "#FEE2E2",
    "critical_text": "#DC2626",
    "warning_bg": "#FEF9C3",
    "warning_text": "#92400E",
}

# ===================== HELPERS =====================

def parse_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    if "Z" in s or ("+" in s[10:]) or (len(s) > 19 and s[19] == "-"):
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    else:
        dt = datetime.fromisoformat(s).replace(tzinfo=MSK)
    return dt.astimezone(MSK)


def format_date_ru(dt: datetime) -> str:
    if not dt:
        return ""
    return f"{dt.day} {MONTHS_RU[dt.month]} {dt.year}"


def hex_to_rgb(hex_color: str) -> Tuple[float, float, float]:
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i+2], 16) / 255.0 for i in (0, 2, 4))


# ===================== WB API =====================

def get_orders(date_from: datetime, wb_token: str) -> List[Dict]:
    url = f"{STATS_HOST}/api/v1/supplier/orders"
    headers = {"Authorization": f"Bearer {wb_token}"}
    params = {"dateFrom": date_from.isoformat(), "flag": 0}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"Error fetching orders: {e}")
        return []


def get_sales(date_from: datetime, wb_token: str) -> List[Dict]:
    url = f"{STATS_HOST}/api/v1/supplier/sales"
    headers = {"Authorization": f"Bearer {wb_token}"}
    params = {"dateFrom": date_from.isoformat(), "flag": 0}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"Error fetching sales: {e}")
        return []


async def get_warehouse_remains(wb_token: str) -> Dict[int, int]:
    headers = {"Authorization": f"Bearer {wb_token}"}
    url = f"{ANALYT_HOST}/api/v1/warehouse_remains"
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        request_id = data.get("data", {}).get("requestId")
        if not request_id:
            print("No requestId in warehouse_remains response")
            return {}

        for attempt in range(10):
            await asyncio.sleep(5)
            resp = requests.get(
                f"{ANALYT_HOST}/api/v1/warehouse_remains",
                headers=headers,
                params={"requestId": request_id},
                timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("data", {}).get("isFinished"):
                remains = data.get("data", {}).get("warehouseRemains", [])
                result = {}
                for item in remains:
                    sku = item.get("nmId")
                    qty = item.get("quantityFull", 0)
                    if sku:
                        result[sku] = qty
                return result

        print("Timeout waiting for warehouse_remains")
        return {}
    except Exception as e:
        print(f"Error fetching warehouse_remains: {e}")
        return {}


async def fetch_all_data(wb_token: str, date_from: datetime) -> Tuple[List, List, Dict]:
    orders = get_orders(date_from, wb_token)
    await asyncio.sleep(62)
    sales = get_sales(date_from, wb_token)
    await asyncio.sleep(62)
    remains = await get_warehouse_remains(wb_token)
    await asyncio.sleep(30)
    return orders, sales, remains


def find_report_date(orders: List[Dict], sales: List[Dict]) -> Optional[datetime]:
    dates = set()
    for item in orders + sales:
        date_str = item.get("date")
        if date_str:
            dt = parse_date(date_str)
            if dt:
                dates.add(dt.replace(hour=0, minute=0, second=0, microsecond=0))
    if not dates:
        return None
    today_msk = datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0)
    for days_back in range(1, 6):
        check_date = today_msk - timedelta(days=days_back)
        if check_date in dates:
            return check_date
    return max(dates)


@dataclass
class ProductMetrics:
    sku: int
    name: str
    orders_today: int
    sales_today: int
    cancellations_today: int
    revenue_today: float
    stock_qty: int
    days_remaining: int   # stock / avg_daily_orders_30d


def aggregate_metrics(
    orders: List[Dict],
    sales: List[Dict],
    remains: Dict[int, int],
    report_date: datetime
) -> List[ProductMetrics]:

    # 30-day velocity per SKU (non-cancelled orders)
    velocity: Dict[int, int] = {}
    for order in orders:
        if order.get("isCancel"):
            continue
        sku = order.get("nmId")
        if sku:
            velocity[sku] = velocity.get(sku, 0) + 1

    # Today's metrics
    today_metrics: Dict[int, dict] = {}

    for order in orders:
        date_str = order.get("date")
        if not date_str:
            continue
        order_date = parse_date(date_str)
        if not order_date or order_date.date() != report_date.date():
            continue
        sku = order.get("nmId")
        brand = (order.get("brand") or "").strip()
        subject = (order.get("subject") or "").strip()
        name = f"{brand} {subject}".strip() if brand or subject else (order.get("supplierArticle") or f"SKU {sku}")
        if sku not in today_metrics:
            today_metrics[sku] = {"name": name, "orders": 0, "sales": 0, "cancellations": 0, "revenue": 0.0}
        today_metrics[sku]["orders"] += 1
        today_metrics[sku]["revenue"] += order.get("priceWithDisc", 0)

    for sale in sales:
        date_str = sale.get("date")
        if not date_str:
            continue
        sale_date = parse_date(date_str)
        if not sale_date or sale_date.date() != report_date.date():
            continue
        sku = sale.get("nmId")
        brand = (sale.get("brand") or "").strip()
        subject = (sale.get("subject") or "").strip()
        name = f"{brand} {subject}".strip() if brand or subject else (sale.get("supplierArticle") or f"SKU {sku}")
        if sku not in today_metrics:
            today_metrics[sku] = {"name": name, "orders": 0, "sales": 0, "cancellations": 0, "revenue": 0.0}
        today_metrics[sku]["sales"] += 1
        if sale.get("isCancel"):
            today_metrics[sku]["cancellations"] += 1

    result = []
    for sku, data in today_metrics.items():
        stock_qty = remains.get(sku, 0)
        v30 = velocity.get(sku, 0)
        avg_daily = v30 / 30.0
        days_rem = int(stock_qty / avg_daily) if avg_daily > 0 else 9999

        result.append(ProductMetrics(
            sku=sku,
            name=data["name"],
            orders_today=data["orders"],
            sales_today=data["sales"],
            cancellations_today=data["cancellations"],
            revenue_today=data["revenue"],
            stock_qty=stock_qty,
            days_remaining=min(days_rem, 9999),
        ))

    return sorted(result, key=lambda x: x.revenue_today, reverse=True)


def get_7day_trend(
    orders: List[Dict],
    sales: List[Dict],
    report_date: datetime
) -> Tuple[List[str], List[int], List[int]]:
    daily_orders: Dict = {}
    daily_sales: Dict = {}
    for i in range(7):
        d = (report_date - timedelta(days=i)).date()
        daily_orders[d] = 0
        daily_sales[d] = 0

    for order in orders:
        dt = parse_date(order.get("date"))
        if dt and dt.date() in daily_orders:
            daily_orders[dt.date()] += 1

    for sale in sales:
        dt = parse_date(sale.get("date"))
        if dt and dt.date() in daily_sales:
            daily_sales[dt.date()] += 1

    dates, ord_list, sal_list = [], [], []
    for i in range(6, -1, -1):
        d = (report_date - timedelta(days=i)).date()
        dates.append(d.strftime("%d.%m"))
        ord_list.append(daily_orders.get(d, 0))
        sal_list.append(daily_sales.get(d, 0))
    return dates, ord_list, sal_list


# ===================== PNG GENERATION =====================

def generate_report_png(
    metrics: List[ProductMetrics],
    report_date: datetime,
    orders_count: int,
    sales_count: int,
    cancellations: int,
    revenue: float,
    dates_7d: List[str],
    orders_7d: List[int],
    sales_7d: List[int],
) -> BytesIO:

    fig = plt.figure(figsize=(13, 15), dpi=150, facecolor="white")
    gs = fig.add_gridspec(4, 1, height_ratios=[0.8, 2.5, 3, 2], hspace=0.35)

    ax_h = fig.add_subplot(gs[0])
    ax_h.axis("off")
    ax_h.text(
        0.5, 0.5,
        f"Отчёт Wildberries за {format_date_ru(report_date)}",
        fontsize=24, fontweight="bold", ha="center", va="center",
        fontfamily="DejaVu Sans",
        bbox=dict(boxstyle="round,pad=0.8",
                  facecolor=hex_to_rgb(COLORS["header_bg"]),
                  edgecolor="none"),
        color="white",
    )

    ax_k = fig.add_subplot(gs[1])
    ax_k.axis("off")
    ax_k.set_xlim(0, 4)
    ax_k.set_ylim(0, 1)

    kpis = [
        ("Заказы",     str(orders_count),            COLORS["kpi_blue"]),
        ("Продажи",    str(sales_count),              COLORS["kpi_green"]),
        ("Отмены",     str(cancellations),            COLORS["kpi_red"]),
        ("Выручка ₽", f"{revenue:,.0f}",              COLORS["kpi_yellow"]),
    ]
    for idx, (label, value, color) in enumerate(kpis):
        x = idx + 0.5
        rect = Rectangle((x - 0.4, 0.1), 0.8, 0.8,
                          linewidth=0, facecolor=hex_to_rgb(color))
        ax_k.add_patch(rect)
        ax_k.text(x, 0.72, label,
                  fontsize=11, ha="center", va="center",
                  fontfamily="DejaVu Sans", color="white", fontweight="bold")
        ax_k.text(x, 0.35, value,
                  fontsize=16, ha="center", va="center",
                  fontfamily="DejaVu Sans", color="white", fontweight="bold")

    ax_t = fig.add_subplot(gs[2])
    ax_t.axis("off")

    headers = ["Артикул", "Заказов", "Продаж", "Отмен", "Выручка ₽", "Остаток", "Дней"]
    table_data = []
    for m in metrics[:15]:
        days_str = str(m.days_remaining) if m.days_remaining < 9999 else "—"
        table_data.append([
            m.name[:20],
            str(m.orders_today),
            str(m.sales_today),
            str(m.cancellations_today),
            f"{m.revenue_today:,.0f}",
            str(m.stock_qty),
            days_str,
        ])

    tbl = ax_t.table(
        cellText=table_data, colLabels=headers,
        cellLoc="center", loc="center",
        colWidths=[0.25, 0.1, 0.1, 0.1, 0.15, 0.1, 0.1],
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(9)
    tbl.scale(1, 2)

    for i in range(len(headers)):
        cell = tbl[(0, i)]
        cell.set_facecolor(hex_to_rgb(COLORS["table_header"]))
        cell.set_text_props(weight="bold", color="white", fontfamily="DejaVu Sans")

    for row in range(1, len(table_data) + 1):
        for col in range(len(headers)):
            cell = tbl[(row, col)]
            if row % 2 == 0:
                cell.set_facecolor(hex_to_rgb(COLORS["table_alt"]))
            if col == 6:
                days_val = table_data[row - 1][6]
                if days_val != "—":
                    d = int(days_val)
                    if d <= 5:
                        cell.set_facecolor(hex_to_rgb(COLORS["critical_bg"]))
                        cell.set_text_props(color=hex_to_rgb(COLORS["critical_text"]), fontweight="bold")
                    elif d <= 14:
                        cell.set_facecolor(hex_to_rgb(COLORS["warning_bg"]))
                        cell.set_text_props(color=hex_to_rgb(COLORS["warning_text"]), fontweight="bold")
            cell.set_text_props(fontfamily="DejaVu Sans")

    ax_c = fig.add_subplot(gs[3])
    x = np.arange(len(dates_7d))
    w = 0.35
    ax_c.bar(x - w/2, orders_7d, w, label="Заказы",
             color=hex_to_rgb(COLORS["kpi_blue"]), alpha=0.85)
    ax_c.bar(x + w/2, sales_7d, w, label="Продажи",
             color=hex_to_rgb(COLORS["kpi_green"]), alpha=0.85)
    ax_c.set_xticks(x)
    ax_c.set_xticklabels(dates_7d, fontfamily="DejaVu Sans")
    ax_c.set_xlabel("Дата", fontsize=11, fontfamily="DejaVu Sans", fontweight="bold")
    ax_c.set_ylabel("Количество", fontsize=11, fontfamily="DejaVu Sans", fontweight="bold")
    ax_c.set_title("Тренд за 7 дней", fontsize=13, fontfamily="DejaVu Sans",
                   fontweight="bold", pad=10)
    ax_c.legend(fontsize=10, loc="upper left", framealpha=0.9)
    ax_c.grid(axis="y", alpha=0.3, linestyle="--")
    ax_c.set_axisbelow(True)

    buf = BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    buf.seek(0)
    plt.close(fig)
    return buf


# ===================== TELEGRAM =====================

def send_telegram_photo(buf: BytesIO, caption: str, bot_token: str, chat_id: str) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    try:
        resp = requests.post(
            url,
            files={"photo": ("report.png", buf, "image/png")},
            data={"chat_id": chat_id, "caption": caption},
            timeout=30,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"Error sending Telegram photo: {e}")
        return False


# ===================== MAIN =====================

async def main():
    if not WB_TOKEN or not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("Missing env vars: WB_TOKEN, TG_BOT_TOKEN, TG_CHAT_ID")
        return

    date_from = datetime.now(MSK) - timedelta(days=30)
    print(f"Fetching WB data from {date_from.isoformat()} ...")

    orders, sales, remains = await fetch_all_data(WB_TOKEN, date_from)
    print(f"Got {len(orders)} orders, {len(sales)} sales, {len(remains)} SKUs in stock")

    # ===== DEBUG: Sales Funnel (замена nm-report) =====
    yesterday = (datetime.now(MSK) - timedelta(days=1)).date()
    yesterday_str = str(yesterday)
    headers_auth = {"Authorization": f"Bearer {WB_TOKEN}"}

    # Сначала получаем ВСЕ nmIDs через Content API
    print(f"\n=== DEBUG: Content API — get all nmIDs ===")
    await asyncio.sleep(62)  # rate limit
    all_nm_ids = []
    cursor = {"limit": 100, "updatedAt": None, "nmID": None}
    for page in range(20):
        body = {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}}
        try:
            cr = requests.post(
                "https://content-api.wildberries.ru/content/v2/get/cards/list",
                headers=headers_auth, json=body, timeout=30)
            if not cr.ok:
                print(f"  Content API error: {cr.status_code} {cr.text[:200]}")
                break
            cdata = cr.json()
            cards = cdata.get("cards", [])
            if not cards:
                break
            for c in cards:
                nm = c.get("nmID")
                if nm:
                    all_nm_ids.append(nm)
            cursor = cdata.get("cursor", {})
            if not cursor.get("nmID"):
                break
        except Exception as e:
            print(f"  Content API exception: {e}")
            break
    print(f"  Total nmIDs from Content API: {len(all_nm_ids)}")

    print(f"\n=== DEBUG: Sales Funnel history for {yesterday} ===")
    await asyncio.sleep(62)  # rate limit

    funnel_url = f"{ANALYT_HOST}/api/analytics/v3/sales-funnel/products/history"
    funnel_body = {
        "selectedPeriod": {
            "start": yesterday_str,
            "end": yesterday_str,
        },
        "nmIds": all_nm_ids,
        "skipDeletedNm": True,
        "aggregationLevel": "day",
    }

    try:
        resp = requests.post(funnel_url, headers=headers_auth, json=funnel_body, timeout=30)
        print(f"POST {funnel_url}")
        print(f"  Status: {resp.status_code}")
        if resp.ok:
            data = resp.json()
            print(f"  Response keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
            items = data.get("data", []) if isinstance(data, dict) else []
            print(f"  Products: {len(items)}")

            total_orders = 0
            total_order_sum = 0
            total_buyouts = 0
            total_buyout_sum = 0

            for item in items:
                prod = item.get("product", {})
                nm = prod.get("nmId", "?")
                title = prod.get("title", "?")[:40]
                hist = item.get("history", [])
                for day in hist:
                    oc = day.get("orderCount", 0)
                    os_ = day.get("orderSum", 0)
                    bc = day.get("buyoutCount", 0)
                    bs = day.get("buyoutSum", 0)
                    total_orders += oc
                    total_order_sum += os_
                    total_buyouts += bc
                    total_buyout_sum += bs
                    if oc > 0:
                        print(f"  nm={nm} '{title}' orders={oc} sum={os_} buyouts={bc}")

            print(f"\n  ИТОГО: orders={total_orders} sum={total_order_sum:.0f}")
            print(f"         buyouts={total_buyouts} buyout_sum={total_buyout_sum:.0f}")
            print(f"  (кабинет: 56 заказов)")

            if total_orders > 0:
                print("  *** SALES FUNNEL РАБОТАЕТ! ***")
        else:
            print(f"  Error: {resp.text[:300]}")
    except Exception as e:
        print(f"  Exception: {e}")

    # ===== DEBUG: Stocks on WB warehouses (замена warehouse_remains) =====
    print(f"\n=== DEBUG: Stocks WB warehouses ===")
    await asyncio.sleep(62)  # rate limit

    stocks_url = f"{ANALYT_HOST}/api/analytics/v1/stocks-report/wb-warehouses"
    stocks_body = {
        "limit": 1000,
        "offset": 0,
    }

    try:
        resp = requests.post(stocks_url, headers=headers_auth, json=stocks_body, timeout=30)
        print(f"POST {stocks_url}")
        print(f"  Status: {resp.status_code}")
        if resp.ok:
            data = resp.json()
            print(f"  Response keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
            items_data = data.get("data", {})
            items = items_data.get("items", []) if isinstance(items_data, dict) else []
            print(f"  Stock rows: {len(items)}")

            # Агрегируем по nmId
            stock_by_nm = {}
            for it in items:
                nm = it.get("nmId", 0)
                qty = it.get("quantity", 0)
                wh = it.get("warehouseName", "?")
                stock_by_nm[nm] = stock_by_nm.get(nm, 0) + qty
                if qty > 0:
                    print(f"  nm={nm} wh={wh} qty={qty}")

            total_stock = sum(stock_by_nm.values())
            print(f"\n  ИТОГО: {len(stock_by_nm)} SKU, {total_stock} шт на складах WB")

            if total_stock > 0:
                print("  *** STOCKS РАБОТАЕТ! ***")
        else:
            print(f"  Error: {resp.text[:300]}")
    except Exception as e:
        print(f"  Exception: {e}")

    print("\n=== END DEBUG ===")

    report_date = find_report_date(orders, sales)
    if not report_date:
        print("No data found — aborting")
        return
    print(f"Report date: {format_date_ru(report_date)}")

    metrics = aggregate_metrics(orders, sales, remains, report_date)

    total_orders = sum(m.orders_today for m in metrics)
    total_sales = sum(m.sales_today for m in metrics)
    total_cancels = sum(m.cancellations_today for m in metrics)
    total_revenue = sum(m.revenue_today for m in metrics)

    print(f"Orders={total_orders} Sales={total_sales} Cancels={total_cancels} Revenue={total_revenue:.2f} ₽")

    dates_7d, orders_7d, sales_7d = get_7day_trend(orders, sales, report_date)

    print("Generating PNG ...")
    buf = generate_report_png(
        metrics, report_date,
        total_orders, total_sales, total_cancels, total_revenue,
        dates_7d, orders_7d, sales_7d,
    )

    caption = (
        f"📊 WB отчёт — {format_date_ru(report_date)}\n\n"
        f"Заказы: {total_orders}\n"
        f"Продажи: {total_sales}\n"
        f"Отмены: {total_cancels}\n"
        f"Выручка: {total_revenue:,.0f} ₽"
    )

    print("Sending to Telegram ...")
    ok = send_telegram_photo(buf, caption, TG_BOT_TOKEN, TG_CHAT_ID)
    print("Done ✓" if ok else "Failed ✗")


if __name__ == "__main__":
    asyncio.run(main())
