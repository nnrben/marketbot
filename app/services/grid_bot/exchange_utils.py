from typing import Optional


_REAL_EXCHANGE_TO_SCHEDULE = {
    "REAL_EXCHANGE_MOEX": "MOEX",
    "REAL_EXCHANGE_RTS": "SPB",
    "REAL_EXCHANGE_OTC": "",
    "REAL_EXCHANGE_DEALER": "",
}


def _resolve_schedule_exchange(instr: dict) -> Optional[str]:
    real = (instr.get("realExchange") or "").upper()
    if real in _REAL_EXCHANGE_TO_SCHEDULE:
        mapped = _REAL_EXCHANGE_TO_SCHEDULE[real]
        return mapped or None

    raw = (instr.get("exchange") or "").lower()
    if raw.startswith("moex"):
        return "MOEX"
    if raw.startswith("spb"):
        return "SPB"
    if raw.startswith("fx"):
        return "FX"
    return None
