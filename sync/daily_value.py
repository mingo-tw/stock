#!/usr/bin/env python3
"""
每日資產紀錄：更新所有持股的收盤價與美元匯率，計算當日總資產並記錄到 JSONBin。

不需要登入券商（持股數量沿用網站上最後一次的資料），只需要：
  JSONBIN_BIN_ID, JSONBIN_MASTER_KEY

報價來源：FinMind（台股 TaiwanStockPrice、美股 USStockPrice、匯率 TaiwanExchangeRate）。
同一天執行多次，只保留最新一筆。
"""
from __future__ import annotations

import datetime as dt
import sys

import requests

from sync_holdings import (
    TZ, fetch_usd_twd, load_bin, log, num, save_bin, upsert_daily_record,
)

FINMIND = "https://api.finmindtrade.com/api/v4/data"
TW_CODE_OK = lambda c: c[:4].isdigit()  # 台股代號開頭是數字（0050、00687B、2330…）


def finmind_close(code: str, market: str) -> float | None:
    dataset = "TaiwanStockPrice" if market == "tw" else "USStockPrice"
    start = (dt.date.today() - dt.timedelta(days=14)).isoformat()
    try:
        r = requests.get(FINMIND, params={"dataset": dataset, "data_id": code, "start_date": start}, timeout=20)
        rows = [x for x in r.json().get("data", []) if num(x.get("close", x.get("Close"))) > 0]
        if rows:
            return num(rows[-1].get("close", rows[-1].get("Close")))
    except Exception as e:  # noqa: BLE001
        log(f"   {code} 報價失敗：{e}")
    return None


def main() -> int:
    now = dt.datetime.now(TZ)
    record = load_bin()
    state = record.get("state") or {}
    history = record.get("history") or []
    if not state.get("tw") and not state.get("us"):
        log("JSONBin 裡沒有持倉資料，結束。")
        return 1

    updated, failed = 0, []
    for market in ("tw", "us"):
        for h in state.get(market, []):
            code = str(h.get("code") or "").strip().upper()
            if not code or (market == "tw" and not TW_CODE_OK(code)) or len(code) > 10:
                continue  # 沒代號、選擇權等抓不到收盤價的略過，沿用原價
            p = finmind_close(code, market)
            if p:
                h["price"] = p
                updated += 1
            else:
                failed.append(code)

    fx = fetch_usd_twd()
    if fx:
        state["fx"] = fx

    if updated == 0:
        log("❌ 所有報價都抓不到，不寫入。")
        return 2

    state["lastPriceUpdate"] = now.isoformat(timespec="seconds")
    rec = upsert_daily_record(state, history, now.date().isoformat())

    # 寫入前再讀一次：如果這段期間券商同步剛好寫入新持倉，就以新持倉重算，避免覆蓋
    latest = load_bin()
    if (latest.get("state") or {}).get("lastSync") != state.get("lastSync"):
        log("偵測到期間有新的券商同步資料，改用最新持倉重新計算。")
        return main()

    save_bin({"state": state, "history": history})
    log(f"✅ 更新 {updated} 檔報價{f'、匯率 {fx}' if fx else ''}；"
        f"{rec['date']} 總資產 NT$ {rec['totalValue']:,}"
        f"（核心 {rec['core']:,}／衛星 {rec['satellite']:,}／債券 {rec['bond']:,}）")
    if failed:
        log(f"⚠️ 未更新：{'、'.join(failed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
