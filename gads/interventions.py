"""GrowLead patch: write ticket gate (GL-ADS governance, ADR-052).

Každý skutečný zápis (`--confirm`) musí mít podpis: ticket z gl-ads
(`interventions/claim`) a důvod (`--why`). CLI před zápisem ověří ticket u gl-ads
(stav `claimed`, správný účet) a po zápisu nahlásí `mark-executed` s resource names,
které mutace vrátila. Bez ticketu se nic nevolá.

Režimy (env `GL_ADS_TICKET_GATE`, výchozí `strict`):
  strict  ticket povinný, preflight GET + postflight POST na gl-ads.
  lite    ticket nepovinný, řádek do deníčku `GL_ADS_JOURNAL_FILE` (přechodný režim,
          než je gl-ads GOV-1 nasazený; formát = ADS-GOVERNANCE.md §6.2).
  off     jen dev/test; hlasitě varuje na stderr.

Exit kódy: 2 = gate odmítl zápis (chybí ticket/důvod/env, ticket nesedí),
3 = gl-ads nedostupné (síť, timeout, 5xx), zápis odmítnut. Dry-run (bez
`--confirm`) tento modul vůbec nevolá.
"""
from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from gads.api import _clean_id, _env
from gads.formatting import _die, _err

GATE_MODES = ("strict", "lite", "off")
HTTP_TIMEOUT = 10
MIN_WHY_LEN = 10
MAX_WHY_LEN = 1000
DEFAULT_AGENT = "cli/google-ads-app"
PRAGUE = ZoneInfo("Europe/Prague")

# Resource names posbírané z odpovědí mutací během jednoho běhu CLI.
_collected: list[str] = []


@dataclass
class TicketContext:
    mode: str
    command: str
    customer_id: str
    why: str
    ticket: str | None = None
    url: str | None = None
    api_key: str | None = None
    agent: str = DEFAULT_AGENT
    started_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def gate_mode(account: str | None = None) -> str:
    mode = (_env("GL_ADS_TICKET_GATE", account) or "strict").strip().lower()
    if mode not in GATE_MODES:
        _die(f"GL_ADS_TICKET_GATE={mode!r} není platný režim (strict|lite|off).", 2)
    return mode


def reset_collected() -> None:
    _collected.clear()


def collected() -> list[str]:
    return list(_collected)


def record_result(response) -> None:
    """Posbírej resource names z odpovědi mutace. Best effort, nikdy nevyhodí."""
    try:
        for r in getattr(response, "results", None) or []:
            rn = getattr(r, "resource_name", None)
            if rn:
                _collected.append(rn)
        for r in getattr(response, "mutate_operation_responses", None) or []:
            pb = getattr(r, "_pb", None)
            if pb is None:
                continue
            for _descriptor, value in pb.ListFields():
                rn = getattr(value, "resource_name", None)
                if rn:
                    _collected.append(rn)
    except Exception:  # noqa: BLE001 — sběr nesmí shodit zápis, který už proběhl
        pass


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def _journal_line(ctx: TicketContext) -> str:
    stamp = dt.datetime.now(PRAGUE).strftime("%Y-%m-%d %H:%M")
    agent = ctx.agent if ctx.agent != DEFAULT_AGENT else "cli"
    return (f"- {stamp} · {agent} (cli/google-ads-app) · google {ctx.customer_id} · "
            f"{ctx.command} · --confirm. Důvod: {ctx.why}.\n")


def _intervention_payload(data: dict) -> dict:
    """gl-ads vrací `{intervention: {...}, adAccount: {...}}` (GOV-1 REST), nebo přímo
    řádek. Sloučí obě podoby: stav z řádku, účet z obálky nebo z řádku."""
    row = data.get("intervention")
    if not isinstance(row, dict):
        return data
    merged = dict(row)
    if isinstance(data.get("adAccount"), dict):
        merged["adAccount"] = data["adAccount"]
    return merged


# ---------------------------------------------------------------------------
# Preflight — před zápisem
# ---------------------------------------------------------------------------
def preflight(args) -> TicketContext | None:
    """Vrátí kontext ticketu, nebo None pro dry-run. Při odmítnutí ukončí proces."""
    if not getattr(args, "confirm", False):
        return None

    account = getattr(args, "account", None)
    mode = gate_mode(account)
    why = (getattr(args, "why", None) or "").strip()
    if len(why) < MIN_WHY_LEN:
        _die(f"--confirm vyžaduje --why (min. {MIN_WHY_LEN} znaků): důvod zásahu se zapisuje "
             "do audit logu gl-ads. Nic nebylo zapsáno.", 2)
    if len(why) > MAX_WHY_LEN:
        _die(f"--why je delší než {MAX_WHY_LEN} znaků. Nic nebylo zapsáno.", 2)

    ctx = TicketContext(
        mode=mode,
        command=getattr(args, "command", None) or "?",
        customer_id=_clean_id(getattr(args, "customer_id", None) or ""),
        why=why,
        ticket=(getattr(args, "ticket", None) or "").strip() or None,
        agent=_env("GL_ADS_AGENT", account) or DEFAULT_AGENT,
    )

    if mode == "off":
        _err("⚠️  GL_ADS_TICKET_GATE=off — zápis bez ticketu a bez deníčku (jen dev/test).")
        reset_collected()
        return ctx

    if mode == "lite":
        journal = _env("GL_ADS_JOURNAL_FILE", account)
        if not journal:
            _die("GL_ADS_TICKET_GATE=lite vyžaduje GL_ADS_JOURNAL_FILE (cesta k deníčku). "
                 "Nic nebylo zapsáno.", 2)
        path = Path(journal).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(_journal_line(ctx))
        reset_collected()
        return ctx

    # strict
    if not ctx.ticket:
        _die("--confirm vyžaduje --ticket <id> z gl-ads (interventions/claim). "
             "Bez ticketu se do účtu nezapisuje. Nic nebylo zapsáno.", 2)
    ctx.url = (_env("GL_ADS_URL", account) or "").rstrip("/") or None
    ctx.api_key = _env("GL_ADS_API_KEY", account)
    for name, value in (("GL_ADS_URL", ctx.url), ("GL_ADS_API_KEY", ctx.api_key)):
        if not value:
            _die(f"Chybí {name} v .env (ticket gate strict). Nic nebylo zapsáno.", 2)

    try:
        resp = requests.get(f"{ctx.url}/api/v1/interventions/{ctx.ticket}",
                            headers=_headers(ctx.api_key), timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        _die(f"gl-ads nedostupné ({exc.__class__.__name__}), zápis odmítnut. "
             "Nic nebylo zapsáno.", 3)
    if resp.status_code >= 500:
        _die(f"gl-ads vrátilo HTTP {resp.status_code}, zápis odmítnut. Nic nebylo zapsáno.", 3)
    if resp.status_code == 404:
        _die(f"Ticket {ctx.ticket} neexistuje nebo nepatří tvé organizaci. Nic nebylo zapsáno.", 2)
    if resp.status_code in (401, 403):
        _die(f"gl-ads odmítlo API klíč (HTTP {resp.status_code}). Nic nebylo zapsáno.", 2)
    if resp.status_code != 200:
        _die(f"gl-ads vrátilo HTTP {resp.status_code} pro ticket {ctx.ticket}. Nic nebylo zapsáno.", 2)

    try:
        data = _intervention_payload(resp.json())
    except ValueError:
        _die("gl-ads vrátilo neplatný JSON pro ticket. Nic nebylo zapsáno.", 3)

    status = data.get("status")
    if status != "claimed":
        _die(f"Ticket {ctx.ticket} je ve stavu {status!r}, zápis vyžaduje 'claimed'. "
             "Nic nebylo zapsáno.", 2)
    account_info = data.get("adAccount") or {}
    external = _clean_id(str(account_info.get("externalAccountId") or ""))
    if external != ctx.customer_id:
        _die(f"Ticket {ctx.ticket} patří účtu {external or '?'}, příkaz míří na "
             f"{ctx.customer_id}. Nic nebylo zapsáno.", 2)

    reset_collected()
    return ctx


# ---------------------------------------------------------------------------
# Postflight — po zápisu
# ---------------------------------------------------------------------------
def postflight(ctx: TicketContext | None, exit_ok: bool) -> None:
    """Nahlásí mark-executed. Nikdy nevyhodí; selhání jen hlasitě ohlásí."""
    if ctx is None or ctx.mode != "strict" or not ctx.ticket:
        return
    names = collected()
    payload = {
        "externalChangeIds": names,
        "after": {
            "command": ctx.command,
            "ok": exit_ok,
            "resourceNames": len(names),
            "agent": ctx.agent,
            "startedAt": ctx.started_at.isoformat(),
        },
    }
    try:
        resp = requests.post(f"{ctx.url}/api/v1/interventions/{ctx.ticket}/executed",
                             json=payload, headers=_headers(ctx.api_key or ""),
                             timeout=HTTP_TIMEOUT)
        if resp.status_code >= 300:
            raise requests.HTTPError(f"HTTP {resp.status_code}")
        print(f"   ticket {ctx.ticket}: mark-executed OK ({len(names)} resource names)",
              file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        _err(f"⚠️  Zápis proběhl, ale mark-executed pro ticket {ctx.ticket} selhal "
             f"({exc.__class__.__name__}: {exc}). Nahlas ho ručně ze session přes "
             f"MCP `interventions/mark-executed` s externalChangeIds={names!r}.")
