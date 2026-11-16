"""Day-level risk book: entry gates and the daily shutdown rules."""
from dataclasses import dataclass, field


@dataclass
class DayRisk:
    capital: float
    r: dict
    realised: float = 0.0          # net of costs
    entries: int = 0
    consec_losses: int = 0
    halted: str = ""               # reason new entries are blocked
    flatten: bool = False          # daily loss limit hit -> close everything
    per_stock: dict = field(default_factory=dict)

    def record_entry(self, sym):
        self.entries += 1
        self.per_stock[sym] = self.per_stock.get(sym, 0) + 1

    def record_exit(self, net_pnl: float):
        self.realised += net_pnl
        self.consec_losses = self.consec_losses + 1 if net_pnl < 0 else 0
        if self.consec_losses >= self.r["max_consecutive_losses"] and not self.halted:
            self.halted = f"{self.consec_losses} consecutive losses"

    def check_loss(self, unrealised_net: float) -> bool:
        """unrealised_net = open P&L minus entry costs already paid minus estimated exit costs."""
        limit = -self.capital * self.r["daily_loss_limit_pct"] / 100
        if self.realised + unrealised_net <= limit and not self.flatten:
            self.flatten = True
            self.halted = "daily loss limit"
        return self.flatten

    def can_enter(self, sym, s: dict) -> str:
        """'' if allowed, else the reason it's blocked."""
        if self.halted:
            return self.halted
        if self.entries >= self.r["max_entries_per_day"]:
            return "max entries per day"
        if self.per_stock.get(sym, 0) >= s["max_entries_per_stock"]:
            return "max re-entries for stock"
        return ""
