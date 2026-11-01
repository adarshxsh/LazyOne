# Dispute Resolution & Prestige

> Status (2026-11-01): **v2 implemented** (115 tests, SQLite + Postgres). Not locked yet - review in use, then lock like [WAVE_DISTRIBUTION.md](WAVE_DISTRIBUTION.md).
> Code: [`basic/services/disputes.py`](../../basic/services/disputes.py) · [`basic/services/prestige.py`](../../basic/services/prestige.py) · Tests: [`basic/tests/test_disputes.py`](../../basic/tests/test_disputes.py)

## Principle: prestige matters more than points

Points are the currency; **prestige is the reputation** that decides who is trusted. Most incentives and penalties land on prestige, and prestige gates jury duty, so a fresh or alt account (prestige 0) can't judge anyone.

## The flow

```
raise ── bond: max(20 % of reward, 50) ── opening offer (taker's share %)
  │
  ▼
1. SETTLEMENT (24 h)   other side: accept · counter-offer · reject
     accepted ───────────────▶ CLOSED (settled)  reward split as agreed, bond refunded, +2 prestige each
     ignored until deadline ─▶ CLOSED (default)  raiser's offer applied, bond refunded, respondent −10
     rejected / engaged ─────▶ 2
  ▼
2. JURY   size by reward: <50 → 1 · <300 → 3 · else 5   (evidence until +24 h, votes until +48 h)
     each vote: 0 (creator) · 50 (split) · 100 (taker) + required one-line reason; secret until close
     result = median vote; no quorum or an even split → MODERATOR
  ▼
3. APPEAL WINDOW (24 h)  money doesn't move yet; the side the verdict went against may appeal once:
     appeal bond = 2 × bond · fresh jury of 2n+1 (no round-1 jurors) · its median is final
     no appeal → CLOSED (jury)
  ▼
4. MODERATOR (staff)  any taker share 0-100 → CLOSED (moderator)

Exits at any time: raiser withdraws (settlement/jury stage, bond refunded) · poster approves full payment (conceded)
```

## Money

| Outcome | Reward (escrow) | Raiser bond | Appeal bond |
|---------|-----------------|-------------|-------------|
| Settled / default / withdrawn / conceded | split as agreed / opening offer / back to in-progress / taker | refunded | refunded |
| Verdict **not against** the raiser (incl. 50/50) | split by verdict | refunded | - |
| Verdict **against** the raiser | split by verdict | shared by **coherent jurors** of the deciding round (remainder → system) | - |
| Appeal **improved** the appellant's result | final verdict | as above | refunded |
| Appeal **didn't improve** it | final verdict | as above | shared by coherent appeal jurors |

All movements are ledger transfers with fixed keys (`task:<id>:settle`, `dispute:<id>:bond`, `dispute:<id>:appeal_bond`, `dispute:<id>:bond_settle`), so nothing can be paid twice.

## Prestige

| Event | Prestige |
|-------|----------|
| Task completed - taker / poster | +10 / +3 (max 3 awards per counterparty per 30 days) |
| Task abandoned | −5 |
| Poster let a submission auto-approve | −2 |
| Dispute settled between the parties | +2 each (pair-capped) |
| Dispute lost (verdict ≠ 50/50, either side) | **−20** |
| Ignored a dispute until default | −10 |
| Juror voted with the round's result | **+5** |
| Juror didn't vote | −5 and 30-day jury cooldown |

Tiers: **Newcomer** < 50 ≤ **Trusted** < 200 ≤ **Respected** < 500 ≤ **Elder**. Values are tunable via `settings.LAZYONE['PRESTIGE']`.

## Who can be a juror

- Same community; active; account ≥ 14 days; prestige ≥ 30
- Verified: phone verified, **or** the community has a campus email domain
- Not within 2 friendship hops of either party; never posted/took a task with either party
- Not a party in an open dispute; not in no-show cooldown
- Drawn with a stored seed (`jury_seed` + round) → auditable and reproducible

## Visibility

Disputes and disputed chats: the two parties, the drawn jurors (while active) and staff only. Jurors read the chat but can't post. Party prestige is **not** shown to jurors (avoids bias toward high-prestige users).

## Configuration (`settings.LAZYONE`)

`DISPUTE_SETTLEMENT_WINDOW` 24 h · `DISPUTE_EVIDENCE_WINDOW` 24 h · `DISPUTE_VOTING_WINDOW` 48 h · `DISPUTE_APPEAL_WINDOW` 24 h · `JURY_TIERS` [(50,1),(300,3),(None,5)] · `BOND_PERCENT` 20 · `BOND_MIN` 50 · `JUROR_MIN_PRESTIGE` 30 · `JUROR_MIN_ACCOUNT_AGE` 14 d · `JUROR_REQUIRE_VERIFIED` · `JUROR_NO_SHOW_COOLDOWN` 30 d · `PRESTIGE` {}.

## Why this design (research, 2026-11-01)

| System | What we took | What we avoided |
|--------|--------------|-----------------|
| **Fiverr** Resolution Center | Parties negotiate first; no response in time = accepted | - |
| **Upwork** | Escrow, then mediation, then a final decider | Weeks-long timelines |
| **Kleros** | Small first jury, larger appeal jury with higher cost; rewards for voting with the result | Pure "guess the majority" - we require a written reason |
| **eBay India Community Court** | Juror quality bar (tenure, reputation), exclude anyone who traded with the parties | Opaque process, weak evidence - we show offers, structured evidence and the task timeline |
| **League of Legends Tribunal** | - | Slow feedback, accuracy doubts - windows are 24–48 h and staff handle unclear cases |

Sources: [Kluwer Arbitration Blog on Kleros](https://arbitrationblog.kluwerarbitration.com/?p=46151) · [Proof of Humanity HIP-17](https://gov.proofofhumanity.id/t/phase-3-hip-17-reduce-number-of-kleros-jurors-drawn-in-first-rounds-of-poh-cases-to-one/969) · [Oxford Business Law Blog](https://blogs.law.ox.ac.uk/oblb/blog-post/2026/09/guessing-really-not-judging-reply-eidenmuller-and-hochgurtel) · [ChannelX: eBay India Community Court](https://channelx.world/2008/10/ebay-india-launch-community-court-for-feedback/) · [Riot: Will Tribunal return](https://nexus.leagueoflegends.com/en-gb/2018/08/will-tribunal-return/) · [Upwork dispute process](https://www.upwork.com/resources/upwork-dispute-process) · [Fiverr Resolution Center](https://help.fiverr.com/hc/en-us/articles/37552897293329-Using-the-Resolution-Center)

## Open questions before locking

1. Are 24 h settlement / 48 h voting right for campus life (exams, weekends)?
2. Should prestige also influence waves (e.g. high-prestige takers see tasks earlier)? Waves are locked v1 - would need a decision entry.
3. Should prestige decay over time so old reputation doesn't last forever?
4. Moderator selection: staff only, or "Elder" tier users as volunteer moderators?
