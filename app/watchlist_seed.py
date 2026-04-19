from dataclasses import dataclass
from typing import Iterable, List

from sqlalchemy.orm import Session

from app.models import Wallet
from app.view_helpers import normalize_tags, validate_wallet_address


@dataclass(frozen=True)
class SeedWallet:
    address: str
    label: str
    tags: str
    notes: str


WATCHLIST_SEED_WALLETS: List[SeedWallet] = [
    SeedWallet(
        address="0x56687bf447db6ffa42ffe2204a05edaa20f55839",
        label="Theo4 — All-Time Whale / Election-Event Specialist",
        tags="all_time_whale, politics, election, legacy_elite",
        notes="Official Polymarket all-time leaderboard standout with major 2024 election-related wins.",
    ),
    SeedWallet(
        address="0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee",
        label="kch123 — Sports Elite / High-PnL Specialist",
        tags="sports_specialist, elite",
        notes="Near the top of Polymarket sports all-time PnL leaderboard.",
    ),
    SeedWallet(
        address="0x204f72f35326db932158cba6adff0b9a1da95e14",
        label="swisstony — Sports Specialist / High-Volume Consistency",
        tags="sports_specialist, active_elite, high_volume",
        notes="Strong sports all-time wallet and also appears on recent monthly leaderboard.",
    ),
    SeedWallet(
        address="0x07bdcabf60da99be8fad11092bf4e8412cffe993",
        label="imnotawizard — Current Monthly Heater / Sports Momentum",
        tags="monthly_heater, sports_momentum",
        notes="Official Polymarket monthly leaderboard standout.",
    ),
    SeedWallet(
        address="0x492442eab586f242b53bda933fd5de859c8a3782",
        label="0x4924…3782 — Anonymous Monthly Breakout / Momentum Trader",
        tags="anonymous_breakout, monthly_momentum",
        notes="Address-form monthly leaderboard performer; treat as anonymous active breakout wallet.",
    ),
    SeedWallet(
        address="0x2005d16a84ceefa912d4e380cd32e7ff827875ea",
        label="RN1 — Sports Shark / Large-Scale Specialist",
        tags="sports_shark, large_scale",
        notes="Official Polymarket sports all-time leaderboard wallet.",
    ),
    SeedWallet(
        address="0x507e52ef684ca2dd91f90a9d26d149dd3288beae",
        label="GamblingIsAllYouNeed — Sports Grinder / Specialist",
        tags="sports_grinder, sports_specialist",
        notes="Official Polymarket sports leaderboard wallet.",
    ),
    SeedWallet(
        address="0xd218e474776403a330142299f7796e8ba32eb5c9",
        label="High-Win-Rate Hype Trader",
        tags="high_win_rate, hype_markets",
        notes="Publicly profiled as a high-win-rate hype-market wallet.",
    ),
]


def seed_watchlist_wallets(db: Session, wallets: Iterable[SeedWallet] = WATCHLIST_SEED_WALLETS) -> dict:
    wallet_items = list(wallets)
    added = 0
    updated = 0

    for item in wallet_items:
        address = item.address.strip().lower()
        validation_error = validate_wallet_address(address)
        if validation_error:
            raise ValueError(f"{address}: {validation_error}")

        wallet = db.query(Wallet).filter(Wallet.address == address).first()
        normalized_tags = normalize_tags(item.tags) or None

        if wallet is None:
            wallet = Wallet(
                address=address,
                label=item.label.strip() or None,
                tags=normalized_tags,
                notes=item.notes.strip() or None,
            )
            db.add(wallet)
            added += 1
            continue

        changed = False
        if wallet.label != (item.label.strip() or None):
            wallet.label = item.label.strip() or None
            changed = True
        if wallet.tags != normalized_tags:
            wallet.tags = normalized_tags
            changed = True
        if wallet.notes != (item.notes.strip() or None):
            wallet.notes = item.notes.strip() or None
            changed = True
        if changed:
            updated += 1

    db.flush()
    return {"added": added, "updated": updated, "total": len(wallet_items)}
