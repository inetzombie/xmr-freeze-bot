from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Optional


@dataclass
class FreezeEvent:
    chain: str
    symbol: str
    decimals: int
    event: str
    kind: str          # "freeze" | "destroy"
    post: bool
    holder: str
    amount_raw: Optional[int]
    amount_source: str
    block: int
    tx: str
    log_index: int
    explorer_url: str
    timestamp: Optional[int] = None
    skip_reason: Optional[str] = None  # set when post=False, stored in the DB
    owner: Optional[str] = None        # wallet owning a Solana token account

    @property
    def uid(self):
        return f"{self.chain}:{self.tx.lower()}:{self.log_index}"

    @property
    def amount(self):
        if self.amount_raw is None:
            return None
        return Decimal(self.amount_raw) / (Decimal(10) ** self.decimals)

    def to_dict(self):
        d = asdict(self)
        d["uid"] = self.uid
        return d
