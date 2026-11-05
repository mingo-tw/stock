#!/usr/bin/env python3
"""
券商持倉同步：Firstrade（美股）+ 永豐金證券 Shioaji（台股）→ JSONBin

網站 (index.html) 從同一個 JSONBin 讀資料，所以這支程式跑完、網頁重新整理就會看到最新持倉。

需要的環境變數（GitHub Actions 請放在 Settings → Secrets and variables → Actions）：
  JSONBIN_BIN_ID        JSONBin 的 Bin ID
  JSONBIN_MASTER_KEY    JSONBin 的 Master Key（請務必先換一把新的，舊的已公開在網頁原始碼裡）

  # Firstrade（不填就略過美股）
  FT_USERNAME, FT_PASSWORD
  FT_MFA_SECRET         2FA 驗證器 App 的 secret key（自動化必填，見 SETUP.md）
  FT_PIN                （選填）若帳號使用 PIN 登入

  # 永豐 Shioaji（不填就略過台股）
  SJ_API_KEY, SJ_SEC_KEY

用法：
  python sync/sync_holdings.py --dry-run      # 只印出抓到的持倉，不寫入雲端（第一次請先跑這個）
  python sync/sync_holdings.py                # 抓取並寫入 JSONBin
  python sync/sync_holdings.py --only us      # 只同步美股（或 --only tw）
  python sync/sync_holdings.py --no-snapshot  # 不自動補本週快照
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import uuid
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Asia/Taipei")
JSONBIN_URL = "https://api.jsonbin.io/v3/b/{bin_id}"

# 這些美股代號會自動歸類為「債券」；其他預設「股票」。
# 已存在網站上的標的會保留你原本設定的類別，不會被覆蓋。
US_BOND_TICKERS = {
    "TLT", "TLH", "IEF", "IEI", "SHY", "SHV", "BIL", "SGOV", "GOVT", "VGLT", "VGIT", "VGSH",
    "EDV", "ZROZ", "BND", "BNDX", "AGG", "LQD", "VCIT", "VCLT", "VCSH", "IGIB", "IGSB",
    "HYG", "JNK", "TIP", "SCHP", "VTIP", "MUB", "EMB", "TMF", "TBT", "TTT",
}

OCC_RE = re.compile(r"^([A-Z.]{1,6})\s*(\d{6})([CP])(\d{8})$")


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(TZ):%H:%M:%S}] {msg}", flush=True)


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def num(v, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return default


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------- JSONBin
def jsonbin_headers() -> dict:
    return {"X-Master-Key": env("JSONBIN_MASTER_KEY"), "Content-Type": "application/json"}


def load_bin() -> dict:
    url = JSONBIN_URL.format(bin_id=env("JSONBIN_BIN_ID")) + "/latest"
    r = requests.get(url, headers=jsonbin_headers(), timeout=30)
    r.raise_for_status()
    return r.json().get("record") or {}


def save_bin(record: dict) -> None:
    url = JSONBIN_URL.format(bin_id=env("JSONBIN_BIN_ID"))
    r = requests.put(url, headers=jsonbin_headers(), data=json.dumps(record), timeout=30)
    r.raise_for_status()


# ---------------------------------------------------------------- Firstrade (美股)
def parse_occ(symbol: str):
    """TLT261016C00085000 → ('TLT', date(2026,10,16), 'C', 85.0)"""
    m = OCC_RE.match(symbol.replace(" ", "").upper())
    if not m:
        return None
    root, ymd, cp, strike = m.groups()
    exp = dt.date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
    return root, exp, cp, int(strike) / 1000


def fetch_firstrade() -> list[dict]:
    from firstrade import account  # pip install firstrade

    ss = account.FTSession(
        username=env("FT_USERNAME"),
        password=env("FT_PASSWORD"),
        pin=env("FT_PIN"),
        mfa_secret=env("FT_MFA_SECRET"),
        email=env("FT_EMAIL"),
        phone=env("FT_PHONE"),
        save_session=True,
        profile_path=env("FT_PROFILE_PATH", ".ft_session"),
    )
    need_code = ss.login()
    if need_code:
        if sys.stdin.isatty():
            ss.login_two(input("Firstrade 驗證碼（Email/簡訊）: ").strip())
        else:
            raise RuntimeError(
                "Firstrade 要求簡訊/Email 驗證碼，自動化環境無法輸入。"
                "請在 Firstrade 啟用驗證器 App 2FA 並設定 FT_MFA_SECRET。"
            )

    acct = account.FTAccountData(ss)
    holdings: list[dict] = []
    for acc_no in acct.account_numbers:
        data = acct.get_positions(account=acc_no)
        for it in data.get("items", []) or []:
            symbol = str(it.get("symbol", "")).strip().upper()
            qty = num(it.get("quantity"))
            if not symbol or qty == 0:
                continue

            occ = parse_occ(symbol)
            is_option = occ is not None or str(it.get("sec_type")) == "2"
            mult = 100 if is_option else 1
            shares = qty * mult  # 網站以「股數」計算，選擇權 1 口 = 100 股

            mv = num(it.get("market_value"))
            cost = num(it.get("adj_cost")) or num(it.get("cost"))
            price = (mv / shares) if mv else num(it.get("last"))
            avg = (cost / shares) if cost else num(it.get("adj_unit_cost")) or num(it.get("unit_cost"))

            h = {
                "code": symbol,
                "name": symbol,
                "qty": round(shares, 6),
                "avgCost": round(avg, 6),
                "price": round(price, 6),
                "category": "bond" if symbol in US_BOND_TICKERS else "equity",
            }
            if occ:
                root, exp, cp, strike = occ
                h["name"] = f"{root} {'Call' if cp == 'C' else 'Put'}"
                h["note"] = f"{exp:%m/%d/%y} ${strike:g} x{qty:g}"
                h["category"] = "bond" if root in US_BOND_TICKERS else "equity"
            holdings.append(h)
    return holdings


# ---------------------------------------------------------------- 永豐 Shioaji (台股)
def fetch_shioaji() -> list[dict]:
    import shioaji as sj  # pip install shioaji

    api = sj.Shioaji(simulation=False)
    api.login(api_key=env("SJ_API_KEY"), secret_key=env("SJ_SEC_KEY"))
    try:
        # Unit.Share → 數量以「股」為單位（含零股），和網站上的股數一致
        Unit = getattr(sj, "Unit", None) or sj.constant.Unit
        positions = api.list_positions(account=api.stock_account, unit=Unit.Share)
        merged: dict[str, dict] = {}
        for p in positions:
            code = str(p.code).strip().upper()
            qty = num(p.quantity)
            if not code or qty == 0:
                continue
            # 同一檔可能分成現股/融資等多筆，依股數加權合併
            m = merged.setdefault(code, {"qty": 0.0, "cost": 0.0, "price": 0.0})
            m["qty"] += qty
            m["cost"] += qty * num(p.price)
            m["price"] = num(getattr(p, "last_price", 0)) or m["price"]

        holdings = []
        for code, m in merged.items():
            name = code
            try:
                c = api.contracts.get(code) if hasattr(api, "contracts") and hasattr(api.contracts, "get") else api.Contracts.Stocks[code]
                name = getattr(c, "name", None) or code
            except Exception:
                pass
            holdings.append({
                "code": code,
                "name": name,
                "qty": m["qty"],
                "avgCost": round(m["cost"] / m["qty"], 4) if m["qty"] else 0,
                "price": m["price"],
                "category": "bond" if code.endswith("B") else "equity",
            })
        return holdings
    finally:
        try:
            api.logout()
        except Exception:
            pass


# ---------------------------------------------------------------- 合併
def merge_market(existing: list[dict], fetched: list[dict], prefix: str) -> list[dict]:
    """
    以券商資料為準：
      - 同代號：更新股數/成本/現價，保留你在網站上設定的名稱、類別、id
      - 券商有、網站沒有：新增
      - 網站有、券商沒有：若是之前同步來的 → 視為已賣出，移除；
                          若是你手動加的（manual: true）→ 保留
    """
    by_code = {str(h.get("code", "")).upper(): h for h in existing if h.get("code")}
    out: list[dict] = []
    seen = set()
    for f in fetched:
        code = f["code"].upper()
        old = by_code.get(code)
        h = {
            "id": old["id"] if old else new_id(prefix),
            "name": (old or {}).get("name") or f["name"],
            "code": f["code"],
            "qty": f["qty"],
            "avgCost": f["avgCost"],
            "price": f["price"] or (old or {}).get("price", 0),
            "category": (old or {}).get("category") or f["category"],
            "source": "broker",
        }
        if f.get("note"):
            h["note"] = f["note"]
        out.append(h)
        seen.add(code)

    for h in existing:
        code = str(h.get("code", "")).upper()
        if code in seen:
            continue
        if h.get("manual"):
            out.append(h)
        elif h.get("source") != "broker" and not code:
            # 沒有代號、無法比對的舊資料（例如舊的 TLT Call）→ 被新選擇權取代就移除，否則保留
            if not any(x.get("note") for x in fetched):
                out.append(h)
    return out


def compute_total(state: dict) -> float:
    fx = num(state.get("fx"), 31.5)
    tw = sum(num(h.get("qty")) * num(h.get("price")) for h in state.get("tw", []))
    us = sum(num(h.get("qty")) * num(h.get("price")) for h in state.get("us", []))
    return tw + us * fx


def fetch_usd_twd() -> float | None:
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/TWD=X?interval=1d&range=5d",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=15,
        )
        p = r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
        return round(float(p), 3) if p and 25 < p < 40 else None
    except Exception:
        return None


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只顯示抓到的持倉，不寫入 JSONBin")
    ap.add_argument("--only", choices=["tw", "us"], help="只同步某個市場")
    ap.add_argument("--no-snapshot", action="store_true", help="不自動新增本週快照")
    args = ap.parse_args()

    now = dt.datetime.now(TZ)
    results: dict[str, dict] = {}
    fetched: dict[str, list[dict]] = {}

    jobs = []
    if args.only in (None, "us") and env("FT_USERNAME"):
        jobs.append(("us", "Firstrade", fetch_firstrade))
    if args.only in (None, "tw") and env("SJ_API_KEY"):
        jobs.append(("tw", "永豐 Shioaji", fetch_shioaji))
    if not jobs:
        log("沒有設定任何券商帳密（FT_USERNAME / SJ_API_KEY），結束。")
        return 1

    for market, label, fn in jobs:
        try:
            log(f"連線 {label} ...")
            data = fn()
            fetched[market] = data
            results[market] = {"ok": True, "count": len(data)}
            log(f"{label}：取得 {len(data)} 檔持倉")
            for h in data:
                log(f"   {h['code']:<20} {h['name']:<14} 股數 {h['qty']:>12,.2f}  成本 {h['avgCost']:>10,.4f}  現價 {h['price']:>10,.4f}  {h.get('note','')}")
        except Exception as e:  # noqa: BLE001
            results[market] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
            log(f"❌ {label} 失敗：{e}")

    if args.dry_run:
        log("--dry-run：未寫入雲端。")
        return 0 if all(r["ok"] for r in results.values()) else 2

    record = load_bin()
    state = record.get("state") or {"fx": 31.5, "tw": [], "us": []}
    history = record.get("history") or []

    for market, data in fetched.items():
        if not data and state.get(market):
            # 防呆：券商回傳 0 檔但網站上有持倉，多半是權限/帳號問題，不要把資料清空
            results[market] = {"ok": False, "error": "券商回傳 0 檔持倉，為避免清空資料已略過"}
            log(f"⚠️ {market} 回傳 0 檔，略過合併")
            continue
        state[market] = merge_market(state.get(market, []), data, market)

    fx = fetch_usd_twd()
    if fx:
        state["fx"] = fx

    prev = state.get("lastSync") or {}
    last_sync = {"time": now.isoformat(timespec="seconds")}
    for market in ("tw", "us"):
        if market in results:
            last_sync[market] = {**results[market], "time": now.isoformat(timespec="seconds")}
        elif market in prev:
            last_sync[market] = prev[market]
    state["lastSync"] = last_sync

    # 每週自動補一筆快照（本週還沒有快照才加）
    if not args.no_snapshot and any(r["ok"] for r in results.values()):
        this_week = now.date().isocalendar()[:2]
        has_this_week = any(
            dt.date.fromisoformat(s["date"]).isocalendar()[:2] == this_week
            for s in history if s.get("date")
        )
        if not has_this_week:
            history.append({"date": now.date().isoformat(), "totalValue": compute_total(state), "auto": True})
            log("已新增本週自動快照")

    save_bin({"state": state, "history": history})
    log(f"✅ 已寫入 JSONBin，總資產約 NT$ {compute_total(state):,.0f}")
    return 0 if all(r["ok"] for r in results.values()) else 2


if __name__ == "__main__":
    sys.exit(main())
