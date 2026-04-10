"""
Find active Polymarket wallets from recent trades
"""
import httpx
from collections import defaultdict

def find_active_wallets(limit=100):
    """Find most active wallets from recent trades"""
    print(f"Fetching {limit} recent trades from Polymarket...\n")
    
    try:
        r = httpx.get(
            'https://data-api.polymarket.com/trades',
            params={'limit': limit},
            timeout=30
        )
        r.raise_for_status()
        trades = r.json()
        
        # Aggregate by wallet
        wallets = defaultdict(lambda: {'size': 0, 'trades': 0, 'name': '', 'markets': set()})
        
        for trade in trades:
            wallet = trade.get('proxyWallet', '')
            if not wallet:
                continue
                
            wallets[wallet]['size'] += float(trade.get('size', 0))
            wallets[wallet]['trades'] += 1
            wallets[wallet]['name'] = trade.get('name') or trade.get('pseudonym') or 'Anonymous'
            
            market = trade.get('title', '')
            if market:
                wallets[wallet]['markets'].add(market[:50])  # First 50 chars
        
        # Sort by total volume
        top_wallets = sorted(
            wallets.items(),
            key=lambda x: x[1]['size'],
            reverse=True
        )[:15]
        
        print("=" * 80)
        print("TOP 15 MOST ACTIVE WALLETS (by recent volume)")
        print("=" * 80)
        
        for i, (wallet, stats) in enumerate(top_wallets, 1):
            print(f"\n{i}. {wallet}")
            print(f"   Name: {stats['name']}")
            print(f"   Volume: ${stats['size']:,.2f}")
            print(f"   Trades: {stats['trades']}")
            print(f"   Markets: {len(stats['markets'])}")
            
        print("\n" + "=" * 80)
        print("\n💡 TIP: Copy any wallet address above and add it to your watchlist!")
        print("   Example: 0xc8d2e6d8380501ef0db5ed84a9aeb6926b95f1c9\n")
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == '__main__':
    find_active_wallets(200)
