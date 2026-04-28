# Polymarket top-wallet performance spike (2026-04-28)

**TL;DR:** The top 100 wallets ranked by 30-day volume **break even
in aggregate** (+0.4% ROI on $2.2B of trades). A train/test
survivorship cut shows a small edge (~+3.8 pp) for the very top wallets,
but the test window is only 7 days and the always-favourite baseline
swings wildly between train and test, so I don't trust the +3.8 pp
as a stable signal. **Recommendation: don't ship copy-trading. A
weak case can be made for a confirmation-filter variant, but only
after a longer survivorship test.**

## Method

- Pulled top 100 wallets by 30-day volume via
  `data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=VOL`.
- Pulled every closed (settled) position for each wallet via
  `data-api.polymarket.com/closed-positions`, filtered to the last
  30 days. **51,364 settled trades total**, $2.23B aggregate cost.
- For each wallet computed: trades, win rate (PnL > 0), total cost,
  total PnL, ROI = PnL/cost, cost-weighted mean entry price (tells
  you whether they're betting favourites or longshots).
- Two baselines on the SAME trade set:
  - "always-favourite" — for each trade, bet the side priced ≥ 0.50
    at entry, with the same cost the wallet wagered.
  - top-100 wallets pooled — collective ROI of the wallets we tracked.
- Survivorship test: split each wallet's 30-day window into 23-day
  train and 7-day test. Picked top 10 wallets by train ROI, measured
  their forward performance in the test window.

Sample-size floor: 20+ resolved trades to be rankable. 91 of 100
wallets cleared this.

## Headline numbers

```
top-100 wallets pooled       51364 trades   $2.23B cost    +0.4% ROI
always-favourite baseline    17625 trades   $1.51B cost   -31.2% ROI
```

Pooled top wallets are essentially break-even over 30 days. The
massive negative on the favourite baseline is suspicious — see the
caveats below; I don't fully trust it.

## Per-wallet distribution (top 25 by ROI)

| Wallet | N | Win % | Cost | PnL | ROI | Mean entry |
|---|---|---|---|---|---|---|
| 0x7da07b2a8b… | 576 | 95.7% | $263K | +$245K | **+93.3%** | 0.02 |
| 0x63a51cbb37… | 66 | 87.9% | $18.6M | +$8.0M | +42.7% | 0.50 |
| 0xbddf61af53… | 132 | 97.0% | $14.8M | +$6.2M | +42.0% | 0.49 |
| 0xdb27bf2ac5… | 83 | 91.6% | $8.6M | +$3.4M | +39.8% | 0.47 |
| 0x5d58e38cd0… | 81 | 87.7% | $16.5M | +$5.9M | +35.7% | 0.52 |
| 0x9495425fee… | 561 | 67.2% | $13.8M | +$3.7M | +26.9% | 0.48 |
| 0x37c1874a60… | 1050 | 64.2% | $16.6M | +$4.2M | +25.3% | 0.48 |
| 0x53757615de… | 194 | 63.9% | $5.6M | +$1.2M | +20.7% | 0.47 |
| 0xfbfd14dd4b… | 76 | 71.1% | $2.0M | +$0.3M | +14.7% | 0.81 |
| 0xe3726a1b9c… | 1050 | 35.7% | $3.1M | +$0.4M | +13.6% | 0.28 |
| 0x36a3f17401… | 23 | 69.6% | $22.9M | +$2.9M | +12.8% | 0.57 |
| 0xfe787d2da7… | 1050 | 70.4% | $11.5M | +$1.4M | +12.6% | 0.53 |
| ... | | | | | | |

Notable patterns:
- **The #1 wallet `0x7da07b2a8b…` has mean_entry of 0.02.** They buy
  tickets priced at 2 cents (heavy underdogs on the OTHER side, i.e.
  a $0.98 favourite the other way) and win 95.7% of the time. This is
  a mechanical "scalp the deep dog" strategy, not "smart money picking
  winners." Easy to replicate without copy-trading: just buy whatever
  is priced ≤ 0.05 on the favoured side.
- **Two of the top 5 wallets (0x63a51cbb37…, 0xdb27bf2ac5…) have
  triple-digit volume and 40%+ ROI with mean entry near 0.50.** Those
  are real picks — beating coinflip markets at 8-figure capital. But
  60-130 trades is small; could be lucky.
- Most rankable wallets cluster around 0-10% ROI. Net edge across the
  full top-100 is +0.4%.

## Survivorship test (23-day train → 7-day test)

```
                                    N   Cost      ROI
top-10 by train ROI, in test    1444  $32.6M  +13.0%
  baseline (always-fav)            -       -   +9.1%

ALL top-100 wallets, in test    33427  $749M    +3.1%
  baseline (always-fav)            -       -  -33.6%
```

Top 10 wallets selected on train ROI did beat the favourite baseline
in the test window: **+3.8 pp of forward edge**. That's the single
positive signal in this analysis.

But:
- The test window is only 7 days. One round of forward validation.
  Need 4-8 weeks to call this real.
- The always-favourite baseline shifts from -31.2% on the full 30d set
  to +9.1% on the top-10 test subset. That's a huge swing, indicating
  the regime in those 7 days was favourite-friendly. The "wallet edge"
  could be the wallets choosing markets in that regime, not picking
  winners.
- The +3.8 pp edge sits inside the noise of the regime shift.

## Caveats

1. **The -31.2% always-favourite full-window baseline is implausibly
   negative for a roughly efficient market.** Two possible explanations:
   (a) the baseline math has a subtle error in reconstructing the
   YES-side price from the wallet's chosen side; (b) top-volume wallets
   are dominated by patterns (longshot scalpers, market makers) whose
   trade selection makes "always bet the favourite at the wallet's
   entry price" pathological. Most likely (b), but worth a sanity
   check before any code commitment.
2. **Volume-ranked, not skill-ranked.** Top-100 by volume includes
   probable market-makers whose ROI is intentionally near zero (they
   earn from spread, not from picking winners). They drag the pooled
   number toward zero. Filtering to wallets with mean_entry between
   0.30 and 0.70 (excluding the longshot scalpers and the heavy
   favourite specialists) would tell us whether mid-market traders
   have edge.
3. **The 30-day window crosses the V2 cutover.** Today (2026-04-28)
   was the V2 contract migration. All trades in the dataset are V1.
   Whether V1 wallet behavior transfers to V2 is unknown until we have
   post-cutover data.
4. **Train/test split is one trial.** Not a meaningful out-of-sample
   test. Real forward validation would re-run this weekly for 4-8
   weeks.

## Recommendation

**Don't ship copy-trading.** The aggregate signal is too weak (~breakeven)
and the per-wallet patterns that look profitable are mechanical
strategies (longshot scalping, heavy-favourite betting) you'd
implement directly, not via copy.

**A confirmation-filter variant is worth considering** but only after
a longer test:
- Run this spike weekly for 4 consecutive weeks.
- Track whether each week's "top 10 by trailing 23d ROI" has positive
  forward edge in week 5.
- If 3 of 4 weeks show > +2 pp edge after baseline, then build a
  feature: only trade markets where forecast + market favourite +
  net wallet flow from the smart-money cohort all agree. Skip
  otherwise.
- If less than 3 of 4, drop the idea.

**Cheaper alternative that uses the same signal:** add an "aggregate
top-wallet flow" gate to the existing V1 sizer. Don't pick wallets;
just check whether the top-N wallets in a market are net long or net
short, and require that direction to agree with the market favourite.
This avoids identifying who's smart and just leverages the wisdom of
all heavy traders.

## Reproduce

```
cd Delfibot/research/wallet_spike
python3 pull_wallets.py    # ~8 min, hits Polymarket data-api
python3 analyze.py         # < 1 sec
```

Raw payloads in `raw/`, CSVs in `wallets.csv` + `trades.csv`. All
gitignored — only the scripts and this writeup are committed.
