#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WB Daily Report — HTML→PNG version (dark dashboard).
Generates a styled HTML report, screenshots it with Playwright, sends PNG to Telegram.
"""

import os
import asyncio
import requests
import json
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple
from pathlib import Path

# ===================== CONSTANTS =====================

ANALYT_HOST = "https://seller-analytics-api.wildberries.ru"
CONTENT_HOST = "https://content-api.wildberries.ru"

WB_TOKEN = os.getenv("WB_TOKEN", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

MSK = timezone(timedelta(hours=3))

MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}

DAYS_RU = {
    0: "понедельник", 1: "вторник", 2: "среда", 3: "четверг",
    4: "пятница", 5: "суббота", 6: "воскресенье",
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


def format_date_full(dt: datetime) -> str:
    if not dt:
        return ""
    day_name = DAYS_RU[dt.weekday()]
    return f"{dt.day} {MONTHS_RU[dt.month]} {dt.year}, {day_name}"


def fmt_number(n) -> str:
    """Format number with space as thousands separator (Russian style)."""
    if isinstance(n, float):
        return f"{n:,.0f}".replace(",", " ")
    return f"{n:,}".replace(",", " ")


# ===================== WB API =====================

def _request_with_retry(method: str, url: str, headers: dict, max_retries: int = 3, **kwargs) -> requests.Response:
    """HTTP request with exponential backoff on 429 / 5xx."""
    kwargs.setdefault("timeout", 30)
    for attempt in range(max_retries):
        resp = requests.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = min(5 * (2 ** attempt), 60)  # 5, 10, 20, 40, 60
            print(f"HTTP {resp.status_code} — retrying in {wait}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


def get_all_nm_ids(wb_token: str) -> List[int]:
    """Получить все nmId через Content API (с пагинацией)."""
    headers = {"Authorization": f"Bearer {wb_token}"}
    nm_ids = []
    cursor = {"limit": 100, "updatedAt": None, "nmID": None}

    for _ in range(20):
        body = {
            "settings": {
                "cursor": {k: v for k, v in cursor.items() if v is not None},
                "filter": {"withPhoto": -1},
            }
        }
        try:
            resp = _request_with_retry(
                "POST", f"{CONTENT_HOST}/content/v2/get/cards/list",
                headers=headers, json=body,
            )
            data = resp.json()
        except Exception as e:
            print(f"Error fetching cards: {e}")
            break

        cards = data.get("cards", [])
        if not cards:
            break

        for c in cards:
            nm = c.get("nmID")
            if nm:
                nm_ids.append(nm)

        cursor_data = data.get("cursor", {})
        cursor["updatedAt"] = cursor_data.get("updatedAt")
        cursor["nmID"] = cursor_data.get("nmID")
        total = cursor_data.get("total", 0)
        print(f"  Content API: {len(nm_ids)} / {total} cards ...")
        if len(nm_ids) >= total or len(cards) < 100:
            break

    print(f"Content API: {len(nm_ids)} nmIds loaded")
    return nm_ids


@dataclass
class FunnelProduct:
    """Данные одного товара из Sales Funnel API."""
    nm_id: int
    title: str
    order_count: int      # штуки заказов (= кабинет WB)
    order_sum: float      # сумма заказов (= кабинет WB)
    buyout_count: int     # штуки выкупов
    buyout_sum: float     # сумма выкупов
    cancel_count: int     # отмены
    views: int            # просмотры карточки
    cart_adds: int        # добавления в корзину
    history: List[Dict]   # [{dt, orderCount, orderSum, buyoutCount, buyoutSum, openCardCount, ...}, ...]


async def get_sales_funnel(
    wb_token: str, date_from: str, date_to: str, nm_ids: List[int] = None
) -> List[FunnelProduct]:
    """
    NM Report Detail History API — статистика карточек товаров по дням.
    Возвращает ordersCount (штуки!) и ordersSumRub, совпадающие с кабинетом WB.
    """
    headers = {"Authorization": wb_token}
    url = f"{ANALYT_HOST}/api/v2/nm-report/detail/history"
    results = []
    errors = 0

    # Формат дат: "YYYY-MM-DD HH:MM:SS"
    period_begin = f"{date_from} 00:00:00"
    period_end = f"{date_to} 23:59:59"

    page = 1
    max_pages = 10
    all_items_count = 0

    while page <= max_pages:
        if page > 1:
            await asyncio.sleep(6)

        body = {
            "nmIDs": [],  # пустой = все товары продавца
            "period": {"begin": period_begin, "end": period_end},
            "timezone": "Europe/Moscow",
            "page": page,
        }
        try:
            resp = _request_with_retry(
                "POST", url, headers=headers, json=body, max_retries=5,
            )
            print(f"  [page {page}] HTTP {resp.status_code}")

            if not resp.ok:
                print(f"  [page {page}] Response: {resp.text[:500]}")
                errors += 1
                break

            data = resp.json()

            # Детальный лог первой страницы
            if page == 1:
                print(f"  [page 1] Response keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
                print(f"  [page 1] Full response (first 800 chars): {str(data)[:800]}")

            # nm-report/detail/history: data.cards[] или data.data[]
            cards = []
            if isinstance(data, dict):
                cards = data.get("data", data.get("cards", []))
                if isinstance(cards, dict):
                    cards = cards.get("cards", [])
            if not cards:
                if page == 1:
                    print(f"  [page 1] No cards in response")
                break

            all_items_count += len(cards)
            print(f"  [page {page}] {len(cards)} cards")

            for card in cards:
                nm_id = card.get("nmID", 0)
                title = card.get("vendorCode", f"SKU {nm_id}")

                # nm-report использует statistics.history[]
                stats = card.get("statistics", card)
                history = stats.get("history", [])

                # Нормализуем поля history (nm-report: ordersCount → orderCount и т.д.)
                normalized_history = []
                total_orders = 0
                total_order_sum = 0.0
                total_buyouts = 0
                total_buyout_sum = 0.0
                total_cancels = 0
                total_views = 0
                total_carts = 0

                for d in history:
                    oc = d.get("ordersCount", d.get("orderCount", 0))
                    os_ = d.get("ordersSumRub", d.get("orderSum", 0))
                    bc = d.get("buyoutsCount", d.get("buyoutCount", 0))
                    bs = d.get("buyoutsSumRub", d.get("buyoutSum", 0))
                    cc = d.get("cancelCount", 0)
                    vc = d.get("openCardCount", 0)
                    ac = d.get("addToCartCount", 0)
                    # Дата: может быть "dt" или "date"
                    dt_val = d.get("dt", d.get("date", ""))

                    total_orders += oc
                    total_order_sum += os_
                    total_buyouts += bc
                    total_buyout_sum += bs
                    total_cancels += cc
                    total_views += vc
                    total_carts += ac

                    normalized_history.append({
                        "dt": dt_val,
                        "orderCount": oc,
                        "orderSum": os_,
                        "buyoutCount": bc,
                        "buyoutSum": bs,
                        "cancelCount": cc,
                        "openCardCount": vc,
                        "addToCartCount": ac,
                    })

                if total_orders > 0 or total_buyouts > 0:
                    results.append(FunnelProduct(
                        nm_id=nm_id,
                        title=title,
                        order_count=total_orders,
                        order_sum=total_order_sum,
                        buyout_count=total_buyouts,
                        buyout_sum=total_buyout_sum,
                        cancel_count=total_cancels,
                        views=total_views,
                        cart_adds=total_carts,
                        history=normalized_history,
                    ))

            # Если карточек меньше лимита — последняя страница
            if len(cards) < 100:
                break
            page += 1

        except Exception as e:
            print(f"  [page {page}] Exception: {e}")
            errors += 1
            break

    print(f"NM Report: {all_items_count} total cards, {len(results)} with activity, {errors} errors")
    return results


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
            resp = _request_with_retry(
                "GET", f"{ANALYT_HOST}/api/v1/warehouse_remains",
                headers=headers, params={"requestId": request_id},
            )
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


# ===================== DATA LOGIC =====================

@dataclass
class ProductMetrics:
    sku: int
    name: str
    orders_today: int       # штуки (из Sales Funnel — совпадает с кабинетом!)
    sales_today: int        # штуки выкупов
    cancellations_today: int
    revenue_today: float    # сумма заказов (из Sales Funnel — совпадает с кабинетом!)
    stock_qty: int
    days_remaining: int


@dataclass
class DaySummary:
    """Итоги за один день (для сравнения вчера vs позавчера)."""
    orders: int = 0
    sales: int = 0
    cancels: int = 0
    revenue: float = 0.0
    views: int = 0
    cart_adds: int = 0


def get_day_summaries(
    funnel: List[FunnelProduct], report_date_str: str,
) -> Tuple[DaySummary, DaySummary]:
    """Возвращает (вчера, позавчера) для сравнения."""
    report_dt = datetime.strptime(report_date_str, "%Y-%m-%d").date()
    prev_date_str = (report_dt - timedelta(days=1)).isoformat()

    today = DaySummary()
    prev = DaySummary()

    for prod in funnel:
        for day in prod.history:
            dt = day.get("dt", "")[:10]
            if dt == report_date_str:
                today.orders += day.get("orderCount", 0)
                today.sales += day.get("buyoutCount", 0)
                today.cancels += day.get("cancelCount", 0)
                today.revenue += day.get("orderSum", 0)
                today.views += day.get("openCardCount", 0)
                today.cart_adds += day.get("addToCartCount", 0)
            elif dt == prev_date_str:
                prev.orders += day.get("orderCount", 0)
                prev.sales += day.get("buyoutCount", 0)
                prev.cancels += day.get("cancelCount", 0)
                prev.revenue += day.get("orderSum", 0)
                prev.views += day.get("openCardCount", 0)
                prev.cart_adds += day.get("addToCartCount", 0)

    return today, prev


def build_metrics_from_funnel(
    funnel: List[FunnelProduct],
    remains: Dict[int, int],
    report_date: str,
) -> List[ProductMetrics]:
    """Собирает метрики из Sales Funnel за конкретный день."""
    result = []

    for prod in funnel:
        day_orders = 0
        day_order_sum = 0.0
        day_buyouts = 0
        day_cancels = 0

        total_orders_all_days = prod.order_count

        for day in prod.history:
            dt = day.get("dt", "")[:10]
            if dt == report_date:
                day_orders = day.get("orderCount", 0)
                day_order_sum = day.get("orderSum", 0)
                day_buyouts = day.get("buyoutCount", 0)
                day_cancels = day.get("cancelCount", 0)

        if day_orders == 0 and day_buyouts == 0:
            continue

        stock_qty = remains.get(prod.nm_id, 0)
        num_days = len(prod.history) if prod.history else 7
        avg_daily = total_orders_all_days / max(num_days, 1)
        days_rem = int(stock_qty / avg_daily) if avg_daily > 0 else 9999

        result.append(ProductMetrics(
            sku=prod.nm_id,
            name=prod.title,
            orders_today=day_orders,
            sales_today=day_buyouts,
            cancellations_today=day_cancels,
            revenue_today=day_order_sum,
            stock_qty=stock_qty,
            days_remaining=min(days_rem, 9999),
        ))

    return sorted(result, key=lambda x: x.revenue_today, reverse=True)


def get_7day_trend_from_funnel(
    funnel: List[FunnelProduct], report_date_str: str,
) -> Tuple[List[str], List[int], List[int]]:
    """Тренд за 7 дней из Sales Funnel history."""
    report_dt = datetime.strptime(report_date_str, "%Y-%m-%d").date()

    # Инициализация 7 дней
    daily_orders: Dict = {}
    daily_buyouts: Dict = {}
    for i in range(7):
        d = (report_dt - timedelta(days=i)).isoformat()
        daily_orders[d] = 0
        daily_buyouts[d] = 0

    for prod in funnel:
        for day in prod.history:
            dt = day.get("dt", "")[:10]
            if dt in daily_orders:
                daily_orders[dt] += day.get("orderCount", 0)
                daily_buyouts[dt] += day.get("buyoutCount", 0)

    dates, ord_list, sal_list = [], [], []
    for i in range(6, -1, -1):
        d = (report_dt - timedelta(days=i)).isoformat()
        d_short = datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m")
        dates.append(d_short)
        ord_list.append(daily_orders.get(d, 0))
        sal_list.append(daily_buyouts.get(d, 0))

    return dates, ord_list, sal_list


# ===================== HTML REPORT =====================

def _delta_html(current: int, previous: int) -> str:
    """Стрелка с процентом изменения: ↑12% или ↓5%."""
    if previous == 0:
        return ""
    pct = (current - previous) / previous * 100
    if abs(pct) < 1:
        return '<span style="color:var(--text-muted);font-size:12px"> →0%</span>'
    if pct > 0:
        return f'<span style="color:var(--accent-green);font-size:12px"> ↑{pct:.0f}%</span>'
    return f'<span style="color:var(--accent-red);font-size:12px"> ↓{abs(pct):.0f}%</span>'


def _delta_html_float(current: float, previous: float) -> str:
    if previous == 0:
        return ""
    pct = (current - previous) / previous * 100
    if abs(pct) < 1:
        return '<span style="color:var(--text-muted);font-size:12px"> →0%</span>'
    if pct > 0:
        return f'<span style="color:var(--accent-green);font-size:12px"> ↑{pct:.0f}%</span>'
    return f'<span style="color:var(--accent-red);font-size:12px"> ↓{abs(pct):.0f}%</span>'


def build_html_report(
    metrics: List[ProductMetrics],
    report_date: datetime,
    orders_count: int,
    sales_count: int,
    cancellations: int,
    revenue: float,
    dates_7d: List[str],
    orders_7d: List[int],
    sales_7d: List[int],
    today_summary: Optional['DaySummary'] = None,
    prev_summary: Optional['DaySummary'] = None,
) -> str:

    # Table rows
    table_rows = ""
    for i, m in enumerate(metrics[:20]):
        days_str = str(m.days_remaining) if m.days_remaining < 9999 else "—"
        if m.days_remaining <= 5:
            days_html = f'<span class="days-badge days-critical">{days_str}</span>'
        elif m.days_remaining <= 14:
            days_html = f'<span class="days-badge days-warning">{days_str}</span>'
        else:
            days_html = f'<span class="days-badge days-ok">{days_str}</span>'

        row_class = ' class="alt"' if i % 2 == 1 else ""
        table_rows += f"""
        <tr{row_class}>
          <td class="left">{m.name[:35]}</td>
          <td>{m.orders_today}</td>
          <td>{m.sales_today}</td>
          <td>{m.cancellations_today}</td>
          <td>{fmt_number(m.revenue_today)} ₽</td>
          <td>{days_html}</td>
        </tr>"""

    # Top-5 for horizontal bar chart
    top5 = metrics[:5]
    top5_labels = json.dumps([m.name[:20] for m in top5], ensure_ascii=False)
    top5_values = json.dumps([round(m.revenue_today) for m in top5])

    chart_labels = json.dumps(dates_7d, ensure_ascii=False)
    chart_orders = json.dumps(orders_7d)
    chart_sales = json.dumps(sales_7d)

    # Дельты (вчера vs позавчера)
    orders_delta = _delta_html(orders_count, prev_summary.orders) if prev_summary else ""
    sales_delta = _delta_html(sales_count, prev_summary.sales) if prev_summary else ""
    cancel_delta = _delta_html(cancellations, prev_summary.cancels) if prev_summary else ""
    revenue_delta = _delta_html_float(revenue, prev_summary.revenue) if prev_summary else ""
    buyout_pct = round(sales_count / orders_count * 100) if orders_count > 0 else 0

    date_full = format_date_full(report_date)
    time_now = datetime.now(MSK).strftime("%H:%M")

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WB Report — {format_date_ru(report_date)}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.1"></script>
<style>
:root {{
  --bg-body: #0f111a;
  --bg-card: #1a1d2e;
  --bg-card-hover: #222640;
  --bg-table-alt: rgba(255,255,255,0.02);
  --border: rgba(255,255,255,0.06);
  --text-primary: #e8eaf0;
  --text-secondary: rgba(255,255,255,0.5);
  --text-muted: rgba(255,255,255,0.3);
  --accent-blue: #6366f1;
  --accent-green: #22c55e;
  --accent-red: #ef4444;
  --accent-amber: #f59e0b;
  --accent-blue-bg: rgba(99,102,241,0.12);
  --accent-green-bg: rgba(34,197,94,0.12);
  --accent-red-bg: rgba(239,68,68,0.12);
  --accent-amber-bg: rgba(245,158,11,0.12);
  --radius: 12px;
  --radius-sm: 8px;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', Roboto, sans-serif;
  background: var(--bg-body);
  color: var(--text-primary);
  padding: 20px;
  width: 820px;
  -webkit-font-smoothing: antialiased;
}}
.dashboard {{ max-width: 780px; margin: 0 auto; }}

.header {{
  display: flex; align-items: center; justify-content: space-between;
  padding: 24px 28px;
  background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius);
  margin-bottom: 16px;
}}
.header-left h1 {{ font-size: 20px; font-weight: 700; letter-spacing: -0.3px; }}
.header-left .date {{ font-size: 13px; color: var(--text-secondary); margin-top: 2px; }}
.header-badge {{
  display: flex; align-items: center; gap: 6px;
  padding: 6px 14px;
  background: var(--accent-green-bg); border: 1px solid rgba(34,197,94,0.2);
  border-radius: 20px; font-size: 12px; font-weight: 600; color: var(--accent-green);
}}
.header-badge .dot {{ width: 6px; height: 6px; background: var(--accent-green); border-radius: 50%; }}

.kpi-row {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 16px; }}
.kpi-card {{
  background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 20px; position: relative; overflow: hidden;
}}
.kpi-card::before {{ content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px; }}
.kpi-card.blue::before {{ background: var(--accent-blue); }}
.kpi-card.green::before {{ background: var(--accent-green); }}
.kpi-card.red::before {{ background: var(--accent-red); }}
.kpi-card.amber::before {{ background: var(--accent-amber); }}
.kpi-icon {{
  width: 36px; height: 36px; border-radius: var(--radius-sm);
  display: flex; align-items: center; justify-content: center;
  font-size: 16px; margin-bottom: 14px;
}}
.kpi-card.blue .kpi-icon  {{ background: var(--accent-blue-bg); color: var(--accent-blue); }}
.kpi-card.green .kpi-icon {{ background: var(--accent-green-bg); color: var(--accent-green); }}
.kpi-card.red .kpi-icon   {{ background: var(--accent-red-bg); color: var(--accent-red); }}
.kpi-card.amber .kpi-icon {{ background: var(--accent-amber-bg); color: var(--accent-amber); }}
.kpi-label {{
  font-size: 12px; font-weight: 500; color: var(--text-secondary);
  text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 6px;
}}
.kpi-value {{ font-size: 26px; font-weight: 700; letter-spacing: -0.5px; }}

.charts-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }}
.chart-card {{
  background: var(--bg-card); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 20px 24px;
}}
.chart-card h3 {{
  font-size: 13px; font-weight: 600; color: var(--text-secondary);
  text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 16px;
}}
.chart-wrap {{ position: relative; height: 200px; }}

.table-card {{
  background: var(--bg-card); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 20px 24px; margin-bottom: 16px;
}}
.table-card h3 {{
  font-size: 13px; font-weight: 600; color: var(--text-secondary);
  text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 14px;
}}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
thead th {{
  text-align: left; padding: 10px 14px; font-size: 11px; font-weight: 600;
  color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.8px;
  border-bottom: 1px solid var(--border);
}}
thead th:not(:first-child) {{ text-align: right; }}
tbody td {{
  padding: 11px 14px; border-bottom: 1px solid var(--border); color: var(--text-primary);
}}
tbody td:not(:first-child) {{ text-align: right; }}
tbody td.left {{ font-weight: 600; color: var(--text-primary); }}
tbody tr:last-child td {{ border-bottom: none; }}
tbody tr.alt {{ background: var(--bg-table-alt); }}
.days-badge {{
  display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 12px; font-weight: 600;
}}
.days-critical {{
  background: var(--accent-red-bg); color: var(--accent-red); border: 1px solid rgba(239,68,68,0.2);
}}
.days-warning {{
  background: var(--accent-amber-bg); color: var(--accent-amber); border: 1px solid rgba(245,158,11,0.2);
}}
.days-ok {{ color: var(--text-secondary); }}

.footer {{
  display: flex; justify-content: space-between; align-items: center;
  padding: 12px 0; font-size: 11px; color: var(--text-muted);
}}
</style>
</head>
<body>
<div class="dashboard">

  <div class="header">
    <div class="header-left">
      <h1>Wildberries — Ежедневный отчёт</h1>
      <div class="date">{date_full}</div>
    </div>
    <div class="header-badge"><span class="dot"></span>Данные актуальны</div>
  </div>

  <div class="kpi-row">
    <div class="kpi-card blue">
      <div class="kpi-icon">📦</div>
      <div class="kpi-label">Заказы</div>
      <div class="kpi-value">{orders_count}{orders_delta}</div>
    </div>
    <div class="kpi-card green">
      <div class="kpi-icon">✅</div>
      <div class="kpi-label">Выкупы</div>
      <div class="kpi-value">{sales_count}{sales_delta}</div>
    </div>
    <div class="kpi-card red">
      <div class="kpi-icon">↩️</div>
      <div class="kpi-label">Отмены</div>
      <div class="kpi-value">{cancellations}{cancel_delta}</div>
    </div>
    <div class="kpi-card amber">
      <div class="kpi-icon">💰</div>
      <div class="kpi-label">Выручка</div>
      <div class="kpi-value">{fmt_number(revenue)} ₽{revenue_delta}</div>
    </div>
    <div class="kpi-card {'green' if buyout_pct >= 50 else 'amber' if buyout_pct >= 30 else 'red'}">
      <div class="kpi-icon">📊</div>
      <div class="kpi-label">% выкупа</div>
      <div class="kpi-value">{buyout_pct}%</div>
    </div>
  </div>

  <div class="charts-row">
    <div class="chart-card">
      <h3>Тренд за 7 дней</h3>
      <div class="chart-wrap"><canvas id="trendChart"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>Топ-5 по выручке</h3>
      <div class="chart-wrap"><canvas id="topChart"></canvas></div>
    </div>
  </div>

  <div class="table-card">
    <h3>Детализация по артикулам</h3>
    <table>
      <thead>
        <tr>
          <th>Товар</th>
          <th>Заказов</th>
          <th>Продаж</th>
          <th>Отмен</th>
          <th>Выручка</th>
          <th>Остаток дней</th>
        </tr>
      </thead>
      <tbody>{table_rows}
      </tbody>
    </table>
  </div>

  <div class="footer">
    <span>BMX Delo · Автоматический отчёт</span>
    <span>{time_now} МСК</span>
  </div>

</div>

<script>
Chart.defaults.color = 'rgba(255,255,255,0.5)';
Chart.defaults.borderColor = 'rgba(255,255,255,0.06)';

new Chart(document.getElementById('trendChart').getContext('2d'), {{
  type: 'bar',
  data: {{
    labels: {chart_labels},
    datasets: [
      {{
        label: 'Заказы',
        data: {chart_orders},
        backgroundColor: 'rgba(99, 102, 241, 0.7)',
        hoverBackgroundColor: 'rgba(99, 102, 241, 0.9)',
        borderRadius: 4, barPercentage: 0.65, categoryPercentage: 0.8,
      }},
      {{
        label: 'Продажи',
        data: {chart_sales},
        backgroundColor: 'rgba(34, 197, 94, 0.7)',
        hoverBackgroundColor: 'rgba(34, 197, 94, 0.9)',
        borderRadius: 4, barPercentage: 0.65, categoryPercentage: 0.8,
      }}
    ]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false, animation: false,
    plugins: {{
      legend: {{
        position: 'top', align: 'end',
        labels: {{ usePointStyle: true, pointStyle: 'circle', padding: 16, font: {{ size: 11, weight: '500' }} }}
      }},
      tooltip: {{
        backgroundColor: '#1a1d2e', titleColor: '#e8eaf0',
        bodyColor: 'rgba(255,255,255,0.7)', borderColor: 'rgba(255,255,255,0.1)',
        borderWidth: 1, cornerRadius: 8, padding: 10,
      }}
    }},
    scales: {{
      x: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 11 }} }} }},
      y: {{ beginAtZero: true, grid: {{ color: 'rgba(255,255,255,0.04)' }}, ticks: {{ font: {{ size: 11 }}, stepSize: 10 }} }}
    }}
  }}
}});

new Chart(document.getElementById('topChart').getContext('2d'), {{
  type: 'bar',
  data: {{
    labels: {top5_labels},
    datasets: [{{
      data: {top5_values},
      backgroundColor: [
        'rgba(99,102,241,0.7)', 'rgba(99,102,241,0.55)',
        'rgba(99,102,241,0.42)', 'rgba(99,102,241,0.32)', 'rgba(99,102,241,0.22)'
      ],
      hoverBackgroundColor: 'rgba(99,102,241,0.9)',
      borderRadius: 4, barPercentage: 0.7,
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false, indexAxis: 'y', animation: false,
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{
        backgroundColor: '#1a1d2e', titleColor: '#e8eaf0',
        bodyColor: 'rgba(255,255,255,0.7)', borderColor: 'rgba(255,255,255,0.1)',
        borderWidth: 1, cornerRadius: 8, padding: 10,
        callbacks: {{ label: function(ctx) {{ return ctx.parsed.x.toLocaleString('ru-RU') + ' ₽'; }} }}
      }}
    }},
    scales: {{
      x: {{
        beginAtZero: true, grid: {{ color: 'rgba(255,255,255,0.04)' }},
        ticks: {{ font: {{ size: 10 }}, callback: function(v) {{ return (v/1000).toFixed(0) + 'k'; }} }}
      }},
      y: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 11, weight: '500' }} }} }}
    }}
  }}
}});
</script>
</body>
</html>"""
    return html


# ===================== HTML → PNG =====================

async def html_to_png(html_content: str) -> bytes:
    """Convert HTML string to PNG using Playwright."""
    from playwright.async_api import async_playwright

    html_path = Path("/tmp/wb_report.html")
    html_path.write_text(html_content, encoding="utf-8")

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 820, "height": 600})
        await page.goto(f"file://{html_path}", wait_until="networkidle")
        await page.wait_for_timeout(1500)

        height = await page.evaluate("document.body.scrollHeight")
        await page.set_viewport_size({"width": 820, "height": height + 48})

        png_bytes = await page.screenshot(full_page=True, type="png")
        await browser.close()

    return png_bytes


# ===================== TELEGRAM =====================

def send_telegram_photo(png_bytes: bytes, caption: str, bot_token: str, chat_id: str) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    for attempt in range(3):
        try:
            resp = requests.post(
                url,
                files={"photo": ("report.png", BytesIO(png_bytes), "image/png")},
                data={"chat_id": chat_id, "caption": caption},
                timeout=30,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            print(f"Error sending Telegram photo (attempt {attempt + 1}/3): {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    return False


def send_telegram_error(message: str, bot_token: str, chat_id: str) -> None:
    """Send a plain text error notification to Telegram."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={"chat_id": chat_id, "text": message},
            timeout=15,
        )
    except Exception as e:
        print(f"Failed to send error notification: {e}")


# ===================== MAIN =====================

async def main():
    if not WB_TOKEN or not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("Missing env vars: WB_TOKEN, TG_BOT_TOKEN, TG_CHAT_ID")
        return

    try:
        yesterday = (datetime.now(MSK) - timedelta(days=1)).date()
        report_date_str = yesterday.isoformat()  # "2026-03-30"
        date_from_str = (yesterday - timedelta(days=6)).isoformat()  # 7 дней для тренда
        report_date = datetime.combine(yesterday, datetime.min.time()).replace(tzinfo=MSK)

        print(f"Report date: {format_date_ru(report_date)}")
        print(f"Fetching data for {date_from_str} — {report_date_str} ...")

        # 1. NM Report Detail History — статистика карточек по дням
        print("Loading NM Report data ...")
        funnel = await get_sales_funnel(WB_TOKEN, date_from_str, report_date_str, [])
        if not funnel:
            send_telegram_error(
                f"⚠️ WB отчёт не сформирован: NM Report API не вернул данных.\n"
                f"Период: {date_from_str} — {report_date_str}\n"
                f"Подробности в логе GitHub Actions",
                TG_BOT_TOKEN, TG_CHAT_ID,
            )
            return

        # 3. Остатки на складах
        print("Loading warehouse remains ...")
        remains = await get_warehouse_remains(WB_TOKEN)
        print(f"Got {len(remains)} SKUs in stock")

        # 4. Метрики за вчера
        metrics = build_metrics_from_funnel(funnel, remains, report_date_str)

        if not metrics:
            send_telegram_error(
                f"⚠️ WB отчёт: за {report_date_str} нет данных по артикулам.\n"
                f"Sales Funnel вернул {len(funnel)} товаров, но за отчётную дату все orderCount=0.\n"
                f"Возможно, данные за вчера ещё не появились — попробуй запустить позже.",
                TG_BOT_TOKEN, TG_CHAT_ID,
            )
            return

        total_orders = sum(m.orders_today for m in metrics)
        total_sales = sum(m.sales_today for m in metrics)
        total_cancels = sum(m.cancellations_today for m in metrics)
        total_revenue = sum(m.revenue_today for m in metrics)

        print(f"Orders={total_orders} Sales={total_sales} Cancels={total_cancels} Revenue={total_revenue:.2f} ₽")

        # Сравнение вчера vs позавчера
        today_summary, prev_summary = get_day_summaries(funnel, report_date_str)

        dates_7d, orders_7d, sales_7d = get_7day_trend_from_funnel(funnel, report_date_str)

        print("Building HTML report ...")
        html = build_html_report(
            metrics, report_date,
            total_orders, total_sales, total_cancels, total_revenue,
            dates_7d, orders_7d, sales_7d,
            today_summary, prev_summary,
        )

        print("Converting HTML → PNG ...")
        png_bytes = await html_to_png(html)
        print(f"PNG size: {len(png_bytes):,} bytes")

        # Low stock предупреждения
        low_stock = [m for m in metrics if 0 < m.days_remaining <= 7]
        low_stock_text = ""
        if low_stock:
            items = ", ".join(f"{m.name[:20]} ({m.days_remaining}д)" for m in low_stock[:5])
            low_stock_text = f"\n\n⚠️ Заканчивается: {items}"

        buyout_pct = round(total_sales / total_orders * 100) if total_orders > 0 else 0

        caption = (
            f"📊 WB отчёт — {format_date_ru(report_date)}\n\n"
            f"📦 Заказы: {total_orders}\n"
            f"✅ Выкупы: {total_sales} ({buyout_pct}%)\n"
            f"↩️ Отмены: {total_cancels}\n"
            f"💰 Выручка: {fmt_number(total_revenue)} ₽"
            f"{low_stock_text}"
        )

        print("Sending to Telegram ...")
        ok = send_telegram_photo(png_bytes, caption, TG_BOT_TOKEN, TG_CHAT_ID)
        print("Done ✓" if ok else "Failed ✗")

    except Exception as e:
        print(f"Fatal error: {e}")
        send_telegram_error(f"⚠️ WB отчёт не сформирован: {type(e).__name__}", TG_BOT_TOKEN, TG_CHAT_ID)
        raise


if __name__ == "__main__":
    asyncio.run(main())
