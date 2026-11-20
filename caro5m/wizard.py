"""Lógica (sin interfaz) del asistente de primer arranque: valida y escribe el fichero de secretos con permisos 0600."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional

from . import config

SECRET_ENV_KEYS = (
    "POLY_PRIVATE_KEY", "POLY_SAFE_ADDRESS", "POLYMARKET_PROXY_LIST",
    "POLY_CLOB_API_KEY", "POLY_CLOB_API_SECRET", "POLY_CLOB_API_PASSPHRASE", "POLY_CLOB_NONCE",
)


def validate_private_key(pk: str) -> str:
    pk = (pk or "").strip()
    h = pk[2:] if pk.lower().startswith("0x") else pk
    if not re.fullmatch(r"[0-9a-fA-F]{64}", h):
        raise ValueError("La clave privada debe ser hexadecimal de 64 caracteres (con o sin 0x)")
    return "0x" + h


def validate_address(addr: str) -> str:
    from .checks import is_address

    addr = (addr or "").strip()
    if not is_address(addr):
        raise ValueError("La dirección debe tener formato 0x + 40 caracteres hexadecimales")
    return addr


def validate_proxy(proxy: str) -> str:
    """Vacío = sin proxy. Admite http(s):// y socks5(h):// (varios separados por coma)."""
    proxy = (proxy or "").strip()
    if not proxy:
        return ""
    from .account import parse_proxy_list

    try:
        return ",".join(parse_proxy_list(proxy))      # se guarda normalizado: esquema://usuario:clave@host:puerto
    except ValueError as e:
        raise ValueError(f"Proxy no válido. {e}")


def mask_address(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) >= 12 else "…"


def signer_address(private_key: str) -> Optional[str]:
    """Dirección EOA de la clave (solo para que el usuario compruebe que es la suya). None si falla."""
    try:
        from eth_account import Account

        return Account.from_key(private_key).address
    except Exception:
        return None


def write_env(updates: Dict[str, str], path: Optional[Path] = None) -> Path:
    """Fusiona ``updates`` en el .env (solo claves SECRET_ENV_KEYS con valor), conserva el resto y fija chmod 600."""
    p = path or config.env_file()
    clean = {k: v for k, v in updates.items() if k in SECRET_ENV_KEYS and v}
    try:
        old = p.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        old = ["# caro5m — SECRETOS. No subir a git. Permisos 600."]
    seen = set()
    out = []
    for raw in old:
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k = line.split("=", 1)[0].strip()
            if k in clean:
                out.append(f"{k}={clean[k]}")
                seen.add(k)
                continue
        out.append(raw)
    for k, v in clean.items():
        if k not in seen:
            out.append(f"{k}={v}")
    config.atomic_write_text(p, "\n".join(out) + "\n", 0o600)
    return p


def remove_env(keys, path: Optional[Path] = None) -> Path:
    """Borra del .env las claves indicadas (solo de SECRET_ENV_KEYS), conserva el resto y fija chmod 600."""
    p = path or config.env_file()
    drop = {k for k in keys if k in SECRET_ENV_KEYS}
    try:
        old = p.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return p
    out = [ln for ln in old if not (ln.strip() and not ln.strip().startswith("#") and "=" in ln and ln.split("=", 1)[0].strip() in drop)]
    config.atomic_write_text(p, "\n".join(out) + "\n", 0o600)
    return p
