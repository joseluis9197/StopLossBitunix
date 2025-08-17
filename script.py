# -*- coding: utf-8 -*-
import time
import json
import hashlib
import random
import string
import re
from decimal import Decimal, ROUND_FLOOR
from typing import Dict, Any, Optional, List, Tuple

try:
    import httpx  # type: ignore
    _USE_HTTPX = True
except Exception:
    import requests as httpx
    from requests import Session
    _USE_HTTPX = False

import config

DEBUG = True
POLL_SECONDS = 3

class BitunixClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str, timeout: float = 15.0):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        if _USE_HTTPX:
            self.http = httpx.Client(timeout=timeout)
            self._is_httpx = True
        else:
            self.http = Session()
            self.http.trust_env = False
            self._is_httpx = False
        self._timeout = timeout

    @staticmethod
    def _nonce(n: int = 32) -> str:
        return ''.join(random.choices(string.ascii_letters + string.digits, k=n))

    def _sign(self, method: str, path: str,
              params: Optional[Dict[str, Any]] = None,
              body: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        params = params or {}
        ts = str(int(time.time() * 1000))
        nonce = self._nonce()
        qp = "".join(f"{k}={params[k]}" for k in sorted(params)) if params else ""
        body_str = "" if method.upper() == "GET" or body is None else json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256((nonce + ts + self.api_key + qp + body_str).encode()).hexdigest()
        sign = hashlib.sha256((digest + self.api_secret).encode()).hexdigest()
        return {
            "api-key": self.api_key,
            "nonce": nonce,
            "timestamp": ts,
            "sign": sign,
            "language": "en-US",
            "Content-Type": "application/json",
        }

    def _req(self, method: str, path: str,
             params: Optional[Dict[str, Any]] = None,
             body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self.base_url + path
        headers = self._sign(method, path, params, body)
        if method.upper() == "GET":
            r = self.http.get(url, params=params or {}, headers=headers, timeout=self._timeout)
        elif method.upper() == "POST":
            payload = body or {}
            raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            if self._is_httpx:
                r = self.http.post(url, params=params or {}, headers=headers, content=raw, timeout=self._timeout)
            else:
                r = self.http.post(url, params=params or {}, headers=headers, data=raw, timeout=self._timeout)
        else:
            raise ValueError("HTTP method no soportado")
        try:
            r.raise_for_status()
        except Exception as e:
            try:
                txt = r.text
            except Exception:
                txt = ""
            raise RuntimeError(f"HTTP error {getattr(r,'status_code', '?')}: {txt}") from e
        try:
            data = r.json()
        except Exception:
            data = {"code": getattr(r, "status_code", None), "raw": getattr(r, "text", "")}
        if DEBUG:
            print(f"[DEBUG] {method} {path} params={params} body={body} -> {str(data)[:500]}")
        return data

    def get_trading_pair(self, symbol: str) -> Dict[str, Any]:
        data = self._req("GET", "/api/v1/futures/market/trading_pairs", params={"symbols": symbol})
        items = data.get("data") or data.get("result") or data.get("list") or []
        if isinstance(items, list) and items:
            return items[0]
        if isinstance(items, dict):
            return items
        raise RuntimeError(f"No se encontró el símbolo: {symbol}")

    def get_all_pending_positions(self) -> List[Dict[str, Any]]:
        data = self._req("GET", "/api/v1/futures/position/get_pending_positions")
        items = data.get("data") or data.get("result") or data.get("list") or []
        return items if isinstance(items, list) else []

    def cancel_all_orders(self, symbol: str) -> Dict[str, Any]:
        return self._req("POST", "/api/v1/futures/trade/cancel_all_orders", body={"symbol": symbol})

    def cancel_all_tpsl(self, symbol: str) -> Optional[Dict[str, Any]]:
        try:
            return self._req("POST", "/api/v1/futures/tpsl/cancel_all", body={"symbol": symbol})
        except Exception:
            return None

    def place_tpsl(self, *, symbol: str, position_id: str, sl_price: float, sl_qty: float,
                   stop_type: str = "LAST_PRICE", sl_order_type: str = "MARKET") -> Dict[str, Any]:
        body = {
            "symbol": symbol,
            "positionId": str(position_id),
            "slPrice": str(sl_price),
            "slStopType": stop_type,
            "slOrderType": sl_order_type,
            "slQty": str(sl_qty),
        }
        return self._req("POST", "/api/v1/futures/tpsl/place_order", body=body)

def normalize_symbol(s: str) -> str:
    s = s.upper()
    s = re.sub(r'[-_]', '', s)
    s = s.replace('PERP', '')
    return s

def symbol_variants(base: str) -> List[str]:
    base = base.upper()
    if base.endswith('USDT'):
        root = base[:-4]
    else:
        root = base
        base = base + 'USDT'
    return [base, f"{root}_USDT", f"{root}-USDT", f"{base}-PERP"]

def safe_float(*vals) -> float:
    for v in vals:
        if v is None:
            continue
        try:
            return float(v)
        except Exception:
            continue
    return 0.0

def extract_position_fields(p: Dict[str, Any]) -> Tuple[str, float, float, float, str]:
    side = (p.get("side") or p.get("positionSide") or p.get("posSide") or "").upper()
    qty = safe_float(p.get("qty"), p.get("positionSize"), p.get("size"), p.get("volume"), p.get("availableQty"))
    entry = safe_float(p.get("avgOpenPrice"), p.get("entryPrice"), p.get("avgPrice"))
    notional = safe_float(p.get("positionValue"), abs(qty) * entry if qty and entry else None)
    position_id = str(p.get("positionId") or p.get("id") or "")
    return side, qty, entry, notional, position_id

def derive_tick_from_symbol_info(info: Dict[str, Any]) -> float:
    tick = info.get("tickSize") or (info.get("priceFilter") or {}).get("tickSize")
    if tick:
        try:
            return float(tick)
        except Exception:
            pass
    scale = (info.get("quotePrecision") or info.get("pricePrecision") or info.get("priceScale") or info.get("quoteScale"))
    if scale is not None:
        try:
            return 1 / (10 ** int(scale))
        except Exception:
            pass
    return 0.01

def quantize_price(price: float, tick: float) -> float:
    d_price = Decimal(str(price))
    d_tick = Decimal(str(tick))
    steps = (d_price / d_tick).quantize(Decimal("1"), rounding=ROUND_FLOOR)
    return float(steps * d_tick)

def find_position_fuzzy(client: BitunixClient, user_symbol: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    all_pos = client.get_all_pending_positions()
    target_norms = set(normalize_symbol(v) for v in symbol_variants(user_symbol))
    for p in all_pos:
        api_sym = (p.get("symbol") or p.get("tradingPair") or "").upper()
        if not api_sym:
            continue
        if normalize_symbol(api_sym) in target_norms:
            return p, api_sym
    if DEBUG:
        inv = [(p.get("symbol") or p.get("tradingPair"), p.get("side"), p.get("qty") or p.get("positionSize")) for p in all_pos]
        print(f"[DEBUG] Inventario de posiciones: {inv}")
    return None, None

def prompt_inputs():
    tick = input("INGRESA EL SÍMBOLO (ej: BTC): ").strip().upper()
    if not tick:
        print("⛔ Dato inválido."); return None, None
    if not tick.endswith("USDT"):
        tick = tick + "USDT"
    try:
        max_loss_usdt = float(input("INGRESA EL USDT MÁXIMO A PERDER: ").strip())
    except Exception:
        print("⛔ Dato inválido."); return None, None
    return tick, max_loss_usdt

def main():
    client = BitunixClient(config.api_key, config.api_secret, getattr(config, "base_url", "https://fapi.bitunix.com"))
    estado = False
    notional_ref = 0.0
    symbol = ""
    max_loss_usdt = 0.0
    print("=== Bitunix SL Manager — robusto (sin params en get_pending_positions) ===")
    while True:
        symbol, max_loss_usdt = prompt_inputs()
        if symbol and max_loss_usdt is not None:
            break
        time.sleep(1)
    try:
        while True:
            if estado:
                pos, api_symbol = find_position_fuzzy(client, symbol)
                side, qty, entry, notional, position_id = ("", 0.0, 0.0, 0.0, "")
                if pos:
                    side, qty, entry, notional, position_id = extract_position_fields(pos)
                if (not pos) or qty == 0 or entry <= 0 or notional <= 0 or not position_id:
                    print(f"[{symbol}] Posición cerrada o no válida. Cancelando TP/SL y órdenes…")
                    client.cancel_all_tpsl(api_symbol or symbol)
                    client.cancel_all_orders(api_symbol or symbol)
                    estado = False; notional_ref = 0.0; symbol = ""; max_loss_usdt = 0.0
                    while True:
                        symbol, max_loss_usdt = prompt_inputs()
                        if symbol and max_loss_usdt is not None:
                            break
                        time.sleep(1)
                    continue
                pct = (max_loss_usdt * 100.0) / notional
                delta = entry * (pct / 100.0)
                stop_price = entry - delta if side in ("LONG", "BUY") else entry + delta
                if stop_price <= 0:
                    print("❗ Stop calculado <= 0. Revisa el USDT máximo a perder.")
                    time.sleep(2); continue
                info = client.get_trading_pair(api_symbol or symbol)
                tick = derive_tick_from_symbol_info(info)
                stop_q = quantize_price(stop_price, tick)
                if abs(notional - notional_ref) > 1e-9:
                    print(f"[{api_symbol or symbol}] SL → {stop_q}  (side {side}, notional {notional:.2f} USDT)")
                    client.place_tpsl(
                        symbol=api_symbol or symbol,
                        position_id=position_id,
                        sl_price=stop_q,
                        sl_qty=abs(qty),
                        stop_type="LAST_PRICE",
                        sl_order_type="MARKET",
                    )
                    notional_ref = notional
            else:
                pos, api_symbol = find_position_fuzzy(client, symbol)
                if pos:
                    side, qty, entry, notional, position_id = extract_position_fields(pos)
                    if qty != 0:
                        print(f"✅ Posición detectada en {api_symbol}. Iniciando gestión SL…")
                        estado = True; notional_ref = 0.0
                        continue
                print(f"ℹ️ No hay posición abierta en {symbol}. Vigilando… (Ctrl+C para salir)")
                time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nSaliendo por teclado…")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()