from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from sniper.errors import UserFacingError
from sniper.ui import esc

logger = logging.getLogger(__name__)

HONEYPOT_WORDS = ("honeypot", "cannot sell", "can't sell", "freeze authority", "mint authority")


@dataclass
class SafetyReport:
    ok_for_autosnipe: bool
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    mint_authority: str | None = None
    freeze_authority: str | None = None
    decimals: int | None = None
    lp_locked_pct: float | None = None
    risk_level: str | None = None
    liquidity_sol: float | None = None

    def summary(self) -> str:
        lines = []
        if self.blockers:
            lines.append("🚫 <b>BLOCKED</b>: " + esc("; ".join(self.blockers)))
        if self.warnings:
            lines.append("⚠️ <b>WARNINGS</b>: " + esc("; ".join(self.warnings)))
        if not lines:
            lines.append("✅ No blocking issue found by the completed checks.")
        extra = []
        if self.risk_level:
            extra.append(f"risk {esc(self.risk_level)}")
        if self.lp_locked_pct is not None:
            extra.append(f"LP {self.lp_locked_pct:.1f}%")
        if self.liquidity_sol is not None:
            extra.append(f"liq ~{self.liquidity_sol:.2f} SOL")
        if extra:
            lines.append("Data: " + ", ".join(extra))
        return "\n".join(lines)


class SafetyService:
    def __init__(self, http: httpx.AsyncClient, rpc, prices, rugcheck_base: str, require_rugcheck: bool, require_mint_revoked: bool):
        self.http = http
        self.rpc = rpc
        self.prices = prices
        self.rugcheck_base = rugcheck_base.rstrip("/")
        self.require_rugcheck = require_rugcheck
        self.require_mint_revoked = require_mint_revoked

    async def evaluate(self, mint: str, liquidity_sol: float | None = None, *, min_liquidity_sol: float = 0) -> SafetyReport:
        report = SafetyReport(ok_for_autosnipe=False, liquidity_sol=liquidity_sol)
        try:
            info = await self.rpc.get_mint_info(mint)
        except UserFacingError as exc:
            report.blockers.append(str(exc))
            return report
        report.decimals = info["decimals"]
        report.mint_authority = info["mint_authority"]
        report.freeze_authority = info["freeze_authority"]
        if info["freeze_authority"]:
            report.blockers.append("freeze authority is still active")
        if info["mint_authority"]:
            message = "mint authority is still active"
            if self.require_mint_revoked:
                report.blockers.append(message)
            else:
                report.warnings.append(message)
        rug = await self._rugcheck(mint)
        if rug is None:
            message = "Risk check unavailable"
            if self.require_rugcheck:
                report.blockers.append(message)
            else:
                report.warnings.append(message)
        else:
            report.risk_level = rug.get("risk_level")
            report.lp_locked_pct = rug.get("lp_locked_pct")
            if rug.get("honeypot"):
                report.blockers.append("Token is reported as a honeypot or cannot be sold")
            level = (rug.get("risk_level") or "").lower()
            if level in {"danger", "high", "critical"}:
                report.blockers.append(f"Risk level is {rug.get('risk_level')}")
            elif level and level not in {"good", "ok", "safe"}:
                report.warnings.append(f"Risk level is {rug.get('risk_level')}")
            if report.lp_locked_pct is not None and report.lp_locked_pct < 50:
                report.warnings.append(f"LP lock is only {report.lp_locked_pct:.1f}% (new pools are often unlocked at second zero)")
            for danger in rug.get("danger_names") or []:
                if danger not in " ".join(report.blockers):
                    report.blockers.append(f"Flagged risk: {danger}")
        if liquidity_sol is None:
            sol_usd = None
            try:
                prices = await self.prices.usd_prices([])
                sol_usd = prices.get("So11111111111111111111111111111111111111112")
            except Exception:
                sol_usd = None
            try:
                liquidity_sol = await self.prices.pair_liquidity_sol(mint, sol_usd)
            except Exception:
                liquidity_sol = None
            report.liquidity_sol = liquidity_sol
        if min_liquidity_sol > 0:
            if report.liquidity_sol is None:
                report.blockers.append("could not measure pool liquidity")
            elif report.liquidity_sol + 1e-9 < min_liquidity_sol:
                report.blockers.append(
                    f"liquidity {report.liquidity_sol:.2f} SOL is below the minimum {min_liquidity_sol:.2f} SOL"
                )
        report.ok_for_autosnipe = not report.blockers
        return report

    async def _rugcheck(self, mint: str) -> dict | None:
        url = f"{self.rugcheck_base}/v1/tokens/{mint}/report/summary"
        try:
            response = await self.http.get(url, timeout=15)
            if response.status_code == 404:
                return None
            if response.status_code >= 400:
                logger.info("RugCheck HTTP %s", response.status_code)
                return None
            data = response.json()
        except Exception as exc:
            logger.info("RugCheck request failed: %s", exc.__class__.__name__)
            return None
        if not isinstance(data, dict):
            return None
        risks = data.get("risks") or []
        danger_names = []
        honeypot = False
        for risk in risks:
            if not isinstance(risk, dict):
                continue
            name = str(risk.get("name") or risk.get("description") or "")
            level = str(risk.get("level") or "").lower()
            blob = name.lower()
            if any(word in blob for word in HONEYPOT_WORDS) and "revoked" not in blob:
                if "honeypot" in blob or "cannot sell" in blob or "can't sell" in blob:
                    honeypot = True
            if level in {"danger", "critical", "high"}:
                danger_names.append(name[:120])
        lp = data.get("lpLockedPct", data.get("lpLockedPCT"))
        try:
            lp_pct = float(lp) if lp is not None else None
        except (TypeError, ValueError):
            lp_pct = None
        if data.get("lpLocked") is True and lp_pct is None:
            lp_pct = 100.0
        return {
            "risk_level": data.get("riskLevel") or data.get("risk_level"),
            "lp_locked_pct": lp_pct,
            "honeypot": honeypot,
            "danger_names": danger_names,
        }
