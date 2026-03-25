"""
=============================================================
  VOL TRADING BOT v3 — Volatility-based Crypto Spot Trader
  ──────────────────────────────────────────────────────────
  Melhor das duas versões anteriores:
    • Filtro de tendência EMA50 > EMA200 (v2) — sem longs em bear
    • Score composto: momentum + volatilidade (v2)
    • Risco dinâmico auto-escalável com saldo (v1)
    • Stop 2× ATR | TP 3× ATR — ratio testado em backtest (v1)
    • Trailing stop 1.5× ATR após TP (v1)
    • Confirmação de volume em entrada (v1+v2)
    • NpEncoder corrigido em todos os JSON dumps
  ──────────────────────────────────────────────────────────
  Exchange  : Binance Spot (nunca alavancado)
  Timeframe : 1h candles | scan a cada hora
  24/7      : loop contínuo com heartbeat de 60s
=============================================================
"""

import os
import json
import time
import datetime
import numpy as np
import pandas as pd
import requests
from binance.client import Client
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv

# ─────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────

# ── Indicadores ───────────────────────────
ATR_PERIOD      = 14
RSI_PERIOD      = 14
MACD_FAST       = 12
MACD_SLOW       = 26
MACD_SIGNAL     = 9
BB_PERIOD       = 20
BB_STD          = 2.0
EMA_FAST        = 50    # Filtro de tendência
EMA_SLOW        = 200   # Filtro de tendência

# ── Risk/Reward ───────────────────────────
SL_ATR_MULT     = 2.0   # Stop loss  = entrada - (2 × ATR)
TP_ATR_MULT     = 3.0   # Take profit= entrada + (3 × ATR)
TRAIL_ATR_MULT  = 1.5   # Trailing   = pico   - (1.5 × ATR)

# ── Risco dinâmico (escala com saldo) ─────
# Tabela: (saldo_min, saldo_max, % por trade)
RISK_TABLE = [
    (0,    200,   0.20),   # $0–$200   → 20%
    (200,  500,   0.10),   # $200–$500 → 10%
    (500,  2000,  0.05),   # $500–$2k  →  5%
    (2000, float("inf"), 0.05),
]
MIN_TRADE_USDT  = 11.0   # Binance min notional
MAX_TRADE_USDT  = 500.0  # Teto por trade (liquidez)

# ── Filtros de qualidade ──────────────────
MIN_VOLUME_USDT = 5_000_000   # Volume mínimo 24h para considerar o ativo
VOLUME_MULT     = 1.2         # Volume da entrada ≥ 1.2× média 20 candles
COOLDOWN_HOURS  = 4           # Espera mínima após sair de um ativo
MIN_HOLD_HOURS  = 2           # Segura posição pelo menos 2h

# ── Loop ──────────────────────────────────
SCAN_INTERVAL_SEC  = 3600   # Re-scan a cada 1h
LOOP_INTERVAL_SEC  = 60     # Heartbeat a cada 60s
KLINE_INTERVAL     = Client.KLINE_INTERVAL_1HOUR
KLINE_LIMIT        = 250    # Candles (precisa de 200+ para EMA200)

STATE_FILE = "vol_state.json"
LOG_DIR    = "logs"

# ─────────────────────────────────────────
#  ENV + CLIENTES
# ─────────────────────────────────────────
load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

client = Client(
    os.getenv("BINANCE_API_KEY"),
    os.getenv("BINANCE_SECRET_KEY")
)

# ─────────────────────────────────────────
#  JSON ENCODER (numpy safe)
# ─────────────────────────────────────────
class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.bool_):    return bool(obj)
        if isinstance(obj, np.integer):  return int(obj)
        if isinstance(obj, np.floating): return float(obj)
        return super().default(obj)

# ─────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

def log_event(data: dict):
    data["ts"] = datetime.datetime.utcnow().isoformat()
    month = datetime.datetime.utcnow().strftime("%Y-%m")
    path  = os.path.join(LOG_DIR, f"vol_bot_{month}.log")
    with open(path, "a") as f:
        f.write(json.dumps(data, cls=NpEncoder) + "\n")
    print(f"[{data['ts']}] {data}")

# ─────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────
def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception:
        pass

# ─────────────────────────────────────────
#  RISCO DINÂMICO
# ─────────────────────────────────────────
def get_trade_usdt(balance: float) -> float:
    """Retorna o valor em USDT a arriscar — escala com o saldo."""
    for min_b, max_b, pct in RISK_TABLE:
        if min_b <= balance < max_b:
            raw = balance * pct
            return min(max(raw, MIN_TRADE_USDT), MAX_TRADE_USDT)
    raw = balance * RISK_TABLE[-1][2]
    return min(max(raw, MIN_TRADE_USDT), MAX_TRADE_USDT)

def get_risk_pct(balance: float) -> float:
    trade = get_trade_usdt(balance)
    return trade / balance if balance > 0 else 0

# ─────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────
DEFAULT_STATE = {
    "symbol":          None,
    "position":        False,
    "entry_price":     0.0,
    "qty":             0.0,
    "atr_at_entry":    0.0,
    "stop_loss":       0.0,
    "take_profit":     0.0,
    "trailing_active": False,
    "peak_price":      0.0,
    "trailing_stop":   0.0,
    "last_scan_ts":    0,
    "last_exit_ts":    {},   # {symbol: iso_timestamp}
}

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            s = json.load(f)
        for k, v in DEFAULT_STATE.items():
            s.setdefault(k, v)
        return s
    return DEFAULT_STATE.copy()

def save_state(s: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2, cls=NpEncoder)

# ─────────────────────────────────────────
#  BINANCE HELPERS
# ─────────────────────────────────────────
def get_usdt_balance() -> float:
    account  = client.get_account()
    balances = {b["asset"]: float(b["free"]) for b in account["balances"]}
    return balances.get("USDT", 0.0)

def get_asset_balance(asset: str) -> float:
    account  = client.get_account()
    balances = {b["asset"]: float(b["free"]) for b in account["balances"]}
    return balances.get(asset, 0.0)

def get_symbol_filters(symbol: str):
    info         = client.get_symbol_info(symbol)
    lot          = next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")
    price_f      = next(f for f in info["filters"] if f["filterType"] == "PRICE_FILTER")
    step_size    = float(lot["stepSize"])
    min_qty      = float(lot["minQty"])
    tick_size    = float(price_f["tickSize"])
    min_notional = 0.0
    try:
        nn = next(f for f in info["filters"] if f["filterType"] in ("MIN_NOTIONAL","NOTIONAL"))
        min_notional = float(nn.get("minNotional", nn.get("minNotional", 0)))
    except StopIteration:
        pass
    return step_size, min_qty, tick_size, min_notional

def adjust_qty(qty: float, step_size: float) -> float:
    if step_size <= 0:
        return qty
    precision = max(0, int(round(-np.log10(step_size))))
    factor    = 10 ** precision
    return np.floor(qty * factor) / factor

def adjust_price(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return price
    precision = max(0, int(round(-np.log10(tick_size))))
    factor    = 10 ** precision
    return np.floor(price * factor) / factor

# ─────────────────────────────────────────
#  MARKET DATA + INDICADORES
# ─────────────────────────────────────────
def fetch_klines(symbol: str) -> pd.DataFrame:
    raw = client.get_klines(
        symbol=symbol,
        interval=KLINE_INTERVAL,
        limit=KLINE_LIMIT
    )
    df = pd.DataFrame(raw, columns=[
        "OpenTime","Open","High","Low","Close","Volume",
        "CloseTime","QuoteVolume","Trades",
        "TakerBuyBase","TakerBuyQuote","Ignore"
    ])
    for col in ["Open","High","Low","Close","Volume","QuoteVolume"]:
        df[col] = df[col].astype(float)
    return df

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # ATR
    df["PrevClose"] = df["Close"].shift(1)
    df["TR"]  = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["PrevClose"]).abs(),
        (df["Low"]  - df["PrevClose"]).abs()
    ], axis=1).max(axis=1)
    df["ATR"] = df["TR"].ewm(span=ATR_PERIOD, adjust=False).mean()

    # RSI
    delta  = df["Close"].diff()
    gain   = delta.clip(lower=0)
    loss   = (-delta).clip(lower=0)
    avg_g  = gain.ewm(com=RSI_PERIOD-1, adjust=False).mean()
    avg_l  = loss.ewm(com=RSI_PERIOD-1, adjust=False).mean()
    df["RSI"] = 100 - (100 / (1 + avg_g / avg_l.replace(0, np.nan)))

    # MACD
    ema_f        = df["Close"].ewm(span=MACD_FAST,   adjust=False).mean()
    ema_s        = df["Close"].ewm(span=MACD_SLOW,   adjust=False).mean()
    df["MACD"]   = ema_f - ema_s
    df["Signal"] = df["MACD"].ewm(span=MACD_SIGNAL, adjust=False).mean()
    df["MACD_h"] = df["MACD"] - df["Signal"]

    # Bollinger Bands
    df["BB_mid"]   = df["Close"].rolling(BB_PERIOD).mean()
    df["BB_std"]   = df["Close"].rolling(BB_PERIOD).std()
    df["BB_upper"] = df["BB_mid"] + BB_STD * df["BB_std"]
    df["BB_lower"] = df["BB_mid"] - BB_STD * df["BB_std"]

    # EMAs para filtro de tendência (v2)
    df["EMA_fast"] = df["Close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["EMA_slow"] = df["Close"].ewm(span=EMA_SLOW, adjust=False).mean()

    # ATR% para ranking de volatilidade
    df["ATR_pct"] = df["ATR"] / df["Close"]

    return df

# ─────────────────────────────────────────
#  SCANNER DE VOLATILIDADE (score composto)
# ─────────────────────────────────────────
def get_most_volatile_symbol(state: dict) -> str | None:
    """
    Score composto (v2): ATR% × momentum
    Só considera símbolos fora do cooldown e com tendência de alta.
    """
    print("\n🔍 Smart scan — buscando ativo mais volátil...")
    tickers   = client.get_ticker()
    usdt_pairs = [
        t for t in tickers
        if t["symbol"].endswith("USDT")
        and float(t.get("quoteVolume", 0)) >= MIN_VOLUME_USDT
        and float(t.get("lastPrice",   0)) > 0
    ]

    now_ts = datetime.datetime.utcnow()
    scores = []

    for t in usdt_pairs:
        symbol = t["symbol"]

        # Cooldown check
        last_exit = state.get("last_exit_ts", {}).get(symbol)
        if last_exit:
            try:
                last_exit_dt = datetime.datetime.fromisoformat(str(last_exit))
            except ValueError:
                last_exit_dt = datetime.datetime.utcfromtimestamp(float(last_exit))
            hours_since  = (now_ts - last_exit_dt).total_seconds() / 3600
            if hours_since < COOLDOWN_HOURS:
                continue

        try:
            df  = fetch_klines(symbol)
            df  = compute_indicators(df)
            row = df.iloc[-1]

            # ── Filtro de tendência (v2) ──────────────────
            # Só considera ativos em tendência de alta
            if row["EMA_fast"] <= row["EMA_slow"]:
                continue

            # ── Score composto: volatilidade × momentum ───
            atr_pct  = row["ATR_pct"]
            momentum = max(row["MACD_h"], 0)   # só positivo
            score    = atr_pct * (1 + momentum)

            scores.append((symbol, score, atr_pct, row["RSI"], row["Close"]))
        except Exception:
            continue
        time.sleep(0.1)

    if not scores:
        print("⚠️  Nenhum ativo passou os filtros (bear market ou cooldown)")
        return None

    scores.sort(key=lambda x: x[1], reverse=True)

    print(f"\nTop 5 candidatos:")
    for sym, sc, atr_pct, rsi, price in scores[:5]:
        print(f"  {sym:15s}  Score={sc:.4f}  ATR%={atr_pct*100:.2f}%  RSI={rsi:.1f}")

    best = scores[0]
    msg = (
        f"📊 Smart Scan Complete\n"
        f"Winner: {best[0]}\n"
        f"Score: {best[1]:.4f}\n"
        f"ATR%: {best[2]*100:.2f}%\n"
        f"RSI: {best[3]:.1f}\n"
        f"Price: {best[4]:.6f}"
    )
    log_event({"event": "vol_scan", "symbol": best[0], "score": best[1],
               "atr_pct": best[2], "rsi": best[3], "price": best[4]})
    send_telegram(msg)
    return best[0]

# ─────────────────────────────────────────
#  SINAIS DE ENTRADA E SAÍDA
# ─────────────────────────────────────────
def check_entry_signal(df: pd.DataFrame) -> bool:
    """
    Entrada quando TODAS as condições são satisfeitas:
      1. EMA50 > EMA200  — tendência de alta confirmada (v2)
      2. RSI entre 40–65 — momentum positivo sem sobrecompra
      3. MACD histogram > 0
      4. MACD cruzou acima do signal nos últimos 3 candles
      5. Close > BB mid  — preço acima da média
      6. Volume ≥ 1.2× média 20 candles — força real
    """
    warmup = max(EMA_SLOW, BB_PERIOD, MACD_SLOW) + 5
    if len(df) < warmup:
        return False

    row = df.iloc[-1]

    trend_ok  = row["EMA_fast"] > row["EMA_slow"]
    rsi_ok    = 40 <= row["RSI"] <= 65
    macd_pos  = row["MACD_h"] > 0
    bb_ok     = row["Close"] > row["BB_mid"]

    crossed = any(
        df.iloc[j]["MACD"] >= df.iloc[j]["Signal"] and
        df.iloc[j-1]["MACD"] < df.iloc[j-1]["Signal"]
        for j in range(-3, 0)
    )

    vol_avg = df["Volume"].iloc[-21:-1].mean()
    vol_ok  = (row["Volume"] >= VOLUME_MULT * vol_avg) if vol_avg > 0 else False

    log_event({
        "event":    "signal_check",
        "symbol":   "scanning",
        "trend_ok": bool(trend_ok),
        "rsi":      round(float(row["RSI"]), 2),
        "macd_h":   round(float(row["MACD_h"]), 6),
        "crossed":  bool(crossed),
        "bb_ok":    bool(bb_ok),
        "vol_ok":   bool(vol_ok),
        "signal":   bool(trend_ok and rsi_ok and macd_pos and crossed and bb_ok and vol_ok)
    })

    return trend_ok and rsi_ok and macd_pos and crossed and bb_ok and vol_ok

def check_exit_signal(df: pd.DataFrame) -> bool:
    """Saída antecipada: MACD negativo + RSI < 40."""
    row = df.iloc[-1]
    return row["MACD_h"] < 0 and row["RSI"] < 40

# ─────────────────────────────────────────
#  EXECUÇÃO DE TRADES
# ─────────────────────────────────────────
def open_position(symbol: str, state: dict) -> dict:
    usdt       = get_usdt_balance()
    trade_usdt = get_trade_usdt(usdt)
    risk_pct   = get_risk_pct(usdt)

    print(f"  💰 Saldo: ${usdt:.2f} | Risco: {risk_pct*100:.0f}% | Trade: ${trade_usdt:.2f}")

    df   = fetch_klines(symbol)
    df   = compute_indicators(df)
    row  = df.iloc[-1]
    atr  = float(row["ATR"])
    price= float(row["Close"])

    step_size, min_qty, tick_size, min_notional = get_symbol_filters(symbol)

    qty = adjust_qty(trade_usdt / price, step_size)
    if qty < min_qty or qty * price < max(min_notional, MIN_TRADE_USDT):
        log_event({"event": "skip_entry", "reason": "qty_too_small", "symbol": symbol})
        return state

    order      = client.create_order(
        symbol=symbol, side="BUY", type="MARKET",
        quantity=str(qty)
    )
    fill_price = float(order["fills"][0]["price"]) if order.get("fills") else price

    sl = adjust_price(fill_price - SL_ATR_MULT * atr, tick_size)
    tp = adjust_price(fill_price + TP_ATR_MULT * atr, tick_size)

    state.update({
        "symbol":          symbol,
        "position":        True,
        "entry_price":     fill_price,
        "qty":             float(qty),
        "atr_at_entry":    atr,
        "stop_loss":       sl,
        "take_profit":     tp,
        "trailing_active": False,
        "peak_price":      fill_price,
        "trailing_stop":   sl,
    })
    save_state(state)

    msg = (
        f"🟢 ENTRY {symbol}\n"
        f"Price:  {fill_price:.6f}\n"
        f"Qty:    {qty}\n"
        f"SL:     {sl:.6f}  (2× ATR)\n"
        f"TP:     {tp:.6f}  (3× ATR)\n"
        f"ATR:    {atr:.6f}\n"
        f"Risco:  ${trade_usdt:.2f} ({risk_pct*100:.0f}%)"
    )
    log_event({"event": "entry", "symbol": symbol, "price": fill_price,
               "qty": float(qty), "sl": sl, "tp": tp, "atr": atr,
               "trade_usdt": trade_usdt, "risk_pct": risk_pct})
    send_telegram(msg)
    print(msg)
    return state

def close_position(state: dict, reason: str) -> dict:
    symbol = state["symbol"]
    asset  = symbol.replace("USDT", "")
    qty    = get_asset_balance(asset)

    step_size, min_qty, tick_size, _ = get_symbol_filters(symbol)
    qty = adjust_qty(qty, step_size)

    if qty < min_qty:
        log_event({"event": "close_skip", "reason": "qty_too_small", "symbol": symbol})
        state["position"] = False
        save_state(state)
        return state

    order      = client.create_order(
        symbol=symbol, side="SELL", type="MARKET",
        quantity=str(qty)
    )
    exit_price = float(order["fills"][0]["price"]) if order.get("fills") else 0
    pnl_pct    = (exit_price - state["entry_price"]) / state["entry_price"] * 100
    pnl_usdt   = (exit_price - state["entry_price"]) * state["qty"]

    msg = (
        f"🔴 EXIT {symbol}\n"
        f"Motivo: {reason}\n"
        f"Entry:  {state['entry_price']:.6f}\n"
        f"Exit:   {exit_price:.6f}\n"
        f"PnL:    {pnl_pct:+.2f}%  (US${pnl_usdt:+.2f})"
    )
    log_event({"event": "exit", "symbol": symbol, "reason": reason,
               "entry": state["entry_price"], "exit": exit_price,
               "pnl_pct": pnl_pct, "pnl_usdt": pnl_usdt})
    send_telegram(msg)
    print(msg)

    # Registra cooldown
    exit_ts = state.get("last_exit_ts", {})
    exit_ts[symbol] = datetime.datetime.utcnow().isoformat()

    state.update({
        "symbol":          None,
        "position":        False,
        "entry_price":     0.0,
        "qty":             0.0,
        "atr_at_entry":    0.0,
        "stop_loss":       0.0,
        "take_profit":     0.0,
        "trailing_active": False,
        "peak_price":      0.0,
        "trailing_stop":   0.0,
        "last_exit_ts":    exit_ts,
    })
    save_state(state)
    return state

# ─────────────────────────────────────────
#  MONITOR DE POSIÇÃO
# ─────────────────────────────────────────
def monitor_position(state: dict) -> dict:
    symbol = state["symbol"]
    ticker = client.get_symbol_ticker(symbol=symbol)
    price  = float(ticker["price"])

    df  = fetch_klines(symbol)
    df  = compute_indicators(df)
    atr = float(df.iloc[-1]["ATR"])

    # Atualiza peak
    if price > state["peak_price"]:
        state["peak_price"] = price

    # Ativa trailing ao atingir TP
    if not state["trailing_active"] and price >= state["take_profit"]:
        state["trailing_active"] = True
        log_event({"event": "trailing_activated", "symbol": symbol, "price": price})
        send_telegram(f"🎯 TP atingido {symbol} @ {price:.6f}\nTrailing stop ativado!")

    # Atualiza trailing
    if state["trailing_active"]:
        new_trail = state["peak_price"] - TRAIL_ATR_MULT * atr
        if new_trail > state["trailing_stop"]:
            state["trailing_stop"] = new_trail

    eff_stop = state["trailing_stop"] if state["trailing_active"] else state["stop_loss"]

    # Horas em posição (para MIN_HOLD_HOURS)
    hours_held = 0
    log_ts = None
    try:
        month = datetime.datetime.utcnow().strftime("%Y-%m")
        path  = os.path.join(LOG_DIR, f"vol_bot_{month}.log")
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                        if ev.get("event") == "entry" and ev.get("symbol") == symbol:
                            log_ts = datetime.datetime.fromisoformat(ev["ts"])
                    except Exception:
                        pass
        if log_ts:
            hours_held = (datetime.datetime.utcnow() - log_ts).total_seconds() / 3600
    except Exception:
        pass

    reason = None
    if price <= eff_stop:
        reason = "trailing_stop" if state["trailing_active"] else "stop_loss"
    elif hours_held >= MIN_HOLD_HOURS and check_exit_signal(df):
        reason = "indicator_exit"

    if reason:
        state = close_position(state, reason)
        return state

    save_state(state)
    pnl_pct = (price - state["entry_price"]) / state["entry_price"] * 100
    log_event({
        "event":        "position_update",
        "symbol":       symbol,
        "price":        price,
        "peak":         state["peak_price"],
        "stop":         eff_stop,
        "pnl_pct":      round(pnl_pct, 2),
        "trail_active": state["trailing_active"]
    })
    return state

# ─────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────
def main():
    send_telegram("🤖 Vol Bot v3 STARTED — scanning markets 24/7")
    log_event({"event": "bot_start", "version": "v3"})
    print("\n" + "="*55)
    print("  VOL TRADING BOT v3  |  Spot  |  24/7")
    print("="*55)

    state = load_state()

    while True:
        try:
            now_ts = time.time()

            # ── Scan horário ─────────────────────────────
            if now_ts - state["last_scan_ts"] >= SCAN_INTERVAL_SEC:
                if not state["position"]:
                    new_symbol = get_most_volatile_symbol(state)
                    if new_symbol:
                        state["symbol"] = new_symbol
                state["last_scan_ts"] = now_ts
                save_state(state)

            # ── Gerencia posição aberta ───────────────────
            if state["position"]:
                state = monitor_position(state)

            # ── Busca entrada ─────────────────────────────
            elif state["symbol"]:
                df   = fetch_klines(state["symbol"])
                df   = compute_indicators(df)
                row  = df.iloc[-1]
                usdt = get_usdt_balance()
                risk = get_risk_pct(usdt)

                print(
                    f"\n[{datetime.datetime.utcnow().strftime('%H:%M:%S')}] "
                    f"{state['symbol']} | "
                    f"USDT: ${usdt:.2f} | Risk: {risk*100:.0f}% | "
                    f"EMA_ok: {row['EMA_fast']>row['EMA_slow']} | "
                    f"RSI: {row['RSI']:.1f} | "
                    f"MACD_h: {row['MACD_h']:.6f}"
                )

                if check_entry_signal(df):
                    state = open_position(state["symbol"], state)

            else:
                print(f"[{datetime.datetime.utcnow().strftime('%H:%M:%S')}] "
                      f"Aguardando próximo scan...")

        except BinanceAPIException as e:
            msg = f"⚠️ Binance API Error:\n{e}"
            log_event({"event": "error_binance", "msg": str(e)})
            send_telegram(msg)
            print(msg)
            time.sleep(30)

        except Exception as e:
            msg = f"⚠️ General Error:\n{e}"
            log_event({"event": "error_general", "msg": str(e)})
            send_telegram(msg)
            print(msg)
            time.sleep(30)

        time.sleep(LOOP_INTERVAL_SEC)


if __name__ == "__main__":
    main()
