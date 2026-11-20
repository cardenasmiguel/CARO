"""Cobro (redeem) de posiciones resueltas en el CTF de Polymarket.

Solo se envía una transacción on-chain para carteras EOA (tipo de firma 0), contra el
contrato oficial Conditional Tokens de Polymarket. En DRY_RUN nunca se firma ni se envía
nada: solo se registra lo que se haría.

Safe (firma 2) y Magic/email (firma 1) no se implementan: los tokens viven en el proxy/Safe
y cobrarlos con seguridad exigiría el relayer de Polymarket. En esos casos el panel avisa
de cobrar a mano en polymarket.com.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from . import account

# redeemPositions(address,bytes32,bytes32,uint256[])
REDEEM_SELECTOR = "01b7037c"
# balanceOf(address,uint256) — ERC-1155
BALANCE_OF_SELECTOR = "00fdd58e"
ZERO_BYTES32 = "0x" + "00" * 32
PENDING_CLAIM_MSG = "posición resuelta pendiente de cobrar en polymarket.com"
PENDING_CLAIM_MSG_EN = "resolved position waiting to be claimed on polymarket.com"

# En pruebas se sustituye para no hablar con la cadena.
_rpc_sleep: Callable[[float], None] = __import__("time").sleep


def redeem_supported(signature_type: int) -> bool:
    """True solo si se puede cobrar on-chain con seguridad (EOA, firma 0)."""
    try:
        return int(signature_type) == 0
    except (TypeError, ValueError):
        return False


def contract_addresses(chain_id: int = 137) -> Dict[str, str]:
    t = account.approval_targets(chain_id)
    return {"collateral": t["collateral"], "ctf": t["ctf"]}


def _word(n: int) -> bytes:
    if n < 0:
        raise ValueError("valor negativo")
    return n.to_bytes(32, "big")


def _addr(a: str) -> bytes:
    h = (a or "").lower().replace("0x", "")
    if len(h) != 40 or any(c not in "0123456789abcdef" for c in h):
        raise ValueError("dirección no válida")
    return _word(int(h, 16))


def _bytes32(h: str) -> bytes:
    s = (h or "").lower().replace("0x", "")
    if len(s) != 64 or any(c not in "0123456789abcdef" for c in s):
        raise ValueError("condition_id debe ser 32 bytes hex")
    return bytes.fromhex(s)


def encode_redeem_positions(collateral: str, condition_id: str, index_set: int,
                            parent_collection_id: str = ZERO_BYTES32) -> str:
    """Calldata de redeemPositions (mercado binario, un index set)."""
    data = (
        bytes.fromhex(REDEEM_SELECTOR)
        + _addr(collateral)
        + _bytes32(parent_collection_id)
        + _bytes32(condition_id)
        + _word(128)          # offset del array dinámico (4 palabras de cabeza)
        + _word(1)            # longitud
        + _word(int(index_set))
    )
    return "0x" + data.hex()


def encode_balance_of(holder: str, token_id: str) -> str:
    tid = token_id if str(token_id).startswith("0x") else hex(int(token_id))
    data = bytes.fromhex(BALANCE_OF_SELECTOR) + _addr(holder) + _word(int(tid, 16))
    return "0x" + data.hex()


def _rpc_any(rpcs: List[str], method: str, params: list, timeout: float = 15.0) -> Any:
    last: Exception = RuntimeError("sin RPC")
    for u in rpcs:
        try:
            return account._rpc(u, method, params, timeout)
        except Exception as e:
            last = e
    raise last


def parse_outcome_prices(raw: Any) -> Optional[List[float]]:
    if raw is None:
        return None
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return None
    try:
        return [float(x) for x in raw]
    except (TypeError, ValueError):
        return None


def market_is_closed(m: Dict[str, Any]) -> bool:
    if m.get("closed") is True or m.get("resolved") is True:
        return True
    uma = str(m.get("umaResolutionStatus") or m.get("uma_resolution_status") or "").lower()
    return uma in ("resolved", "resolvedpayouts", "settled")


def side_index(side: str, outcomes: Optional[List[Any]] = None) -> int:
    """Índice 0-based del resultado UP/DOWN en la lista de outcomes (por defecto Up=0, Down=1)."""
    side_u = str(side or "").upper()
    if outcomes:
        for i, o in enumerate(outcomes):
            if str(o).strip().upper() == side_u:
                return i
    return 0 if side_u == "UP" else 1


def index_set_for_side(side: str, outcomes: Optional[List[Any]] = None) -> int:
    """indexSets del CTF: 2**i para el outcome i (binario: UP→1, DOWN→2 si Up es el primero)."""
    return 1 << side_index(side, outcomes)


def resolution_for_side(m: Dict[str, Any], side: str) -> str:
    """'open' | 'won' | 'lost' | 'unknown'. No inventa un resultado si faltan precios."""
    if not market_is_closed(m):
        return "open"
    prices = parse_outcome_prices(m.get("outcomePrices") or m.get("outcome_prices"))
    if not prices:
        return "unknown"
    outcomes = m.get("outcomes")
    if isinstance(outcomes, str):
        import json
        try:
            outcomes = json.loads(outcomes)
        except (TypeError, ValueError):
            outcomes = None
    i = side_index(side, outcomes if isinstance(outcomes, list) else None)
    if i >= len(prices):
        return "unknown"
    p = prices[i]
    if p >= 0.99:
        return "won"
    if p <= 0.01:
        return "lost"
    return "unknown"


def ctf_balance(holder: str, token_id: str, rpcs: List[str], chain_id: int = 137) -> Optional[int]:
    """Saldo ERC-1155 del token de la posición (unidades 1e6 típicas). None si el RPC falla."""
    try:
        ctf = contract_addresses(chain_id)["ctf"]
        raw = _rpc_any(rpcs, "eth_call", [{"to": ctf, "data": encode_balance_of(holder, token_id)}, "latest"])
        if not raw or raw in ("0x", "0x0"):
            return 0
        return int(raw, 16)
    except Exception:
        return None


def redeem_eoa(private_key: str, condition_id: str, index_set: int, rpcs: List[str],
               chain_id: int = 137, *, dry: bool = False, wait: bool = True,
               collateral: Optional[str] = None) -> Dict[str, Any]:
    """Llama a redeemPositions con la clave EOA. En dry=True no firma ni envía.

    Devuelve {ok, dry_run, tx, status, error}. Nunca registra la clave.
    """
    if dry:
        return {"ok": True, "dry_run": True, "tx": None, "status": "simulado", "error": None}
    from eth_account import Account
    from . import wizard

    pk = wizard.validate_private_key(private_key)
    addrs = contract_addresses(chain_id)
    coll = collateral or addrs["collateral"]
    data = encode_redeem_positions(coll, condition_id, index_set)
    acct = Account.from_key(pk)
    try:
        gas_price = int(int(_rpc_any(rpcs, "eth_gasPrice", []), 16) * 1.25)
        balance = int(_rpc_any(rpcs, "eth_getBalance", [acct.address, "latest"]), 16)
        nonce = int(_rpc_any(rpcs, "eth_getTransactionCount", [acct.address, "pending"]), 16)
        try:
            gas = int(int(_rpc_any(rpcs, "eth_estimateGas",
                                   [{"from": acct.address, "to": addrs["ctf"], "data": data}]), 16) * 1.3)
        except Exception:
            gas = 300_000
        need = gas * gas_price
        if balance < need:
            return {"ok": False, "dry_run": False, "tx": None, "status": "error",
                    "error": f"POL insuficiente para el gas: tienes {balance / 1e18:.4f} y hacen falta ≈ {need / 1e18:.4f} POL"}
        tx = {"chainId": chain_id, "to": addrs["ctf"], "value": 0, "data": data,
              "gas": gas, "gasPrice": gas_price, "nonce": nonce}
        raw = acct.sign_transaction(tx)
        raw_bytes = getattr(raw, "raw_transaction", None) or getattr(raw, "rawTransaction")
        txh = _rpc_any(rpcs, "eth_sendRawTransaction", ["0x" + bytes(raw_bytes).hex()])
        status = "enviada"
        if wait:
            status = "pendiente"
            for _ in range(45):
                rc = _rpc_any(rpcs, "eth_getTransactionReceipt", [txh])
                if rc:
                    status = "confirmada" if int(rc.get("status", "0x0"), 16) == 1 else "FALLIDA"
                    break
                _rpc_sleep(2)
        ok = status in ("enviada", "confirmada", "pendiente")
        return {"ok": ok and status != "FALLIDA", "dry_run": False, "tx": txh, "status": status, "error": None if status != "FALLIDA" else "transacción revertida"}
    except Exception as e:
        return {"ok": False, "dry_run": False, "tx": None, "status": "error", "error": f"{type(e).__name__}: {str(e)[:160]}"}
