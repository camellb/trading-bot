# Trade-level analysis — Phase 5 backtest (90 days)

Generated 2026-04-21T06:11:49+00:00. Sizer: `execution/pm_sizer.py` (positive-EV, flat confidence-scaled stake). Cost model: 1.5% per trade. User config: default (min_ev=0.03, base_stake=0.02, max_stake=0.05, dry_powder_reserve=0.20).

- Trades taken: **156**  
- Trades resolved: **20**  
- Wins: **6**  
- Trades open: **136**


## Section 1 — Per-trade table (all 156)

Sorted by resolution date ascending; open / unknown-resolution trades at the bottom.

| # | Market | Archetype | Res date | cp | cf | ask Y | ask N | mktP Y | Δ | side | EV | bucket | stake | entry | outcome | P&L | W |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 78 | Bitcoin Up or Down - April 20, 5:30AM-5:45AM ET | price_threshold | 2026-04-20 | 0.42 | 0.45 | 0.385 | 0.615 | 0.39 | 0.03 | YES | +7.6% | 5-10% | $3.73 | 0.385 | YES | +$5.96 | ✓ |
| 93 | Bitcoin Up or Down - April 20, 6:45AM-7:00AM ET | price_threshold | 2026-04-20 | 0.18 | 0.40 | 0.135 | 0.865 | 0.14 | 0.04 | YES | +31.8% | 20%+ | $3.27 | 0.135 | NO | -$3.27 | ✗ |
| 35 | LoL: Weibo Gaming vs Oh My God (BO3) - Esports World Cup Ch… | sports_match | 2026-04-21 | 0.75 | 0.45 | 0.815 | 0.185 | 0.81 | 0.06 | NO | +33.6% | 20%+ | $5.48 | 0.185 | YES | -$5.48 | ✗ |
| 18 | Bitcoin Up or Down on April 20? | price_threshold | 2026-04-21 | 0.38 | 0.45 | 0.105 | 0.895 | 0.10 | 0.28 | YES | +260.4% | 20%+ | $6.65 | 0.105 | NO | -$6.65 | ✗ |
| 66 | Will the price of Bitcoin be between $72,000 and $74,000 on… | price_threshold | 2026-04-21 | 0.21 | 0.60 | 0.175 | 0.825 | 0.17 | 0.04 | YES | +18.5% | 10-20% | $8.34 | 0.175 | NO | -$8.34 | ✗ |
| 94 | Madrid Open, Qualification: Sara Sorribes Tormo vs Tamara K… | sports_match | 2026-04-21 | 0.72 | 0.60 | 0.585 | 0.415 | 0.58 | 0.14 | YES | +21.6% | 20%+ | $6.49 | 0.585 | NO | -$6.49 | ✗ |
| 73 | Counter-Strike: KUUSAMO.gg vs Fire Flux Esports (BO3) - Eur… | sports_match | 2026-04-21 | 0.40 | 0.60 | 0.735 | 0.265 | 0.73 | 0.33 | NO | +124.9% | 20%+ | $7.96 | 0.265 | YES | -$7.96 | ✗ |
| 72 | Counter-Strike: KUUSAMO.gg vs Fire Flux Esports - Map 2 Win… | sports_match | 2026-04-21 | 0.45 | 0.40 | 0.555 | 0.445 | 0.56 | 0.11 | NO | +22.1% | 20%+ | $3.94 | 0.445 | NO | +$4.91 | ✓ |
| 134 | Ethereum Up or Down - April 20, 4PM ET | price_threshold | 2026-04-21 | 0.75 | 0.35 | 0.895 | 0.105 | 0.90 | 0.15 | NO | +136.6% | 20%+ | $2.32 | 0.105 | YES | -$2.32 | ✗ |
| 109 | Madrid Open, Qualification: Ksenia Efremova vs Lulu Sun | sports_match | 2026-04-21 | 0.25 | 0.45 | 0.165 | 0.835 | 0.17 | 0.08 | YES | +50.0% | 20%+ | $2.76 | 0.165 | YES | +$13.97 | ✓ |
| 39 | S&P 500 (SPX) Up or Down on April 20? | binary_event | 2026-04-21 | 0.32 | 0.45 | 0.240 | 0.760 | 0.24 | 0.08 | YES | +31.8% | 20%+ | $5.31 | 0.240 | NO | -$5.31 | ✗ |
| 24 | S&P 500 (SPX) Opens Up or Down on April 20? | binary_event | 2026-04-21 | 0.25 | 0.40 | 0.105 | 0.895 | 0.10 | 0.15 | YES | +136.6% | 20%+ | $6.23 | 0.105 | NO | -$6.23 | ✗ |
| 69 | SPY (SPY) Up or Down on April 20? | binary_event | 2026-04-21 | 0.35 | 0.40 | 0.215 | 0.785 | 0.21 | 0.13 | YES | +61.3% | 20%+ | $4.04 | 0.215 | NO | -$4.04 | ✗ |
| 150 | Bitcoin Up or Down - April 20, 8:45PM-9:00PM ET | price_threshold | 2026-04-21 | 0.58 | 0.33 | 0.705 | 0.295 | 0.70 | 0.12 | NO | +39.6% | 20%+ | $2.05 | 0.295 | YES | -$2.05 | ✗ |
| 120 | Dota 2: Zero Tenacity vs Power Rangers - Game 1 Winner | sports_match | 2026-04-21 | 0.45 | 0.55 | 0.705 | 0.295 | 0.70 | 0.25 | NO | +84.9% | 20%+ | $5.30 | 0.295 | YES | -$5.30 | ✗ |
| 144 | Valorant: Nightblood Gaming vs Division One (BO3) - VCL Nor… | sports_match | 2026-04-21 | 0.45 | 0.65 | 0.315 | 0.685 | 0.32 | 0.14 | YES | +41.4% | 20%+ | $4.24 | 0.315 | NO | -$4.24 | ✗ |
| 86 | St. Louis Cardinals vs. Miami Marlins: O/U 8.5 | sports_prop | 2026-04-21 | 0.55 | 0.40 | 0.445 | 0.555 | 0.45 | 0.11 | YES | +22.1% | 20%+ | $3.49 | 0.445 | NO | -$3.49 | ✗ |
| 50 | Spread: Cavaliers (-8.5) | sports_prop | 2026-04-21 | 0.62 | 0.45 | 0.505 | 0.495 | 0.51 | 0.11 | YES | +21.3% | 20%+ | $4.74 | 0.505 | YES | +$4.65 | ✓ |
| 125 | Senators vs. Hurricanes: O/U 5.5 | sports_prop | 2026-04-21 | 0.42 | 0.55 | 0.545 | 0.455 | 0.55 | 0.13 | NO | +26.0% | 20%+ | $4.97 | 0.455 | NO | +$5.95 | ✓ |
| 146 | Valorant: Alliance Guardians vs Pigeons (BO3) - VCL North A… | sports_match | 2026-04-21 | 0.65 | 0.45 | 0.750 | 0.250 | 0.75 | 0.10 | NO | +38.5% | 20%+ | $2.05 | 0.250 | NO | +$6.16 | ✓ |
| 1 | Will Israel conduct military action against Iran by April 2… | geopolitical | open | 0.25 | 0.60 | 0.162 | 0.838 | 0.16 | 0.09 | YES | +52.8% | 20%+ | $16.00 | 0.162 | open | — | — |
| 11 | Trump announces US x Iran ceasefire end by April 21, 2026? | binary_event | open | 0.35 | 0.40 | 0.235 | 0.765 | 0.23 | 0.11 | YES | +47.4% | 20%+ | $7.09 | 0.235 | open | — | — |
| 127 | Spread: Nuggets (-7.5) | sports_prop | open | 0.58 | 0.40 | 0.505 | 0.495 | 0.51 | 0.07 | YES | +13.4% | 10-20% | $2.51 | 0.505 | open | — | — |
| 122 | Will Bitcoin reach $78,000 on April 20? | price_threshold | open | 0.18 | 0.60 | 0.083 | 0.917 | 0.08 | 0.10 | YES | +115.4% | 20%+ | $5.17 | 0.083 | open | — | — |
| 5 | Will Elon Musk post 200-219 tweets from April 14 to April 2… | price_threshold | open | 0.28 | 0.45 | 0.216 | 0.784 | 0.22 | 0.06 | YES | +28.1% | 20%+ | $7.56 | 0.216 | open | — | — |
| 8 | Will Elon Musk post 220-239 tweets from April 14 to April 2… | price_threshold | open | 0.35 | 0.65 | 0.395 | 0.605 | 0.40 | 0.05 | NO | +5.9% | 5-10% | $14.64 | 0.605 | open | — | — |
| 41 | Magic vs. Pistons | sports_match | open | 0.28 | 0.60 | 0.215 | 0.785 | 0.21 | 0.07 | YES | +28.7% | 20%+ | $10.44 | 0.215 | open | — | — |
| 23 | Will Donald Trump announce that the United States blockade … | binary_event | open | 0.18 | 0.55 | 0.250 | 0.750 | 0.25 | 0.07 | NO | +7.8% | 5-10% | $12.67 | 0.750 | open | — | — |
| 2 | US x Iran diplomatic meeting by April 21, 2026? | geopolitical | open | 0.65 | 0.40 | 0.620 | 0.380 | 0.62 | 0.03 | YES | +3.3% | 3-5% | $7.87 | 0.620 | open | — | — |
| 3 | US x Iran ceasefire extended by April 21, 2026? | geopolitical | open | 0.25 | 0.70 | 0.610 | 0.390 | 0.61 | 0.36 | NO | +90.8% | 20%+ | $15.62 | 0.390 | open | — | — |
| 4 | 76ers vs. Celtics | sports_match | open | 0.14 | 0.50 | 0.115 | 0.885 | 0.12 | 0.03 | YES | +20.2% | 20%+ | $15.37 | 0.115 | open | — | — |
| 6 | Dota 2: Heroic vs Virtus.pro (BO3) - PGL Wallachia Group St… | sports_match | open | 0.58 | 0.45 | 0.605 | 0.395 | 0.60 | 0.03 | NO | +4.8% | 3-5% | $7.50 | 0.395 | open | — | — |
| 7 | Will Elon Musk post 40-64 tweets from April 18 to April 20,… | other | open | 0.82 | 0.50 | 0.835 | 0.165 | 0.83 | 0.02 | NO | +7.6% | 5-10% | $14.88 | 0.165 | open | — | — |
| 9 | Will the price of Bitcoin be above $74,000 on April 20? | price_threshold | open | 0.58 | 0.45 | 0.605 | 0.395 | 0.60 | 0.03 | NO | +4.8% | 3-5% | $7.20 | 0.395 | open | — | — |
| 10 | Timberwolves vs. Nuggets | sports_match | open | 0.32 | 0.45 | 0.295 | 0.705 | 0.29 | 0.03 | YES | +7.0% | 5-10% | $7.15 | 0.295 | open | — | — |
| 12 | Will the price of Bitcoin be above $76,000 on April 20? | price_threshold | open | 0.12 | 0.45 | 0.085 | 0.915 | 0.09 | 0.03 | YES | +39.7% | 20%+ | $7.03 | 0.085 | open | — | — |
| 13 | Dota 2: Team Spirit vs GamerLegion (BO3) - PGL Wallachia Gr… | sports_match | open | 0.58 | 0.40 | 0.635 | 0.365 | 0.64 | 0.06 | NO | +13.6% | 10-20% | $6.98 | 0.365 | open | — | — |
| 14 | Dota 2: Team Liquid vs Vici Gaming (BO3) - PGL Wallachia Gr… | sports_match | open | 0.58 | 0.45 | 0.615 | 0.385 | 0.61 | 0.04 | NO | +7.6% | 5-10% | $6.92 | 0.385 | open | — | — |
| 15 | Gwangju: Clement Chidekh vs Coleman Wong | sports_match | open | 0.35 | 0.45 | 0.315 | 0.685 | 0.32 | 0.03 | YES | +9.6% | 5-10% | $6.87 | 0.315 | open | — | — |
| 16 | Dota 2: BetBoom Team vs Team Falcons (BO3) - PGL Wallachia … | sports_match | open | 0.44 | 0.45 | 0.415 | 0.585 | 0.41 | 0.03 | YES | +4.5% | 3-5% | $6.81 | 0.415 | open | — | — |
| 17 | Will Real Madrid CF win on 2026-04-21? | sports_match | open | 0.70 | 0.60 | 0.755 | 0.245 | 0.76 | 0.06 | NO | +20.9% | 20%+ | $13.51 | 0.245 | open | — | — |
| 19 | Senators vs. Hurricanes | sports_match | open | 0.38 | 0.40 | 0.425 | 0.575 | 0.42 | 0.04 | NO | +6.3% | 5-10% | $6.59 | 0.575 | open | — | — |
| 20 | Will West Ham United FC win on 2026-04-20? | sports_match | open | 0.38 | 0.45 | 0.335 | 0.665 | 0.34 | 0.04 | YES | +11.9% | 10-20% | $6.54 | 0.335 | open | — | — |
| 21 | Raptors vs. Cavaliers | sports_match | open | 0.20 | 0.60 | 0.235 | 0.765 | 0.23 | 0.03 | NO | +3.1% | 3-5% | $12.98 | 0.765 | open | — | — |
| 22 | LoL: Gen.G Global Academy vs Nongshim Esports Academy (BO3)… | sports_match | open | 0.22 | 0.45 | 0.205 | 0.795 | 0.20 | 0.02 | YES | +5.8% | 5-10% | $6.39 | 0.205 | open | — | — |
| 25 | Will the price of Ethereum be above $2,200 on April 20? | price_threshold | open | 0.89 | 0.60 | 0.916 | 0.084 | 0.92 | 0.03 | NO | +29.5% | 20%+ | $12.37 | 0.084 | open | — | — |
| 26 | Will Manchester City FC win on 2026-04-22? | sports_match | open | 0.82 | 0.60 | 0.850 | 0.150 | 0.85 | 0.03 | NO | +18.5% | 10-20% | $12.17 | 0.150 | open | — | — |
| 27 | Will Crystal Palace FC win on 2026-04-20? | sports_match | open | 0.42 | 0.45 | 0.385 | 0.615 | 0.39 | 0.03 | YES | +7.6% | 5-10% | $5.99 | 0.385 | open | — | — |
| 28 | Will the highest temperature in Shanghai be 18°C on April 2… | weather | open | 0.20 | 0.70 | 0.874 | 0.126 | 0.87 | 0.67 | NO | +533.4% | 20%+ | $11.88 | 0.126 | open | — | — |
| 29 | LoL: Bilibili Gaming vs Invictus Gaming (BO3) - Esports Wor… | sports_match | open | 0.79 | 0.50 | 0.815 | 0.185 | 0.81 | 0.02 | NO | +12.0% | 10-20% | $11.69 | 0.185 | open | — | — |
| 30 | Dota 2: Team Liquid vs Vici Gaming - Game 1 Winner | sports_match | open | 0.38 | 0.45 | 0.350 | 0.650 | 0.35 | 0.03 | YES | +7.1% | 5-10% | $5.75 | 0.350 | open | — | — |
| 31 | Rockets vs. Lakers | sports_match | open | 0.58 | 0.45 | 0.625 | 0.375 | 0.62 | 0.05 | NO | +10.5% | 10-20% | $5.70 | 0.375 | open | — | — |
| 32 | Madrid Open, Qualification: Cristian Garin vs Nicolas Jarry | sports_match | open | 0.58 | 0.45 | 0.505 | 0.495 | 0.51 | 0.07 | YES | +13.4% | 10-20% | $5.66 | 0.505 | open | — | — |
| 33 | Gwangju: Yu-Hsiou Hsu vs Murphy Cassone | sports_match | open | 0.78 | 0.60 | 0.805 | 0.195 | 0.81 | 0.03 | NO | +11.3% | 10-20% | $11.23 | 0.195 | open | — | — |
| 34 | ODI Series Bangladesh vs New Zealand: Bangladesh vs New Zea… | sports_match | open | 0.58 | 0.40 | 0.605 | 0.395 | 0.60 | 0.03 | NO | +4.8% | 3-5% | $5.52 | 0.395 | open | — | — |
| 36 | Flyers vs. Penguins | sports_match | open | 0.45 | 0.40 | 0.425 | 0.575 | 0.42 | 0.03 | YES | +4.4% | 3-5% | $5.44 | 0.425 | open | — | — |
| 37 | Dota 2: Aurora vs PARIVISION (BO3) - PGL Wallachia Group St… | sports_match | open | 0.32 | 0.45 | 0.350 | 0.650 | 0.35 | 0.03 | NO | +3.1% | 3-5% | $5.39 | 0.650 | open | — | — |
| 38 | Will the price of Bitcoin be above $78,000 on April 21? | price_threshold | open | 0.09 | 0.40 | 0.080 | 0.920 | 0.08 | 0.01 | YES | +11.0% | 10-20% | $5.35 | 0.080 | open | — | — |
| 40 | Madrid Open, Qualification: Zachary Svajda vs Rei Sakamoto | sports_match | open | 0.42 | 0.45 | 0.395 | 0.605 | 0.40 | 0.02 | YES | +4.8% | 3-5% | $5.26 | 0.395 | open | — | — |
| 42 | Madrid Open, Qualification: Andrea Pellegrino vs Martin Damm | sports_match | open | 0.58 | 0.40 | 0.505 | 0.495 | 0.51 | 0.07 | YES | +13.4% | 10-20% | $5.14 | 0.505 | open | — | — |
| 43 | Spread: Knicks (-5.5) | sports_prop | open | 0.55 | 0.40 | 0.505 | 0.495 | 0.51 | 0.05 | YES | +7.4% | 5-10% | $5.10 | 0.505 | open | — | — |
| 44 | Timberwolves vs. Nuggets: O/U 231.5 | sports_prop | open | 0.45 | 0.45 | 0.515 | 0.485 | 0.52 | 0.07 | NO | +11.9% | 10-20% | $5.06 | 0.485 | open | — | — |
| 45 | Will Paris Saint-Germain FC win on 2026-04-22? | sports_match | open | 0.80 | 0.45 | 0.825 | 0.175 | 0.82 | 0.02 | NO | +12.8% | 10-20% | $5.02 | 0.175 | open | — | — |
| 46 | Will Elon Musk post 280-299 tweets from April 17 to April 2… | other | open | 0.11 | 0.45 | 0.105 | 0.895 | 0.10 | 0.01 | YES | +3.3% | 3-5% | $4.98 | 0.105 | open | — | — |
| 47 | LoL: paiN Gaming Academy vs 7REX (BO1) - Circuito Desafiant… | sports_match | open | 0.35 | 0.45 | 0.385 | 0.615 | 0.39 | 0.04 | NO | +4.2% | 3-5% | $4.94 | 0.615 | open | — | — |
| 48 | Madrid Open, Qualification: Alina Charaeva vs Aliaksandra S… | sports_match | open | 0.23 | 0.60 | 0.215 | 0.785 | 0.21 | 0.02 | YES | +5.5% | 5-10% | $9.79 | 0.215 | open | — | — |
| 49 | Will FC Bayern München win on 2026-04-25? | sports_match | open | 0.58 | 0.60 | 0.610 | 0.390 | 0.61 | 0.03 | NO | +6.2% | 5-10% | $9.64 | 0.390 | open | — | — |
| 51 | Will Chelsea FC win on 2026-04-21? | sports_match | open | 0.32 | 0.45 | 0.355 | 0.645 | 0.35 | 0.03 | NO | +3.9% | 3-5% | $4.78 | 0.645 | open | — | — |
| 52 | Hawks vs. Knicks: O/U 216.5 | sports_prop | open | 0.45 | 0.40 | 0.515 | 0.485 | 0.52 | 0.07 | NO | +11.9% | 10-20% | $4.74 | 0.485 | open | — | — |
| 53 | Counter-Strike: MOUZ NXT vs CYBERSHOKE Esports (BO3) - NODW… | sports_match | open | 0.40 | 0.45 | 0.370 | 0.630 | 0.37 | 0.03 | YES | +6.6% | 5-10% | $4.70 | 0.370 | open | — | — |
| 54 | Will the price of Bitcoin be above $72,000 on April 21? | price_threshold | open | 0.88 | 0.45 | 0.898 | 0.102 | 0.90 | 0.02 | NO | +16.7% | 10-20% | $4.66 | 0.102 | open | — | — |
| 55 | Detroit Tigers vs. Boston Red Sox: O/U 8.5 | sports_prop | open | 0.42 | 0.45 | 0.445 | 0.555 | 0.45 | 0.03 | NO | +3.0% | 3-5% | $4.63 | 0.555 | open | — | — |
| 56 | Madrid Open, Qualification: Simona Waltert vs Despina Papam… | sports_match | open | 0.68 | 0.45 | 0.705 | 0.295 | 0.70 | 0.02 | NO | +7.0% | 5-10% | $4.59 | 0.295 | open | — | — |
| 57 | Madrid Open, Qualification: Arthur Gea vs Vilius Gaubas | sports_match | open | 0.22 | 0.50 | 0.185 | 0.815 | 0.18 | 0.04 | YES | +17.4% | 10-20% | $9.11 | 0.185 | open | — | — |
| 58 | LoL: Weibo Gaming vs Oh My God - Game 1 Winner | sports_match | open | 0.74 | 0.40 | 0.755 | 0.245 | 0.76 | 0.02 | NO | +4.6% | 3-5% | $4.48 | 0.245 | open | — | — |
| 59 | Madrid Open, Qualification: Nikola Bartunkova vs Katarzyna … | sports_match | open | 0.62 | 0.45 | 0.575 | 0.425 | 0.57 | 0.05 | YES | +6.3% | 5-10% | $4.44 | 0.575 | open | — | — |
| 60 | Madrid Open, Qualification: Ruth Roura vs Dominika Salkova | sports_match | open | 0.10 | 0.45 | 0.085 | 0.915 | 0.08 | 0.02 | YES | +16.8% | 10-20% | $4.41 | 0.085 | open | — | — |
| 61 | Dota 2: Team Liquid vs Vici Gaming - Game 2 Winner | sports_match | open | 0.62 | 0.45 | 0.590 | 0.410 | 0.59 | 0.03 | YES | +3.6% | 3-5% | $4.37 | 0.590 | open | — | — |
| 62 | Will Como 1907 win on 2026-04-21? | sports_match | open | 0.22 | 0.45 | 0.195 | 0.805 | 0.20 | 0.02 | YES | +11.3% | 10-20% | $4.34 | 0.195 | open | — | — |
| 63 | Will the price of Ethereum be above $2,400 on April 21? | price_threshold | open | 0.16 | 0.45 | 0.140 | 0.860 | 0.14 | 0.02 | YES | +12.8% | 10-20% | $4.30 | 0.140 | open | — | — |
| 64 | Will Deportivo Alavés win on 2026-04-21? | sports_match | open | 0.09 | 0.45 | 0.085 | 0.915 | 0.09 | 0.00 | YES | +4.4% | 3-5% | $4.27 | 0.085 | open | — | — |
| 65 | Real Madrid CF vs. Deportivo Alavés: O/U 2.5 | sports_prop | open | 0.72 | 0.60 | 0.655 | 0.345 | 0.66 | 0.06 | YES | +8.4% | 5-10% | $8.47 | 0.655 | open | — | — |
| 67 | Will Donald Trump post 200+ Truth Social posts from April 1… | binary_event | open | 0.21 | 0.45 | 0.255 | 0.745 | 0.26 | 0.05 | NO | +4.5% | 3-5% | $4.10 | 0.745 | open | — | — |
| 68 | Will Athletic Club vs. CA Osasuna end in a draw? | sports_match | open | 0.31 | 0.45 | 0.295 | 0.705 | 0.29 | 0.02 | YES | +3.6% | 3-5% | $4.07 | 0.295 | open | — | — |
| 70 | Games Total: O/U 2.5 | sports_prop | open | 0.30 | 0.45 | 0.285 | 0.715 | 0.28 | 0.02 | YES | +3.8% | 3-5% | $4.00 | 0.285 | open | — | — |
| 71 | Games Total: O/U 2.5 | sports_prop | open | 0.40 | 0.45 | 0.360 | 0.640 | 0.36 | 0.04 | YES | +9.6% | 5-10% | $3.97 | 0.360 | open | — | — |
| 74 | Bitcoin Up or Down - April 20, 5:15AM-5:30AM ET | price_threshold | open | 0.56 | 0.45 | 0.585 | 0.415 | 0.58 | 0.02 | NO | +4.5% | 3-5% | $3.92 | 0.415 | open | — | — |
| 75 | Madrid Open, Qualification: Anna Blinkova vs Nuria Brancacc… | sports_match | open | 0.55 | 0.45 | 0.585 | 0.415 | 0.58 | 0.03 | NO | +6.9% | 5-10% | $3.88 | 0.415 | open | — | — |
| 76 | Madrid Open, Qualification: Elvina Kalieva vs Suzan Lamens | sports_match | open | 0.42 | 0.50 | 0.445 | 0.555 | 0.45 | 0.03 | NO | +3.0% | 3-5% | $7.71 | 0.555 | open | — | — |
| 77 | Shymkent: Sandro Kopp vs Damir Zhalgasbay | sports_match | open | 0.80 | 0.55 | 0.820 | 0.180 | 0.82 | 0.02 | NO | +9.6% | 5-10% | $7.58 | 0.180 | open | — | — |
| 79 | Madrid Open, Qualification: Yibing Wu vs Manoj Dhamne Manas | sports_match | open | 0.82 | 0.60 | 0.845 | 0.155 | 0.84 | 0.03 | NO | +14.6% | 10-20% | $7.56 | 0.155 | open | — | — |
| 80 | LoL: BNK FearX Youth vs KT Rolster Challengers (BO3) - LCK … | sports_match | open | 0.32 | 0.45 | 0.305 | 0.695 | 0.30 | 0.02 | YES | +3.4% | 3-5% | $3.72 | 0.305 | open | — | — |
| 81 | Madrid Open, Qualification: Elmer Moeller vs Hugo Gaston | sports_match | open | 0.36 | 0.50 | 0.340 | 0.660 | 0.34 | 0.02 | YES | +4.4% | 3-5% | $7.38 | 0.340 | open | — | — |
| 82 | LoL: Weibo Gaming vs Oh My God - Game 2 Winner | sports_match | open | 0.70 | 0.40 | 0.725 | 0.275 | 0.72 | 0.03 | NO | +7.6% | 5-10% | $3.63 | 0.275 | open | — | — |
| 83 | Dota 2: Team Yandex vs Natus Vincere (BO3) - PGL Wallachia … | sports_match | open | 0.58 | 0.40 | 0.600 | 0.400 | 0.60 | 0.02 | NO | +3.5% | 3-5% | $3.60 | 0.400 | open | — | — |
| 84 | Dota 2: Nigma Galaxy vs 1win (BO3) - DreamLeague Division 2… | sports_match | open | 0.80 | 0.45 | 0.850 | 0.150 | 0.85 | 0.05 | NO | +31.8% | 20%+ | $3.57 | 0.150 | open | — | — |
| 85 | Will the Virginia redistricting referendum pass? | binary_event | open | 0.82 | 0.60 | 0.845 | 0.155 | 0.84 | 0.03 | NO | +14.6% | 10-20% | $7.09 | 0.155 | open | — | — |
| 87 | LoL: BNK FearX Youth vs KT Rolster Challengers - Game 1 Win… | sports_match | open | 0.22 | 0.45 | 0.210 | 0.790 | 0.21 | 0.01 | YES | +3.3% | 3-5% | $3.46 | 0.210 | open | — | — |
| 88 | Madrid Open, Qualification: Donna Vekic vs Leolia Jeanjean | sports_match | open | 0.78 | 0.60 | 0.795 | 0.205 | 0.80 | 0.02 | NO | +5.8% | 5-10% | $6.86 | 0.205 | open | — | — |
| 89 | Madrid Open, Qualification: Rebeka Masarova vs Rebecca Sram… | sports_match | open | 0.62 | 0.45 | 0.640 | 0.360 | 0.64 | 0.02 | NO | +4.1% | 3-5% | $3.38 | 0.360 | open | — | — |
| 90 | Shymkent: Tomasz Berkieta vs Jelle Sels | sports_match | open | 0.65 | 0.45 | 0.675 | 0.325 | 0.68 | 0.03 | NO | +6.2% | 5-10% | $3.35 | 0.325 | open | — | — |
| 91 | Madrid Open, Qualification: Aleksandar Vukic vs Pablo Llama… | sports_match | open | 0.22 | 0.45 | 0.195 | 0.805 | 0.20 | 0.02 | YES | +11.3% | 10-20% | $3.32 | 0.195 | open | — | — |
| 92 | Game Handicap: BLG (-1.5) vs Invictus Gaming (+1.5) | sports_prop | open | 0.55 | 0.45 | 0.590 | 0.410 | 0.59 | 0.04 | NO | +8.3% | 5-10% | $3.30 | 0.410 | open | — | — |
| 95 | Shymkent: Petr Bar Biryukov vs Gijs Brouwer | sports_match | open | 0.18 | 0.50 | 0.145 | 0.855 | 0.14 | 0.04 | YES | +22.6% | 20%+ | $6.38 | 0.145 | open | — | — |
| 96 | Madrid Open, Qualification: Marco Trungelliti vs Borna Gojo | sports_match | open | 0.56 | 0.45 | 0.505 | 0.495 | 0.51 | 0.06 | YES | +9.4% | 5-10% | $3.14 | 0.505 | open | — | — |
| 97 | Madrid Open, Qualification: Dino Prizmic vs Christopher O'C… | sports_match | open | 0.78 | 0.55 | 0.835 | 0.165 | 0.83 | 0.05 | NO | +31.8% | 20%+ | $6.23 | 0.165 | open | — | — |
| 98 | Will US Lecce win on 2026-04-20? | sports_match | open | 0.28 | 0.45 | 0.265 | 0.735 | 0.27 | 0.02 | YES | +4.2% | 3-5% | $3.06 | 0.265 | open | — | — |
| 99 | Will Gaziantep FK vs. Kayserispor end in a draw? | sports_match | open | 0.30 | 0.45 | 0.285 | 0.715 | 0.28 | 0.02 | YES | +3.8% | 3-5% | $3.04 | 0.285 | open | — | — |
| 100 | Madrid Open, Qualification: Anastasia Potapova vs Teodora K… | sports_match | open | 0.75 | 0.45 | 0.800 | 0.200 | 0.80 | 0.05 | NO | +23.5% | 20%+ | $3.02 | 0.200 | open | — | — |
| 101 | Will Girona FC win on 2026-04-21? | sports_match | open | 0.42 | 0.45 | 0.385 | 0.615 | 0.39 | 0.03 | YES | +7.6% | 5-10% | $2.99 | 0.385 | open | — | — |
| 102 | Madrid Open, Qualification: Hanne Vandewinkel vs Linda Fruh… | sports_match | open | 0.88 | 0.60 | 0.918 | 0.082 | 0.92 | 0.04 | NO | +45.7% | 20%+ | $5.94 | 0.082 | open | — | — |
| 103 | Counter-Strike: Sinners vs Sashi Esport (BO3) - Conquest of… | sports_match | open | 0.60 | 0.45 | 0.570 | 0.430 | 0.57 | 0.03 | YES | +3.8% | 3-5% | $2.92 | 0.570 | open | — | — |
| 104 | Counter-Strike: STATE vs Washed (BO3) - Dust2.dk Ligaen Gro… | sports_match | open | 0.55 | 0.40 | 0.500 | 0.500 | 0.50 | 0.05 | YES | +8.5% | 5-10% | $2.90 | 0.500 | open | — | — |
| 105 | LoL: SK Gaming vs Karmine Corp - Game 1 Winner | sports_match | open | 0.25 | 0.60 | 0.235 | 0.765 | 0.23 | 0.02 | YES | +4.9% | 3-5% | $5.75 | 0.235 | open | — | — |
| 106 | Dota 2: MOUZ vs Tundra Esports - Game 1 Winner | sports_match | open | 0.45 | 0.40 | 0.520 | 0.480 | 0.52 | 0.07 | NO | +13.1% | 10-20% | $2.83 | 0.480 | open | — | — |
| 107 | Savannah: Michael Mmoh vs Franco Roncadelli | sports_match | open | 0.75 | 0.45 | 0.770 | 0.230 | 0.77 | 0.02 | NO | +7.2% | 5-10% | $2.81 | 0.230 | open | — | — |
| 108 | Spread: Crystal Palace FC (-1.5) | sports_prop | open | 0.18 | 0.45 | 0.165 | 0.835 | 0.17 | 0.01 | YES | +7.6% | 5-10% | $2.78 | 0.165 | open | — | — |
| 110 | Game Handicap: KC (-1.5) vs SK Gaming (+1.5) | sports_prop | open | 0.75 | 0.45 | 0.780 | 0.220 | 0.78 | 0.03 | NO | +12.1% | 10-20% | $2.87 | 0.220 | open | — | — |
| 111 | Madrid Open, Qualification: Martina Trevisan vs Sinja Kraus | sports_match | open | 0.35 | 0.45 | 0.300 | 0.700 | 0.30 | 0.05 | YES | +15.2% | 10-20% | $2.85 | 0.300 | open | — | — |
| 112 | Crystal Palace FC vs. West Ham United FC: O/U 1.5 | sports_prop | open | 0.72 | 0.45 | 0.755 | 0.245 | 0.76 | 0.04 | NO | +12.8% | 10-20% | $2.83 | 0.245 | open | — | — |
| 113 | Bitcoin Up or Down - April 20, 2:00PM-2:15PM ET | price_threshold | open | 0.75 | 0.45 | 0.765 | 0.235 | 0.77 | 0.02 | NO | +4.9% | 3-5% | $2.80 | 0.235 | open | — | — |
| 114 | Dota 2: MOUZ vs Tundra Esports - Game 2 Winner | sports_match | open | 0.35 | 0.45 | 0.380 | 0.620 | 0.38 | 0.03 | NO | +3.3% | 3-5% | $2.78 | 0.620 | open | — | — |
| 115 | Madrid Open, Qualification: Zsombor Piros vs Tomas Barrios | sports_match | open | 0.72 | 0.45 | 0.680 | 0.320 | 0.68 | 0.04 | YES | +4.4% | 3-5% | $2.76 | 0.680 | open | — | — |
| 116 | Nuggets vs. Timberwolves | sports_match | open | 0.58 | 0.45 | 0.600 | 0.400 | 0.60 | 0.02 | NO | +3.5% | 3-5% | $2.74 | 0.400 | open | — | — |
| 117 | Crystal Palace FC vs. West Ham United FC: O/U 3.5 | sports_prop | open | 0.32 | 0.45 | 0.295 | 0.705 | 0.29 | 0.03 | YES | +7.0% | 5-10% | $2.72 | 0.295 | open | — | — |
| 118 | Will Bitcoin reach $77,000 on April 20? | price_threshold | open | 0.37 | 0.45 | 0.350 | 0.650 | 0.35 | 0.02 | YES | +4.2% | 3-5% | $2.69 | 0.350 | open | — | — |
| 119 | Ethereum Up or Down - April 20, 2PM ET | price_threshold | open | 0.88 | 0.35 | 0.895 | 0.105 | 0.90 | 0.02 | NO | +12.8% | 10-20% | $2.67 | 0.105 | open | — | — |
| 121 | Nasdaq 100 (NDX) Up or Down on April 20? | binary_event | open | 0.10 | 0.35 | 0.080 | 0.920 | 0.08 | 0.02 | YES | +23.5% | 20%+ | $2.61 | 0.080 | open | — | — |
| 123 | Will Bitcoin reach $78,000 April 20-26? | price_threshold | open | 0.70 | 0.60 | 0.660 | 0.340 | 0.66 | 0.04 | YES | +4.6% | 3-5% | $5.09 | 0.660 | open | — | — |
| 124 | Counter-Strike: Tricked vs Johnny Speeds (BO3) - Conquest o… | sports_match | open | 0.25 | 0.40 | 0.200 | 0.800 | 0.20 | 0.05 | YES | +23.5% | 20%+ | $2.51 | 0.200 | open | — | — |
| 126 | Will CA Banfield win on 2026-04-20? | sports_match | open | 0.40 | 0.45 | 0.375 | 0.625 | 0.38 | 0.03 | YES | +5.2% | 5-10% | $2.53 | 0.375 | open | — | — |
| 128 | Dota 2: Zero Tenacity vs Power Rangers - Game 2 Winner | sports_match | open | 0.25 | 0.60 | 0.230 | 0.770 | 0.23 | 0.02 | YES | +7.2% | 5-10% | $4.99 | 0.230 | open | — | — |
| 129 | LoL: INTZ e-Sports vs Ei Nerd Esports (BO1) - Circuito Desa… | sports_match | open | 0.45 | 0.40 | 0.430 | 0.570 | 0.43 | 0.02 | YES | +3.2% | 3-5% | $2.45 | 0.430 | open | — | — |
| 130 | Will Ethereum reach $2,350 on April 20? | price_threshold | open | 0.72 | 0.60 | 0.680 | 0.320 | 0.68 | 0.04 | YES | +4.4% | 3-5% | $4.87 | 0.680 | open | — | — |
| 131 | Raptors vs. Cavaliers: O/U 221.5 | sports_prop | open | 0.55 | 0.45 | 0.525 | 0.475 | 0.53 | 0.03 | YES | +3.3% | 3-5% | $2.39 | 0.525 | open | — | — |
| 132 | Dota 2: Zero Tenacity vs Power Rangers (BO3) - DreamLeague … | sports_match | open | 0.35 | 0.45 | 0.395 | 0.605 | 0.40 | 0.05 | NO | +5.9% | 5-10% | $2.37 | 0.605 | open | — | — |
| 133 | Will FC Barcelona win on 2026-04-22? | sports_match | open | 0.78 | 0.50 | 0.795 | 0.205 | 0.80 | 0.02 | NO | +5.8% | 5-10% | $4.71 | 0.205 | open | — | — |
| 135 | Bitcoin Up or Down - April 20, 5:00PM-5:15PM ET | price_threshold | open | 0.41 | 0.40 | 0.385 | 0.615 | 0.39 | 0.02 | YES | +5.0% | 3-5% | $2.30 | 0.385 | open | — | — |
| 136 | Paris Saint-Germain FC vs. FC Nantes: O/U 3.5 | sports_prop | open | 0.58 | 0.45 | 0.520 | 0.480 | 0.52 | 0.06 | YES | +10.0% | 10-20% | $2.28 | 0.520 | open | — | — |
| 137 | Will the price of Bitcoin be between $76,000 and $78,000 on… | price_threshold | open | 0.45 | 0.40 | 0.475 | 0.525 | 0.47 | 0.02 | NO | +3.3% | 3-5% | $2.26 | 0.525 | open | — | — |
| 138 | Bitcoin Up or Down - April 20, 5PM ET | price_threshold | open | 0.20 | 0.40 | 0.185 | 0.815 | 0.18 | 0.02 | YES | +6.6% | 5-10% | $2.25 | 0.185 | open | — | — |
| 139 | Bitcoin Up or Down - April 20, 4:00PM-8:00PM ET | price_threshold | open | 0.26 | 0.45 | 0.235 | 0.765 | 0.23 | 0.03 | YES | +9.1% | 5-10% | $2.23 | 0.235 | open | — | — |
| 140 | Savannah: Hynek Barton vs Daniel Galan | sports_match | open | 0.68 | 0.45 | 0.715 | 0.285 | 0.71 | 0.03 | NO | +10.8% | 10-20% | $2.21 | 0.285 | open | — | — |
| 141 | LoL: KaBuM! Ilha das Lendas vs Team Solid (BO1) - Circuito … | sports_match | open | 0.82 | 0.60 | 0.780 | 0.220 | 0.78 | 0.04 | YES | +3.6% | 3-5% | $4.38 | 0.780 | open | — | — |
| 142 | Savannah: Kei Nishikori vs Colton Smith | sports_match | open | 0.67 | 0.45 | 0.690 | 0.310 | 0.69 | 0.02 | NO | +5.0% | 3-5% | $2.16 | 0.310 | open | — | — |
| 143 | Houston Astros vs. Cleveland Guardians | sports_match | open | 0.42 | 0.45 | 0.445 | 0.555 | 0.45 | 0.03 | NO | +3.0% | 3-5% | $2.14 | 0.555 | open | — | — |
| 145 | Philadelphia Phillies vs. Chicago Cubs | sports_match | open | 0.44 | 0.62 | 0.485 | 0.515 | 0.48 | 0.04 | NO | +7.2% | 5-10% | $4.18 | 0.515 | open | — | — |
| 147 | Will CA Osasuna win on 2026-04-21? | sports_match | open | 0.22 | 0.45 | 0.205 | 0.795 | 0.20 | 0.02 | YES | +5.8% | 5-10% | $2.10 | 0.205 | open | — | — |
| 148 | Los Angeles Dodgers vs. Colorado Rockies | sports_match | open | 0.62 | 0.45 | 0.655 | 0.345 | 0.66 | 0.04 | NO | +8.6% | 5-10% | $2.09 | 0.345 | open | — | — |
| 149 | US x Iran diplomatic meeting by April 23, 2026? | binary_event | open | 0.75 | 0.45 | 0.775 | 0.225 | 0.78 | 0.03 | NO | +9.6% | 5-10% | $2.07 | 0.225 | open | — | — |
| 151 | Timberwolves vs. Nuggets: O/U 233.5 | sports_prop | open | 0.45 | 0.40 | 0.475 | 0.525 | 0.47 | 0.02 | NO | +3.3% | 3-5% | $2.04 | 0.525 | open | — | — |
| 152 | Bitcoin Up or Down - April 20, 9:30PM-9:45PM ET | price_threshold | open | 0.32 | 0.45 | 0.295 | 0.705 | 0.29 | 0.03 | YES | +7.0% | 5-10% | $2.02 | 0.295 | open | — | — |
| 153 | Will the highest temperature in Hong Kong be 27°C on April … | binary_event | open | 0.12 | 0.60 | 0.100 | 0.900 | 0.10 | 0.02 | YES | +18.5% | 10-20% | $4.01 | 0.100 | open | — | — |
| 154 | Gwangju: Jason Jung vs Hyeon Chung | sports_match | open | 0.32 | 0.60 | 0.365 | 0.635 | 0.36 | 0.04 | NO | +5.6% | 5-10% | $3.95 | 0.635 | open | — | — |
| 155 | Dota 2: PARIVISION vs South America Rejects (BO3) - PGL Wal… | sports_match | open | 0.78 | 0.45 | 0.845 | 0.155 | 0.84 | 0.06 | NO | +40.4% | 20%+ | $2.00 | 0.155 | open | — | — |
| 156 | Gwangju: Yuta Shimizu vs Yasutaka Uchiyama | sports_match | open | 0.32 | 0.45 | 0.285 | 0.715 | 0.28 | 0.04 | YES | +10.8% | 10-20% | $2.00 | 0.285 | open | — | — |


## Section 2 — Resolved trades deep-dive (reasoning)

20 resolved trades. Wins first, then losses (biggest-magnitude first).
`research_quality_score` shown when logged; many evals predate the column.

### #109 — WIN +$13.97 · YES @ 0.165 · EV +50.0%

- **Market:** Madrid Open, Qualification: Ksenia Efremova vs Lulu Sun
- **Archetype:** sports_match · cp=0.25 · cf=0.45 · ask_yes=0.165 · Δ=0.08
- **Stake:** $2.76 · Shares: 16.73 · Outcome: YES resolved TRUE
- **Resolution quality:** 0.95

**Claude reasoning:**

> Market overweights ranking vs current form. Sun's #109 ranking and experience justify favoritism, but 83% probability ignores Efremova's excellent 2026 form (85.7% ITF win rate), clay-court comfort as French junior, and Sun's struggles (4-8 YTD, recent clay loss). Young players often upset higher-ranked opponents in qualifiers. Adjusting to 25% for Efremova better reflects the form/surface dynamics while respecting the ranking differential.

### #146 — WIN +$6.16 · NO @ 0.250 · EV +38.5%

- **Market:** Valorant: Alliance Guardians vs Pigeons (BO3) - VCL North America: Stage 2 Group Stage
- **Archetype:** sports_match · cp=0.65 · cf=0.45 · ask_yes=0.750 · Δ=0.10
- **Stake:** $2.05 · Shares: 8.22 · Outcome: NO resolved TRUE
- **Resolution quality:** 0.90

**Claude reasoning:**

> The market at 0.75 for Alliance Guardians seems too high given the context. While AG has a 2-0 head-to-head advantage from Stage 1, Pigeons shows better recent form with superior round differential (-9 vs -21) and higher regional ranking (17th vs 41st). Both teams are 2-3 in an elimination scenario, but Pigeons just upset Rise of the Scarlet Empire while AG suffered back-to-back losses to NRG Academy and QoR. The related Map 1 market at 0.99 for AG suggests extreme confidence that may be mispla…

### #78 — WIN +$5.96 · YES @ 0.385 · EV +7.6%

- **Market:** Bitcoin Up or Down - April 20, 5:30AM-5:45AM ET
- **Archetype:** price_threshold · cp=0.42 · cf=0.45 · ask_yes=0.385 · Δ=0.03
- **Stake:** $3.73 · Shares: 9.69 · Outcome: YES resolved TRUE
- **Resolution quality:** 0.95

**Claude reasoning:**

> This is a short-term (15-minute) Bitcoin price direction market with precise Chainlink resolution. The current market price of 0.39 for UP reflects the crowd's assessment that Bitcoin faces slight downward pressure. With Bitcoin down -0.1% in the last 24 hours, there's mild bearish momentum, but over such a short timeframe (15 minutes), price movements are essentially random walks. The market appears efficiently priced slightly below 50% to account for the recent negative momentum. Strategy mem…

### #125 — WIN +$5.95 · NO @ 0.455 · EV +26.0%

- **Market:** Senators vs. Hurricanes: O/U 5.5
- **Archetype:** sports_prop · cp=0.42 · cf=0.55 · ask_yes=0.545 · Δ=0.13
- **Stake:** $4.97 · Shares: 10.92 · Outcome: NO resolved TRUE
- **Resolution quality:** 1.00

**Claude reasoning:**

> The market at 55% seems too high given the strong defensive context. Game 1 was a 1-0 shutout with Ottawa managing just 22 shots despite 21 penalty minutes. Carolina's home advantage, elite goaltending from Andersen, and Ottawa's defensive injuries (Jensen, Kleven out) suggest another low-scoring affair is likely. Playoff hockey typically features tighter checking and better goaltending. While Ottawa may bounce back offensively, the 5.5 total requires 6+ goals - a significant jump from the 1-go…

### #72 — WIN +$4.91 · NO @ 0.445 · EV +22.1%

- **Market:** Counter-Strike: KUUSAMO.gg vs Fire Flux Esports - Map 2 Winner
- **Archetype:** sports_match · cp=0.45 · cf=0.40 · ask_yes=0.555 · Δ=0.11
- **Stake:** $3.94 · Shares: 8.85 · Outcome: NO resolved TRUE
- **Resolution quality:** 0.90

**Claude reasoning:**

> The 56% price for KUUSAMO.gg contradicts Fire Flux's superior recent form (70% vs 30% win rates) and status as overall match favorite. Without map-specific data to justify KUUSAMO.gg's advantage on Map 2, the fundamental team strength disparity suggests Fire Flux should be slightly favored. However, confidence is moderate due to potential unknown map-specific factors.

### #50 — WIN +$4.65 · YES @ 0.505 · EV +21.3%

- **Market:** Spread: Cavaliers (-8.5)
- **Archetype:** sports_prop · cp=0.62 · cf=0.45 · ask_yes=0.505 · Δ=0.11
- **Stake:** $4.74 · Shares: 9.39 · Outcome: YES resolved TRUE
- **Resolution quality:** 0.95

**Claude reasoning:**

> Market undervalues Cleveland's advantages from Game 1 dominance, home court, and Toronto's injury concerns. However, 8.5 points is substantial for playoffs where teams adjust. Cleveland covered by 4.5 points in Game 1 on road, so home court could provide the extra edge needed. Strategy memory shows sports_prop overconfidence issues, so moderating confidence despite seeing edge.

### #150 — LOSS -$2.05 · NO @ 0.295 · EV +39.6%

- **Market:** Bitcoin Up or Down - April 20, 8:45PM-9:00PM ET
- **Archetype:** price_threshold · cp=0.58 · cf=0.33 · ask_yes=0.705 · Δ=0.12
- **Stake:** $2.05 · Shares: 6.96 · Outcome: YES resolved TRUE
- **Resolution quality:** 0.95

**Claude reasoning:**

> This is a very short-term (15-minute) price direction bet on Bitcoin using Chainlink's BTC/USD oracle. The current market price of 0.70 implies strong bullish sentiment, but this seems overly confident given the brief timeframe. Bitcoin is up 2.2% in 24h showing positive momentum, but 15-minute moves are essentially random walks with slight upward bias from recent trend. The key insight is that such short timeframes have inherent ~50/50 odds regardless of broader trends. Oracle feeds like Chain…

### #134 — LOSS -$2.32 · NO @ 0.105 · EV +136.6%

- **Market:** Ethereum Up or Down - April 20, 4PM ET
- **Archetype:** price_threshold · cp=0.75 · cf=0.35 · ask_yes=0.895 · Δ=0.15
- **Stake:** $2.32 · Shares: 22.08 · Outcome: YES resolved TRUE
- **Resolution quality:** 1.00

**Claude reasoning:**

> Market price of 0.90 appears too high for a single-hour directional bet. While ETH shows positive momentum (+2.4% in 24h) and broader crypto strength supports bullish bias, 90% probability seems excessive for an inherently noisy 1-hour timeframe. Base rate for hourly candles closing flat/up should be near 50%, and even with positive momentum, 75% seems more reasonable. The low trading volume suggests limited price discovery, potentially inflating the confidence of the current price.

### #93 — LOSS -$3.27 · YES @ 0.135 · EV +31.8%

- **Market:** Bitcoin Up or Down - April 20, 6:45AM-7:00AM ET
- **Archetype:** price_threshold · cp=0.18 · cf=0.40 · ask_yes=0.135 · Δ=0.04
- **Stake:** $3.27 · Shares: 24.21 · Outcome: NO resolved TRUE
- **Resolution quality:** 1.00

**Claude reasoning:**

> This 15-minute Bitcoin direction market is priced at 14% Up vs 86% Down, significantly below the ~50% base rate expected for such short timeframes. While Bitcoin shows mild bearish momentum (-0.2% 24h), this seems insufficient to justify such extreme bearish positioning. However, the market may have information about expected developments during this specific timeframe that I lack. Given the precise resolution criteria and my limited context about immediate factors, I'll adjust only modestly fr…

### #86 — LOSS -$3.49 · YES @ 0.445 · EV +22.1%

- **Market:** St. Louis Cardinals vs. Miami Marlins: O/U 8.5
- **Archetype:** sports_prop · cp=0.55 · cf=0.40 · ask_yes=0.445 · Δ=0.11
- **Stake:** $3.49 · Shares: 7.83 · Outcome: NO resolved TRUE
- **Resolution quality:** 1.00

**Claude reasoning:**

> Market undervalues Over at 0.45 given Cardinals' 5-game win streak with strong offense (7 runs yesterday) facing Meyer's weaker 4.12 ERA. Both teams showed offensive capability recently. Pitching differential and Cardinals' momentum suggest higher scoring potential than crowd expects.

### #69 — LOSS -$4.04 · YES @ 0.215 · EV +61.3%

- **Market:** SPY (SPY) Up or Down on April 20?
- **Archetype:** binary_event · cp=0.35 · cf=0.40 · ask_yes=0.215 · Δ=0.13
- **Stake:** $4.04 · Shares: 18.77 · Outcome: NO resolved TRUE
- **Resolution quality:** 0.95

**Claude reasoning:**

> The market's 21% probability seems significantly underpriced versus historical base rates of SPY up days (~52-54%). While I lack current market context that might justify such bearish sentiment, this level of deviation from base rates suggests potential mispricing. The resolution criteria are precise and unambiguous. Adjusting upward from market price while acknowledging the crowd may have information I lack.

### #144 — LOSS -$4.24 · YES @ 0.315 · EV +41.4%

- **Market:** Valorant: Nightblood Gaming vs Division One (BO3) - VCL North America: Stage 2 Group Stage
- **Archetype:** sports_match · cp=0.45 · cf=0.65 · ask_yes=0.315 · Δ=0.14
- **Stake:** $4.24 · Shares: 13.47 · Outcome: NO resolved TRUE
- **Resolution quality:** 0.90

**Claude reasoning:**

> Market significantly undervalues NBG at 32% despite superior 3-2 record, +27 round differential vs D1's 2-3/-16, and better 8-6 vs 4-7 map record. While D1 has recent 2-0 win and upset potential, the season-long performance metrics strongly favor NBG. Round differential is highly predictive in tactical shooters, and the 43-round swing represents substantial skill difference. Market appears to overweight D1's recent result while undervaluing NBG's consistent performance.

### #120 — LOSS -$5.30 · NO @ 0.295 · EV +84.9%

- **Market:** Dota 2: Zero Tenacity vs Power Rangers - Game 1 Winner
- **Archetype:** sports_match · cp=0.45 · cf=0.55 · ask_yes=0.705 · Δ=0.25
- **Stake:** $5.30 · Shares: 17.97 · Outcome: YES resolved TRUE
- **Resolution quality:** 0.90

**Claude reasoning:**

> The market appears overconfident in Zero Tenacity at 70%. Current tournament form strongly favors Power Rangers who are undefeated (2-0, 4-0 maps) with clean BO3 sweeps, while Zero Tenacity shows inconsistency at 1-1 (2-3 maps) with mixed results on April 19. Though Zero Tenacity holds a 2-1 historical edge from January, recent form matters more in esports. Power Rangers' dominant 4-0 map performance suggests superior preparation and execution under the current patch. The 30% implied probabilit…

### #39 — LOSS -$5.31 · YES @ 0.240 · EV +31.8%

- **Market:** S&P 500 (SPX) Up or Down on April 20?
- **Archetype:** binary_event · cp=0.32 · cf=0.45 · ask_yes=0.240 · Δ=0.08
- **Stake:** $5.31 · Shares: 22.11 · Outcome: NO resolved TRUE
- **Resolution quality:** 0.95

**Claude reasoning:**

> Market pricing 24% chance of up day seems low versus historical base rate of ~53%. However, with no contradicting research and reasonable volume suggesting price discovery, I modestly adjust upward to 32% rather than fully reverting to base rate. The crowd may be incorporating April 2026 conditions I cannot access.

### #35 — LOSS -$5.48 · NO @ 0.185 · EV +33.6%

- **Market:** LoL: Weibo Gaming vs Oh My God (BO3) - Esports World Cup China Qualifier Phase 2
- **Archetype:** sports_match · cp=0.75 · cf=0.45 · ask_yes=0.815 · Δ=0.06
- **Stake:** $5.48 · Shares: 29.62 · Outcome: YES resolved TRUE
- **Resolution quality:** 0.95

**Claude reasoning:**

> Market at 81% for WBG seems slightly high given recent form. WBG is clearly the stronger team historically (top-6 LPL seed vs qualifier team), but their recent 0-2 losses to BLG (April 10), NIP (April 19), and Anyone's Legend (April 13) show concerning form. OMG bounced back well with a 2-0 over Team WE after losing to JDG. In high-pressure elimination matches, recent momentum can matter more than historical strength. The 75-25 split better reflects WBG's skill advantage tempered by form concer…

### #24 — LOSS -$6.23 · YES @ 0.105 · EV +136.6%

- **Market:** S&P 500 (SPX) Opens Up or Down on April 20?
- **Archetype:** binary_event · cp=0.25 · cf=0.40 · ask_yes=0.105 · Δ=0.15
- **Stake:** $6.23 · Shares: 59.37 · Outcome: NO resolved TRUE
- **Resolution quality:** 0.90

**Claude reasoning:**

> The 10% market price appears too low given historical base rates of S&P 500 overnight gaps being positive ~52-55% of the time. Without specific negative catalysts in the research context, this extreme bearish positioning seems unjustified. While single-day gap direction is inherently unpredictable, the market appears to be pricing in certainty of negative news that isn't evident from available information.

### #94 — LOSS -$6.49 · YES @ 0.585 · EV +21.6%

- **Market:** Madrid Open, Qualification: Sara Sorribes Tormo vs Tamara Korpatsch
- **Archetype:** sports_match · cp=0.72 · cf=0.60 · ask_yes=0.585 · Δ=0.14
- **Stake:** $6.49 · Shares: 11.09 · Outcome: NO resolved TRUE
- **Resolution quality:** 0.90

**Claude reasoning:**

> The market significantly undervalues Sorribes Tormo at 58%. Key factors heavily favor her: perfect 5-0 head-to-head record including recent January 2025 victory, clay court specialization matching the surface, home support in Madrid, and strong recent form with ITF W75 victories on clay. While Korpatsch holds a massive ranking advantage (99 vs 528), this appears less relevant given the specific matchup dynamics, surface expertise, and historical dominance. The H2H record is particularly compell…

### #18 — LOSS -$6.65 · YES @ 0.105 · EV +260.4%

- **Market:** Bitcoin Up or Down on April 20?
- **Archetype:** price_threshold · cp=0.38 · cf=0.45 · ask_yes=0.105 · Δ=0.28
- **Stake:** $6.65 · Shares: 63.31 · Outcome: NO resolved TRUE
- **Resolution quality:** 1.00

**Claude reasoning:**

> Market pricing 90% down probability seems excessive for 24-hour Bitcoin move. While current momentum is bearish due to geopolitical tensions, this timeframe is essentially a coin flip with slight bearish bias. Recent 2% decline may be causing anchoring bias. Base rate for random 24-hour periods should be closer to 50-50, adjusted down only moderately for current negative sentiment and news flow.

### #73 — LOSS -$7.96 · NO @ 0.265 · EV +124.9%

- **Market:** Counter-Strike: KUUSAMO.gg vs Fire Flux Esports (BO3) - European Pro League Regular Group D
- **Archetype:** sports_match · cp=0.40 · cf=0.60 · ask_yes=0.735 · Δ=0.33
- **Stake:** $7.96 · Shares: 30.03 · Outcome: YES resolved TRUE
- **Resolution quality:** 0.90

**Claude reasoning:**

> The market has KUUSAMO.gg at 73% to win, but the research strongly contradicts this. Fire Flux shows superior recent form with 70% win rate vs KUUSAMO's 30%. Fire Flux just upset Brute 2-1 and has additional wins, while KUUSAMO sits on a 2-match losing streak including 0-2 losses. The research explicitly states "Fire Flux Esports enters as the trader consensus favorite" yet the market prices KUUSAMO as heavy favorite - this appears to be a significant mispricing. In BO3 format, the more consist…

### #66 — LOSS -$8.34 · YES @ 0.175 · EV +18.5%

- **Market:** Will the price of Bitcoin be between $72,000 and $74,000 on April 20?
- **Archetype:** price_threshold · cp=0.21 · cf=0.60 · ask_yes=0.175 · Δ=0.04
- **Stake:** $8.34 · Shares: 47.63 · Outcome: NO resolved TRUE
- **Resolution quality:** 0.95

**Claude reasoning:**

> The current Bitcoin price of $74,836 sits just above the target $72k-74k range. The multi-outcome market shows strong consensus: 67% probability for $74k-76k range vs 21% for $72k-74k range. This suggests the crowd expects Bitcoin to either stay around current levels or move higher rather than drop into the target range. The resolution requires Bitcoin to be between $72k-74k at exactly 12:00 ET on a specific 1-minute candle, creating precise timing risk. Given Bitcoin's typical daily volatility…



## Section 3 — Losing trades by failure mode (14 losses)

A single trade is assigned to exactly one primary bucket by the priority:
longshot > extreme-disagreement > moderate-disagreement > low-disagreement.
Archetype rollup shown separately below.

| Failure mode | N | Avg stake | Avg EV | Total P&L | Trades |
|---|---|---|---|---|---|
| Longshot loss (p_win<0.40) | 8 | $5.20 | +88.8% | -$41.63 | #18, #24, #35, #39, #66, #69, #93, #134 |
| Moderate-disagreement loss (0.10≤Δ≤0.20) | 4 | $4.07 | +31.2% | -$16.27 | #86, #94, #144, #150 |
| Extreme-disagreement loss (Δ>0.20) | 2 | $6.63 | +104.9% | -$13.26 | #73, #120 |

### Archetype rollup (losses only)

| Archetype | N losses | Avg stake | Avg EV | Total P&L |
|---|---|---|---|---|
| price_threshold | 5 | $4.53 | +97.4% | -$22.63 |
| sports_match | 5 | $5.89 | +61.3% | -$29.47 |
| binary_event | 3 | $5.19 | +76.6% | -$15.58 |
| sports_prop | 1 | $3.49 | +22.1% | -$3.49 |


## Section 4 — Winning trades analysis (6 wins)

### Wins vs. losses — side-by-side

| Metric | Wins | Losses |
|---|---|---|
| N | 6 | 14 |
| Avg stake | $3.70 | $5.08 |
| Avg EV | +27.6% | +74.7% |
| Avg disagreement | 0.09 | 0.14 |
| Avg confidence | 0.46 | 0.47 |
| Total P&L | +$41.61 | -$71.16 |

### Wins by archetype and EV bucket

| # | Market | Archetype | cp | cf | Δ | side | EV | bucket | stake | P&L |
|---|---|---|---|---|---|---|---|---|---|---|
| 109 | Madrid Open, Qualification: Ksenia Efremova vs Lulu Sun | sports_match | 0.25 | 0.45 | 0.08 | YES | +50.0% | 20%+ | $2.76 | +$13.97 |
| 146 | Valorant: Alliance Guardians vs Pigeons (BO3) - VCL North A… | sports_match | 0.65 | 0.45 | 0.10 | NO | +38.5% | 20%+ | $2.05 | +$6.16 |
| 78 | Bitcoin Up or Down - April 20, 5:30AM-5:45AM ET | price_threshold | 0.42 | 0.45 | 0.03 | YES | +7.6% | 5-10% | $3.73 | +$5.96 |
| 125 | Senators vs. Hurricanes: O/U 5.5 | sports_prop | 0.42 | 0.55 | 0.13 | NO | +26.0% | 20%+ | $4.97 | +$5.95 |
| 72 | Counter-Strike: KUUSAMO.gg vs Fire Flux Esports - Map 2 Win… | sports_match | 0.45 | 0.40 | 0.11 | NO | +22.1% | 20%+ | $3.94 | +$4.91 |
| 50 | Spread: Cavaliers (-8.5) | sports_prop | 0.62 | 0.45 | 0.11 | YES | +21.3% | 20%+ | $4.74 | +$4.65 |

### Reasoning excerpts (wins, truncated to 300 chars)

- **#50** (sports_prop, YES): Market undervalues Cleveland's advantages from Game 1 dominance, home court, and Toronto's injury concerns. However, 8.5 points is substantial for playoffs where teams adjust. Cleveland covered by 4.5 points in Game 1 on road, so home court could provide the extra edge needed. Strategy memory shows…
- **#72** (sports_match, NO): The 56% price for KUUSAMO.gg contradicts Fire Flux's superior recent form (70% vs 30% win rates) and status as overall match favorite. Without map-specific data to justify KUUSAMO.gg's advantage on Map 2, the fundamental team strength disparity suggests Fire Flux should be slightly favored. However…
- **#78** (price_threshold, YES): This is a short-term (15-minute) Bitcoin price direction market with precise Chainlink resolution. The current market price of 0.39 for UP reflects the crowd's assessment that Bitcoin faces slight downward pressure. With Bitcoin down -0.1% in the last 24 hours, there's mild bearish momentum, but ov…
- **#109** (sports_match, YES): Market overweights ranking vs current form. Sun's #109 ranking and experience justify favoritism, but 83% probability ignores Efremova's excellent 2026 form (85.7% ITF win rate), clay-court comfort as French junior, and Sun's struggles (4-8 YTD, recent clay loss). Young players often upset higher-r…
- **#125** (sports_prop, NO): The market at 55% seems too high given the strong defensive context. Game 1 was a 1-0 shutout with Ottawa managing just 22 shots despite 21 penalty minutes. Carolina's home advantage, elite goaltending from Andersen, and Ottawa's defensive injuries (Jensen, Kleven out) suggest another low-scoring a…
- **#146** (sports_match, NO): The market at 0.75 for Alliance Guardians seems too high given the context. While AG has a 2-0 head-to-head advantage from Stage 1, Pigeons shows better recent form with superior round differential (-9 vs -21) and higher regional ranking (17th vs 41st). Both teams are 2-3 in an elimination scenario…


## Section 5 — Open positions composition (136 open / 20 resolved)

### By archetype

| Bucket | Total | Open | Resolved |
|---|---|---|---|
| sports_match | 95 | 87 | 8 |
| price_threshold | 26 | 20 | 6 |
| sports_prop | 19 | 16 | 3 |
| binary_event | 10 | 7 | 3 |
| geopolitical | 3 | 3 | 0 |
| other | 2 | 2 | 0 |
| weather | 1 | 1 | 0 |

### By EV bucket

| Bucket | Total | Open | Resolved |
|---|---|---|---|
| 3-5% | 45 | 45 | 0 |
| 5-10% | 43 | 42 | 1 |
| 20%+ | 37 | 19 | 18 |
| 10-20% | 31 | 30 | 1 |

### By disagreement

| Bucket | Total | Open | Resolved |
|---|---|---|---|
| Δ<0.05 | 116 | 113 | 3 |
| Δ 0.05–0.10 | 24 | 20 | 4 |
| Δ 0.10–0.20 | 11 | 1 | 10 |
| Δ ≥0.30 | 3 | 2 | 1 |
| Δ 0.20–0.30 | 2 | 0 | 2 |

### By time to resolution

| Bucket | Total | Open | Resolved |
|---|---|---|---|
| no date recorded | 128 | 128 | 0 |
| resolved | 20 | 0 | 20 |
| expected-past (stuck) | 4 | 4 | 0 |
| 0–1d | 4 | 4 | 0 |



## Section 6 — Side distribution

| Side | Total | Resolved | Wins | Win rate | Avg disagreement |
|---|---|---|---|---|---|
| YES | 82 | 12 | 3 | 3/12 = 25% | 0.05 |
| NO  | 74  | 8  | 3  | 3/8 = 38% | 0.06 |

**Average disagreement on resolved trades only:**
- YES resolved: 0.11
- NO  resolved: 0.16


## Section 7 — Observations (no recommendations)

- **Resolved sample is biased toward high-EV trades.** 18/20 resolved are in the 20%+ EV bucket, but only 19/136 opens are there. Low-EV trades (3–10%) are 1/20 resolved vs 87/136 open. The 20%+ bucket resolves faster because those are short-horizon markets. The 20-trade resolved sample is not representative of the strategy as a whole.
- **8 of 14 losses are longshots** (chosen side had p_win<0.40 before the trade). EV math loves these when priced cheap, but the realised hit rate is what actually funds them.
- **8 resolved trades are short-horizon BTC/ETH/SPX "Up or Down" markets**, P&L -$23.91. These are the same class PROFITABILITY_ANALYSIS.md flagged — the sizer still takes them because the prices are extreme and Claude defaults near 0.5, generating huge apparent EV on a random walk.
- **Losses are higher-disagreement than wins on average** (Δ=0.14 vs 0.09). Consistent with the anti-selection hypothesis: the more Claude disagrees with the market, the likelier the market is right.
- **`resolution_quality_score` is populated on all 156 trades.** Avg on wins 0.94 vs losses 0.95. Gap is small — resolution quality isn't discriminating outcomes in this sample.
- **Confidence is nearly identical on wins vs losses** (0.46 vs 0.47). Confidence is not distinguishing the two groups in this sample — it's not a useful lever on its own.