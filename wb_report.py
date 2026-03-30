#!/usr/bin/env python3
"""
WB Analytics — ежедневный дайджест в Telegram
Продажи | Остатки | ABC-анализ | Поисковые запросы

Запуск: python wb_report.py
Env vars: WB_TOKEN, TG_BOT_TOKEN, TG_CHAT_ID
"""

import os
import time
import json
import logging
import requests
from datetime import datetime, timedelta, timezone
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Конфиг ────────────────────────────────────────────────────────────────────
WB_TOKEN    = os.environ["WB_TOKEN"]
TG_BOT      = os.environ["TG_BOT_TOKEN"]
TG_CHAT     = os.environ["TG_CHAT_ID"]

STATS_HOST  = "https://statistics-api.wildberries.ru"
ANALYT_HOST = "https://seller-analytics-api.wildberries.ru"

HEADERS = {"Authorization": WB_TOKEN}

MSK = timezone(timedelta(hours=3))


# ── WB API helpers ────────────────────────────────────────────────────────────

def wb_get(url: str, params: dict = None, retries: int = 3) -> list | dict:
    """GET-запрос к WB API с повтором при 429."""
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=30)
            if r.status_code == 429:
                wait = 65 if attempt == 0 else 120
                log.warning(f"429 rate limit — жду {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            log.error(f"HTTP error {e} | url={url}")
            if attempt == retries - 1:
                raise
            time.sleep(10)
    return []


def wb_post(url: str, payload: dict, retries: int = 3) -> dict:
    """POST-запрос к WB API."""
    for attempt in range(retries):
        try:
            r = requests.post(url, headers=HEADERS, json=payload, timeout=30)
            if r.status_code == 429:
                wait = 65
                log.warning(f"429 rate limit — жду {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            log.error(f"HTTP error {e} | url={url}")
            if attempt == retries - 1:
                raise
            time.sleep(10)
    return {}


# ── Получение данных ──────────────────────────────────────────────────────────

def get_orders(date_from: str) -> list:
    """Все заказы начиная с date_from (flag=1 = все за дату, не инкрементально)."""
    log.info(f"Загружаю заказы с {date_from}...")
    result = []
    current_date = date_from

    while True:
        data = wb_get(
            f"{STATS_HOST}/api/v1/supplier/orders",
            params={"dateFrom": current_date, "flag": 1}
        )
        if not data:
            break
        result.extend(data)
        # Если вернулось 80000 строк — есть ещё, берём lastChangeDate последней строки
        if len(data) < 80000:
            break
        current_date = data[-1]["lastChangeDate"]
        time.sleep(62)  # rate limit: 1 req/min

    log.info(f"Загружено заказов: {len(result)}")
    return result


def get_sales(date_from: str) -> list:
    """Все продажи начиная с date_from."""
    log.info(f"Загружаю продажи с {date_from}...")
    time.sleep(62)  # rate limit между запросами

    result = []
    current_date = date_from

    while True:
        data = wb_get(
            f"{STATS_HOST}/api/v1/supplier/sales",
            params={"dateFrom": current_date, "flag": 1}
        )
        if not data:
            break
        result.extend(data)
        if len(data) < 80000:
            break
        current_date = data[-1]["lastChangeDate"]
        time.sleep(62)

    log.info(f"Загружено продаж: {len(result)}")
    return result


def get_warehouse_remains() -> list:
    """Async-отчёт об остатках на складах WB."""
    log.info("Запрашиваю отчёт об остатках...")
    time.sleep(62)

    # Шаг 1: создаём задачу
    resp = wb_get(f"{ANALYT_HOST}/api/v1/warehouse_remains", params={"groupByBrand": False})
    task_id = resp.get("data", {}).get("taskId") if isinstance(resp, dict) else None

    if not task_id:
        log.error(f"Не удалось создать задачу остатков: {resp}")
        return []

    log.info(f"Задача остатков: {task_id} — жду готовности...")

    # Шаг 2: ждём готовности (до 10 минут)
    for _ in range(20):
        time.sleep(30)
        status_resp = wb_get(
            f"{ANALYT_HOST}/api/v1/warehouse_remains/tasks/{task_id}/status"
        )
        status = status_resp.get("data", {}).get("status") if isinstance(status_resp, dict) else None
        log.info(f"Статус задачи: {status}")
        if status == "done":
            break
        if status in ("error", "purged"):
            log.error(f"Задача провалилась: {status}")
            return []

    # Шаг 3: скачиваем
    result = wb_get(f"{ANALYT_HOST}/api/v1/warehouse_remains/tasks/{task_id}/download")
    data = result.get("data", []) if isinstance(result, dict) else result
    log.info(f"Загружено остатков: {len(data)} строк")
    return data if isinstance(data, list) else []


def get_search_queries(nm_ids: list) -> list:
    """Поисковые запросы по артикулам (топ-20 SKU)."""
    if not nm_ids:
        return []

    log.info(f"Загружаю поисковые запросы для {len(nm_ids)} SKU...")
    time.sleep(62)

    today = datetime.now(MSK)
    date_from = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    date_to = today.strftime("%Y-%m-%d")

    result = []
    for nm_id in nm_ids[:5]:  # берём топ-5 чтобы не убиться о rate limit
        try:
            resp = wb_post(
                f"{ANALYT_HOST}/api/v2/search-report/product/search-texts",
                payload={
                    "nmId": nm_id,
                    "dateFrom": date_from,
                    "dateTo": date_to
                }
            )
            queries = resp.get("data", {}).get("rows", []) if isinstance(resp, dict) else []
            if queries:
                result.append({"nmId": nm_id, "queries": queries[:5]})
            time.sleep(62)
        except Exception as e:
            log.warning(f"Ошибка запроса для nmId={nm_id}: {e}")

    return result


# ── Аналитика ─────────────────────────────────────────────────────────────────

def filter_yesterday(records: list, date_field: str) -> list:
    """Фильтрует записи за вчера (МСК)."""
    yesterday = (datetime.now(MSK) - timedelta(days=1)).date()
    result = []
    for r in records:
        try:
            dt_str = r.get(date_field, "")
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00")).astimezone(MSK).date()
            if dt == yesterday:
                result.append(r)
        except Exception:
            pass
    return result


def compute_sales_summary(orders: list, sales: list) -> dict:
    """Сводка продаж за вчера."""
    yesterday_orders = filter_yesterday(orders, "date")
    yesterday_sales  = filter_yesterday(sales, "date")

    total_orders   = len(yesterday_orders)
    total_sales    = len([s for s in yesterday_sales if s.get("saleID", "").startswith("S")])
    total_returns  = len([s for s in yesterday_sales if s.get("saleID", "").startswith("R")])
    revenue        = sum(s.get("priceWithDisc", 0) for s in yesterday_sales if s.get("saleID", "").startswith("S"))
    cancellations  = len([o for o in yesterday_orders if o.get("isCancel", False)])

    buyout_rate = round(total_sales / total_orders * 100) if total_orders > 0 else 0

    # Топ-10 по выручке
    by_sku = defaultdict(lambda: {"name": "", "qty": 0, "revenue": 0.0})
    for s in yesterday_sales:
        if not s.get("saleID", "").startswith("S"):
            continue
        nm = s.get("nmId", 0)
        by_sku[nm]["name"]    = s.get("subject", "") or s.get("supplierArticle", str(nm))
        by_sku[nm]["qty"]    += 1
        by_sku[nm]["revenue"] += s.get("priceWithDisc", 0)

    top_skus = sorted(by_sku.items(), key=lambda x: x[1]["revenue"], reverse=True)[:10]

    return {
        "orders":       total_orders,
        "sales":        total_sales,
        "returns":      total_returns,
        "cancellations": cancellations,
        "revenue":      revenue,
        "buyout_rate":  buyout_rate,
        "top_skus":     top_skus,
    }


def compute_abc(sales: list, days: int = 30) -> dict:
    """ABC-анализ по выручке за последние N дней."""
    cutoff = (datetime.now(MSK) - timedelta(days=days)).date()
    by_sku = defaultdict(lambda: {"name": "", "revenue": 0.0, "qty": 0})

    for s in sales:
        if not s.get("saleID", "").startswith("S"):
            continue
        try:
            dt = datetime.fromisoformat(
                s.get("date", "").replace("Z", "+00:00")
            ).astimezone(MSK).date()
            if dt < cutoff:
                continue
        except Exception:
            continue

        nm = s.get("nmId", 0)
        by_sku[nm]["name"]    = s.get("subject", "") or s.get("supplierArticle", str(nm))
        by_sku[nm]["revenue"] += s.get("priceWithDisc", 0)
        by_sku[nm]["qty"]    += 1

    if not by_sku:
        return {"A": [], "B": [], "C": [], "total_revenue": 0}

    sorted_skus   = sorted(by_sku.items(), key=lambda x: x[1]["revenue"], reverse=True)
    total_revenue = sum(v["revenue"] for _, v in sorted_skus)

    groups = {"A": [], "B": [], "C": []}
    cumulative = 0.0
    for nm, v in sorted_skus:
        cumulative += v["revenue"]
        share = cumulative / total_revenue
        if share <= 0.80:
            groups["A"].append((nm, v))
        elif share <= 0.95:
            groups["B"].append((nm, v))
        else:
            groups["C"].append((nm, v))

    return {**groups, "total_revenue": total_revenue}


def compute_stock_alerts(remains: list, sales: list, days: int = 30) -> list:
    """Критические остатки: SKU с запасом < 14 дней при текущем темпе продаж."""
    cutoff = (datetime.now(MSK) - timedelta(days=days)).date()

    # Продажи за период
    sales_by_nm = defaultdict(int)
    for s in sales:
        if not s.get("saleID", "").startswith("S"):
            continue
        try:
            dt = datetime.fromisoformat(
                s.get("date", "").replace("Z", "+00:00")
            ).astimezone(MSK).date()
            if dt >= cutoff:
                sales_by_nm[s.get("nmId", 0)] += 1
        except Exception:
            pass

    # Текущие остатки
    stock_by_nm = defaultdict(lambda: {"name": "", "qty": 0})
    for r in remains:
        nm = r.get("nmId", 0)
        stock_by_nm[nm]["name"] = r.get("subjectName", "") or r.get("supplierArticle", str(nm))
        stock_by_nm[nm]["qty"] += r.get("quantityWarehousesFull", 0)

    alerts = []
    for nm, stock in stock_by_nm.items():
        if stock["qty"] <= 0:
            continue
        daily_sales = sales_by_nm.get(nm, 0) / days
        if daily_sales <= 0:
            continue
        days_left = int(stock["qty"] / daily_sales)
        if days_left <= 14:
            alerts.append({
                "nm":       nm,
                "name":     stock["name"],
                "qty":      stock["qty"],
                "days":     days_left,
                "daily":    round(daily_sales, 1),
            })

    return sorted(alerts, key=lambda x: x["days"])


# ── Telegram ──────────────────────────────────────────────────────────────────

def fmt_money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ") + " ₽"


def send_telegram(text: str) -> None:
    """Отправляет сообщение в Telegram. Разбивает если > 4096 символов."""
    url = f"https://api.telegram.org/bot{TG_BOT}/sendMessage"
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        r = requests.post(url, json={
            "chat_id": TG_CHAT,
            "text": chunk,
            "parse_mode": "HTML"
        }, timeout=15)
        r.raise_for_status()
        if len(chunks) > 1:
            time.sleep(1)


def build_message(summary: dict, abc: dict, alerts: list, search: list) -> str:
    yesterday = (datetime.now(MSK) - timedelta(days=1)).strftime("%d %B %Y")
    lines = []

    # Заголовок
    lines.append(f"📊 <b>WB Дайджест — {yesterday}</b>")
    lines.append("")

    # Продажи
    lines.append("💰 <b>ПРОДАЖИ ВЧЕРА</b>")
    lines.append(f"Выручка: <b>{fmt_money(summary['revenue'])}</b>")
    lines.append(
        f"Заказов: {summary['orders']} | Продаж: {summary['sales']} | "
        f"Возвратов: {summary['returns']} | Отмен: {summary['cancellations']}"
    )
    if summary['orders'] > 0:
        lines.append(f"Выкуп: <b>{summary['buyout_rate']}%</b>")
    lines.append("")

    # Топ-5 SKU вчера
    if summary["top_skus"]:
        lines.append("🏆 <b>ТОП-5 по выручке вчера</b>")
        for i, (nm, v) in enumerate(summary["top_skus"][:5], 1):
            lines.append(f"{i}. {v['name']} — {v['qty']} шт / {fmt_money(v['revenue'])}")
        lines.append("")

    # Критические остатки
    if alerts:
        lines.append("📦 <b>КРИТИЧЕСКИЕ ОСТАТКИ (менее 14 дней)</b>")
        for a in alerts[:10]:
            emoji = "🔴" if a["days"] <= 5 else "🟡"
            lines.append(
                f"{emoji} {a['name']} (nmId {a['nm']}) — "
                f"{a['qty']} шт / <b>{a['days']} дн</b> "
                f"({a['daily']} шт/день)"
            )
        if len(alerts) > 10:
            lines.append(f"... и ещё {len(alerts) - 10} позиций")
        lines.append("")

    # ABC
    lines.append("🔠 <b>ABC-АНАЛИЗ (30 дней)</b>")
    lines.append(f"Итого выручка: {fmt_money(abc['total_revenue'])}")
    lines.append(f"A — {len(abc['A'])} SKU → 80% выручки")
    lines.append(f"B — {len(abc['B'])} SKU → 15% выручки")
    lines.append(f"C — {len(abc['C'])} SKU → 5% выручки (хвост)")
    if abc["A"]:
        lines.append("")
        lines.append("Группа A:")
        for nm, v in abc["A"][:10]:
            lines.append(f"  • {v['name']} — {fmt_money(v['revenue'])} / {v['qty']} шт")
        if len(abc["A"]) > 10:
            lines.append(f"  ... и ещё {len(abc['A']) - 10}")
    lines.append("")

    # Поисковые запросы
    if search:
        lines.append("🔍 <b>ТОП ПОИСКОВЫЕ ЗАПРОСЫ (7 дней)</b>")
        for item in search:
            nm = item["nmId"]
            lines.append(f"<b>nmId {nm}:</b>")
            for q in item["queries"][:3]:
                keyword  = q.get("keyword", "—")
                orders   = q.get("ordersCount", 0)
                position = q.get("avgPosition", "—")
                lines.append(f"  • «{keyword}» — {orders} заказов, позиция ~{position}")
        lines.append("")

    lines.append(f"<i>Обновлено {datetime.now(MSK).strftime('%H:%M МСК')}</i>")
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=== WB Analytics запущен ===")
    today_msk = datetime.now(MSK)
    date_30d  = (today_msk - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00")

    # 1. Заказы за 30 дней
    orders = get_orders(date_30d)

    # 2. Продажи за 30 дней
    sales = get_sales(date_30d)

    # 3. Остатки на складах
    remains = get_warehouse_remains()

    # 4. ABC по продажам
    abc = compute_abc(sales, days=30)

    # 5. Сводка за вчера
    summary = compute_sales_summary(orders, sales)

    # 6. Критические остатки
    alerts = compute_stock_alerts(remains, sales, days=30)

    # 7. Поисковые запросы для топ-SKU группы A
    top_nm_ids = [nm for nm, _ in abc["A"][:5]]
    search = get_search_queries(top_nm_ids)

    # 8. Собираем и отправляем
    msg = build_message(summary, abc, alerts, search)
    log.info("Отправляю в Telegram...")
    send_telegram(msg)
    log.info("✅ Готово!")


if __name__ == "__main__":
    main()
