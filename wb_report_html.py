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

# Chart.js local cache — downloaded once and embedded inline in HTML
_CHARTJS_CACHE_PATH = Path("/tmp/chart.js.4.5.1.min.js")
_CHARTJS_CDN_URL = "https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js"


def _get_chartjs() -> str:
    """Return Chart.js source code, downloading and caching if needed."""
    if _CHARTJS_CACHE_PATH.exists():
        return _CHARTJS_CACHE_PATH.read_text(encoding="utf-8")
    try:
        resp = requests.get(_CHARTJS_CDN_URL, timeout=30)
        resp.raise_for_status()
        _CHARTJS_CACHE_PATH.write_text(resp.text, encoding="utf-8")
        print(f"Chart.js cached at {_CHARTJS_CACHE_PATH}")
        return resp.text
    except Exception as e:
        print(f"WARNING: Failed to download Chart.js: {e}")
        return ""

ANALYT_HOST = "https://seller-analytics-api.wildberries.ru"
STATS_HOST = "https://statistics-api.wildberries.ru"
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
class StatOrder:
    """Один заказ из Statistics API /orders."""
    nm_id: int
    date: str             # YYYY-MM-DD
    subject: str          # категория
    supplier_article: str
    price_with_disc: float  # розничная цена с учётом согласованной скидки (= кабинет WB)
    total_price: float    # базовая цена до скидок
    finished_price: float # финальная цена покупателя (после СПП)
    is_cancel: bool
    g_number: str         # номер заказа (группирует товары одного заказа)


def get_statistics_orders(wb_token: str, date_from: str) -> List[StatOrder]:
    """
    Statistics API /orders — возвращает все заказы начиная с date_from.
    priceWithDisc = розничная цена с учётом согласованной скидки (совпадает с кабинетом WB).
    Не включает заказы с неподтверждённой оплатой (рассрочки) — ~20% от реальных.
    """
    headers = {"Authorization": f"Bearer {wb_token}"}
    url = f"{STATS_HOST}/api/v1/supplier/orders?dateFrom={date_from}T00:00:00&flag=0"

    try:
        resp = _request_with_retry("GET", url, headers=headers, max_retries=3)
        data = resp.json()
    except Exception as e:
        print(f"Statistics /orders error: {e}")
        return []

    results = []
    for o in data:
        results.append(StatOrder(
            nm_id=o.get("nmId", 0),
            date=o.get("date", "")[:10],
            subject=o.get("subject", ""),
            supplier_article=o.get("supplierArticle", ""),
            price_with_disc=o.get("priceWithDisc", 0),
            total_price=o.get("totalPrice", 0),
            finished_price=o.get("finishedPrice", 0),
            is_cancel=o.get("isCancel", False),
            g_number=o.get("gNumber", ""),
        ))
    print(f"Statistics /orders: {len(results)} records from {date_from}")
    return results


def get_statistics_sales(wb_token: str, date_from: str) -> List[Dict]:
    """
    Statistics API /sales — возвращает выкупы (продажи) начиная с date_from.
    """
    headers = {"Authorization": f"Bearer {wb_token}"}
    url = f"{STATS_HOST}/api/v1/supplier/sales?dateFrom={date_from}T00:00:00&flag=0"

    try:
        resp = _request_with_retry("GET", url, headers=headers, max_retries=3)
        data = resp.json()
    except Exception as e:
        print(f"Statistics /sales error: {e}")
        return []

    print(f"Statistics /sales: {len(data)} records from {date_from}")
    return data


def _build_daily_stats(
    orders: List[StatOrder], sales: List[Dict],
    catalog: Dict[int, str],
) -> Dict[str, Dict]:
    """
    Группирует заказы и выкупы по дням.
    Возвращает {date_str: {orders, cancels, revenue, sales, sales_sum, by_nm: {nm_id: {...}}}}.
    """
    from collections import defaultdict

    daily = defaultdict(lambda: {
        "orders": 0, "cancels": 0, "revenue": 0.0,
        "sales": 0, "sales_sum": 0.0,
        "by_nm": defaultdict(lambda: {
            "orders": 0, "cancels": 0, "revenue": 0.0,
            "sales": 0, "sales_sum": 0.0,
            "name": "", "subject": "",
        }),
    })

    for o in orders:
        d = daily[o.date]
        nm = d["by_nm"][o.nm_id]
        nm["name"] = nm["name"] or catalog.get(o.nm_id, o.supplier_article or f"SKU {o.nm_id}")
        nm["subject"] = nm["subject"] or o.subject
        if o.is_cancel:
            d["cancels"] += 1
            nm["cancels"] += 1
        else:
            d["orders"] += 1
            d["revenue"] += o.price_with_disc
            nm["orders"] += 1
            nm["revenue"] += o.price_with_disc

    for s in sales:
        dt = s.get("date", "")[:10]
        nm_id = s.get("nmId", 0)
        d = daily[dt]
        d["sales"] += 1
        d["sales_sum"] += s.get("priceWithDisc", 0)
        nm = d["by_nm"][nm_id]
        nm["sales"] += 1
        nm["sales_sum"] += s.get("priceWithDisc", 0)
        if not nm["name"]:
            nm["name"] = catalog.get(nm_id, s.get("supplierArticle", f"SKU {nm_id}"))

    return dict(daily)


async def get_warehouse_remains(wb_token: str) -> Tuple[Dict[int, int], bool]:
    """Returns (remains_dict, success_flag). If success_flag is False, stock data is unavailable."""
    headers = {"Authorization": f"Bearer {wb_token}"}
    url = f"{ANALYT_HOST}/api/v1/warehouse_remains"
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        request_id = data.get("data", {}).get("requestId")
        if not request_id:
            print("WARNING: No requestId in warehouse_remains response — stock data unavailable")
            return {}, False

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
                return result, True

        print("WARNING: Timeout waiting for warehouse_remains — stock data unavailable")
        return {}, False
    except Exception as e:
        print(f"WARNING: Error fetching warehouse_remains: {e} — stock data unavailable")
        return {}, False


# ===================== DATA LOGIC =====================

@dataclass
class ProductMetrics:
    sku: int
    name: str
    orders_today: int       # штуки заказов (из Statistics API)
    sales_today: int        # штуки выкупов
    cancellations_today: int
    revenue_today: float    # priceWithDisc — розничная цена с учётом согл. скидки
    stock_qty: int
    days_remaining: int


@dataclass
class DaySummary:
    """Итоги за один день (для сравнения вчера vs позавчера)."""
    orders: int = 0
    sales: int = 0
    cancels: int = 0
    revenue: float = 0.0


def get_day_summaries_from_stats(
    daily_stats: Dict[str, Dict], report_date_str: str,
) -> Tuple[DaySummary, DaySummary]:
    """Возвращает (вчера, позавчера) из Statistics API данных."""
    report_dt = datetime.strptime(report_date_str, "%Y-%m-%d").date()
    prev_date_str = (report_dt - timedelta(days=1)).isoformat()

    today = DaySummary()
    prev = DaySummary()

    d = daily_stats.get(report_date_str, {})
    today.orders = d.get("orders", 0)
    today.sales = d.get("sales", 0)
    today.cancels = d.get("cancels", 0)
    today.revenue = d.get("revenue", 0.0)

    d = daily_stats.get(prev_date_str, {})
    prev.orders = d.get("orders", 0)
    prev.sales = d.get("sales", 0)
    prev.cancels = d.get("cancels", 0)
    prev.revenue = d.get("revenue", 0.0)

    return today, prev


def build_metrics_from_stats(
    daily_stats: Dict[str, Dict],
    remains: Dict[int, int],
    report_date: str,
    num_days: int = 7,
) -> List[ProductMetrics]:
    """Собирает метрики из Statistics API за конкретный день."""
    day_data = daily_stats.get(report_date, {})
    by_nm = day_data.get("by_nm", {})

    if not by_nm:
        return []

    # Средний дневной заказ за все дни (для расчёта дней остатка)
    report_dt = datetime.strptime(report_date, "%Y-%m-%d").date()
    nm_total_orders = {}
    for i in range(num_days):
        d = (report_dt - timedelta(days=i)).isoformat()
        d_data = daily_stats.get(d, {})
        for nm_id, nm in d_data.get("by_nm", {}).items():
            nm_total_orders[nm_id] = nm_total_orders.get(nm_id, 0) + nm.get("orders", 0)

    result = []
    for nm_id, nm in by_nm.items():
        if nm["orders"] == 0 and nm["sales"] == 0:
            continue

        stock_qty = remains.get(nm_id, 0)
        total_orders = nm_total_orders.get(nm_id, 0)
        avg_daily = total_orders / max(num_days, 1)
        days_rem = int(stock_qty / avg_daily) if avg_daily > 0 else 9999

        result.append(ProductMetrics(
            sku=nm_id,
            name=nm["name"],
            orders_today=nm["orders"],
            sales_today=nm["sales"],
            cancellations_today=nm["cancels"],
            revenue_today=nm["revenue"],
            stock_qty=stock_qty,
            days_remaining=min(days_rem, 9999),
        ))

    return sorted(result, key=lambda x: x.revenue_today, reverse=True)


def get_7day_trend_from_stats(
    daily_stats: Dict[str, Dict], report_date_str: str,
) -> Tuple[List[str], List[int], List[int]]:
    """Тренд за 7 дней из Statistics API данных."""
    report_dt = datetime.strptime(report_date_str, "%Y-%m-%d").date()

    dates, ord_list, sal_list = [], [], []
    for i in range(6, -1, -1):
        d = (report_dt - timedelta(days=i)).isoformat()
        d_short = datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m")
        dates.append(d_short)

        day = daily_stats.get(d, {})
        ord_list.append(day.get("orders", 0))
        sal_list.append(day.get("sales", 0))

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

    chartjs_src = _get_chartjs()

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WB Report — {format_date_ru(report_date)}</title>
<script>{chartjs_src}</script>
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

        # 1. Каталог товаров (для названий)
        print("Loading product catalog ...")
        nm_ids = get_all_nm_ids(WB_TOKEN)
        catalog = {}  # nm_id → title (заполним из Content API)
        print(f"Catalog: {len(nm_ids)} products")

        # 2. Statistics API — заказы и выкупы
        print("Loading orders (Statistics API) ...")
        stat_orders = get_statistics_orders(WB_TOKEN, date_from_str)
        if not stat_orders:
            send_telegram_error(
                f"⚠️ WB отчёт не сформирован: Statistics API не вернул заказов.\n"
                f"Период: с {date_from_str}\n"
                f"Подробности в логе.",
                TG_BOT_TOKEN, TG_CHAT_ID,
            )
            return

        print("Loading sales (Statistics API) ...")
        stat_sales = get_statistics_sales(WB_TOKEN, date_from_str)

        # Строим каталог названий из данных заказов
        for o in stat_orders:
            if o.nm_id and o.nm_id not in catalog:
                catalog[o.nm_id] = o.supplier_article or o.subject or f"SKU {o.nm_id}"

        # 3. Группируем данные по дням
        daily_stats = _build_daily_stats(stat_orders, stat_sales, catalog)

        # 4. Остатки на складах
        print("Loading warehouse remains ...")
        remains, warehouse_ok = await get_warehouse_remains(WB_TOKEN)
        if not warehouse_ok:
            print("WARNING: Warehouse data failed — stock quantities will show as 0")
        print(f"Got {len(remains)} SKUs in stock")

        # 5. Метрики за вчера
        metrics = build_metrics_from_stats(daily_stats, remains, report_date_str)

        if not metrics:
            send_telegram_error(
                f"⚠️ WB отчёт: за {report_date_str} нет заказов.\n"
                f"Statistics API вернул {len(stat_orders)} записей, но за отчётную дату заказов нет.\n"
                f"Возможно, данные за вчера ещё не появились — попробуй запустить позже.",
                TG_BOT_TOKEN, TG_CHAT_ID,
            )
            return

        total_orders = sum(m.orders_today for m in metrics)
        total_sales = sum(m.sales_today for m in metrics)
        total_cancels = sum(m.cancellations_today for m in metrics)
        total_revenue = sum(m.revenue_today for m in metrics)

        print(f"Orders={total_orders} Sales={total_sales} Cancels={total_cancels} Revenue={total_revenue:,.0f} ₽")

        # Сравнение вчера vs позавчера
        today_summary, prev_summary = get_day_summaries_from_stats(daily_stats, report_date_str)

        dates_7d, orders_7d, sales_7d = get_7day_trend_from_stats(daily_stats, report_date_str)

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
        send_telegram_error(f"⚠️ WB отчёт не сформирован: {type(e).__name__}: {e}", TG_BOT_TOKEN, TG_CHAT_ID)
        raise


if __name__ == "__main__":
    asyncio.run(main())
