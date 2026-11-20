"""Comprobaciones de SOLO LECTURA (sin claves): saldo y allowance de USDC del Safe/funder, por RPC público."""

from __future__ import annotations

import time
from typing import Any, Dict, List

import requests

BALANCE_OF = "0x70a08231"  # balanceOf(address)
ALLOWANCE = "0xdd62ed3e"  # allowance(address,address)


def _pad(addr: str) -> str:
    return addr.lower().replace("0x", "").rjust(64, "0")


def is_address(addr: str) -> bool:
    a = (addr or "").strip()
    return len(a) == 42 and a.startswith("0x") and all(c in "0123456789abcdefABCDEF" for c in a[2:])


def _eth_call(rpcs: List[str], to: str, data: str, timeout: float = 6.0) -> int:
    last: Exception = RuntimeError("sin RPC")
    for rpc in rpcs:
        try:
            r = requests.post(
                rpc, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": to, "data": data}, "latest"]},
                timeout=timeout,
            )
            r.raise_for_status()
            j = r.json()
            res = j.get("result")
            if isinstance(res, str) and res.startswith("0x"):
                return int(res, 16) if len(res) > 2 else 0
            last = RuntimeError(str(j.get("error"))[:80])
        except Exception as e:  # prueba el siguiente RPC
            last = e
    raise last


def check_wallet(address: str, rpcs: List[str], chain_id: int = 137, bet_size: float = 1.0) -> Dict[str, Any]:
    """Saldo (USDC, colateral de Polymarket) y allowance al exchange de ``address``. Solo lectura; no necesita clave.

    Devuelve {configured, balance_usdc, allowance_ok, allowance_usdc, error, checked_ts}. ``allowance_ok`` es
    None si no se pudo determinar, True si algún exchange de Polymarket tiene aprobado ≥ ``bet_size``.
    """
    out: Dict[str, Any] = {"configured": bool(address), "balance_usdc": None, "allowance_ok": None, "allowance_usdc": None,
                           "error": None, "checked_ts": time.time()}
    if not is_address(address):
        out["error"] = "dirección no válida"
        return out
    try:
        from py_clob_client_v2.config import COLLATERAL_TOKEN_DECIMALS, get_contract_config

        cc = get_contract_config(chain_id)
        scale = 10 ** COLLATERAL_TOKEN_DECIMALS
        out["balance_usdc"] = _eth_call(rpcs, cc.collateral, BALANCE_OF + _pad(address)) / scale
        best = 0.0
        for spender in (cc.exchange_v2, cc.exchange, cc.neg_risk_exchange_v2):
            if not spender:
                continue
            try:
                best = max(best, _eth_call(rpcs, cc.collateral, ALLOWANCE + _pad(address) + _pad(spender)) / scale)
            except Exception:
                continue
        out["allowance_usdc"] = best
        out["allowance_ok"] = best >= float(bet_size)
    except Exception as e:
        out["error"] = str(e)[:100]
    return out
