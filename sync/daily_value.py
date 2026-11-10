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
QUOTE_API = "https://mingo-quote.dnweimin.workers.dev/quote"  # 自己的 Cloudflare 轉接站
TW_CODE_OK = lambda c: c[:4].isdigit()  # 台股代號開頭是數字（0050、00687B、2330…）


def relay_closes(tw_codes: list[str], us_codes: list[str]) -> dict:
    """
    從轉接站拿「收盤價」：
      台股：證交所當天收盤價（14:30 後即為當天收盤；盤中執行則是當下成交價）
      美股：正式盤收盤價 regularPrice（不含盤前盤後）
    回傳 {("tw"|"us", code): price}
    """
    out = {}
    try:
        r = requests.get(QUOTE_API, params={"tw": ",".join(tw_codes), "us": ",".join(us_codes)}, timeout=30)
        j = r.json()
        for code, q in (j.get("tw") or {}).items():
            if isinstance(q, dict) and num(q.get("price")) > 0:
                out[("tw", code.upper())] = num(q["price"])
        for code, q in (j.get("us") or {}).items():
            if isinstance(q, dict):
                if q.get("session") == "regular":
                    # 美股正在交易中：用前一日收盤，不記盤中價
                    p = num(q.get("prevClose")) or num(q.get("regularPrice")) or num(q.get("price"))
                else:
                    p = num(q.get("regularPrice")) or num(q.get("price"))
                if p > 0:
                    out[("us", code.upper())] = p
    except Exception as e:  # noqa: BLE001
        log(f"   轉接站失敗，改用 FinMind：{e}")
    return out


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
    # GitHub 排程常延遲好幾小時；若剛好在台股交易時間執行，價格還不是收盤價，先跳過等下一次
    if now.weekday() < 5 and dt.time(8, 30) <= now.time() < dt.time(14, 30):
        log(f"現在 {now:%H:%M} 台股還在交易中，不記錄（等收盤後的排程）。")
        return 0
    record = load_bin()
    state = record.get("state") or {}
    history = record.get("history") or []
    if not state.get("tw") and not state.get("us"):
        log("JSONBin 裡沒有持倉資料，結束。")
        return 1

    def ok_code(market, code):
        return code and not (market == "tw" and not TW_CODE_OK(code)) and len(code) <= 10

    codes = {m: [str(h.get("code") or "").strip().upper() for h in state.get(m, [])] for m in ("tw", "us")}
    relay = relay_closes([c for c in codes["tw"] if ok_code("tw", c)], [c for c in codes["us"] if ok_code("us", c)])
    log(f"   轉接站取得 {len(relay)} 檔收盤價")

    updated, failed = 0, []
    for market in ("tw", "us"):
        for h in state.get(market, []):
            code = str(h.get("code") or "").strip().upper()
            if not ok_code(market, code):
                continue  # 沒代號、選擇權等抓不到收盤價的略過，沿用原價
            p = relay.get((market, code)) or finmind_close(code, market)
            if p:
                h["price"] = p
                h["priceSession"] = "close" if market == "tw" else "closed:regular"  # 網站上標示為「收盤」
                h["priceTime"] = None
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
