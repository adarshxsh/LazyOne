# Dispute Resolution & Prestige

> Status (2026-11-01): **v2 implemented** (117 tests, SQLite + Postgres). Open questions answered; ready to lock like [WAVE_DISTRIBUTION.md](WAVE_DISTRIBUTION.md).
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

## Decisions on the open questions (2026-11-01)

| Question | Decision |
|----------|----------|
| Time windows (24 h settle, 48 h vote) | Keep the defaults; all are settings, so tune them after real use |
| Prestige influencing waves | **Yes, only because it costs nothing**: head start on the community wave ([D15](../Development/DECISIONS.md), [WAVE_DISTRIBUTION §3a](WAVE_DISTRIBUTION.md#3a-prestige-head-start-v11-decision-d15)) |
| Prestige decay | Not now; revisit with real data |
| Moderators | Staff only for now; Elder volunteer moderators later |

## Next iteration: AI assistance (planned, not built)

**Rule ([D16](../Development/DECISIONS.md)): AI assists, never decides.** Points and prestige move only because a person chose it: the parties accept an offer, jurors vote, or staff resolve. The AI's text is shown to everyone in the dispute, so nothing is hidden.

| Where | What the AI does | Why it helps |
|-------|------------------|--------------|
| Settlement | **Mediator suggestion:** reads the task, timeline and chat, then suggests a split (e.g. "taker 70 %") with a two-line reason. Either side can make it their offer with one tap | Most disputes should end here, with no jury |
| Jury | **Neutral brief:** timeline, what each side claims, and the evidence, citing message times. Parties appear as "Creator" and "Taker", with no names or prestige | Jurors decide faster and on the same facts |
| Votes | **Reason check:** flags empty reasons ("ok", "asdf") so the juror rewrites them | Keeps the written-reason rule meaningful |
| Moderator | **Triage:** marks abusive messages, possible alt accounts and disputes the jury couldn't decide, most urgent first | Staff time goes to the hard cases |

### Guardrails
- Runs as an **outbox job** after `dispute.opened` / `dispute.jury_drawn`, never inside a web request, so users wait for nothing and an AI outage can't block a dispute.
- Evidence and chat are **untrusted text**: the prompt treats them as data, and the output is a fixed schema (structured outputs), not free text.
- Kill switch `AI_ASSIST_ENABLED`; if it's off or the call fails, the dispute works exactly as today.
- Every brief is stored (model, prompt version, output) for audit and appeals.
- Jurors also see the raw evidence, not just the summary.

### Cost estimate
One call reads about 8,000 tokens (task, chat, evidence) and writes about 800. A dispute needs about 3 calls (mediator, jury brief, appeal brief), so 100 disputes a month is about 300 calls. Prices are per million tokens (input / output), from public price lists in late Oct 2026; sources disagree on some models, so check the providers' pricing pages before choosing.

| Provider | Model (tier) | Price in / out | Per call | 100 disputes / month |
|----------|--------------|----------------|----------|----------------------|
| Anthropic | Claude Haiku 5.5 (budget) | $0.10 / $0.50 | ~$0.001 | ~$0.36 |
| Anthropic | Claude Sonnet 5.5 (mid) | $2 / $10 | ~$0.024 | ~$7 |
| Anthropic | Claude Opus 5.5 (top) | $4 / $20 | ~$0.048 | ~$14 |
| OpenAI | GPT-6 Luna (budget) | $0.10 / $0.50 | ~$0.001 | ~$0.36 |
| OpenAI | GPT-6.1 Terra (mid) | $1 / $5 | ~$0.012 | ~$3.60 |
| OpenAI | GPT-6.1 Sol (mid-high) | $2 / $10 | ~$0.024 | ~$7 |
| OpenAI | GPT-6 Astra (top) | $10 / $50 | ~$0.12 | ~$36 |
| Google | Gemini 2.5 Flash-Lite (budget) | $0.10 / $0.40 | ~$0.001 | ~$0.34 |
| Google | Gemini 3.8 Flash (mid) | $0.75 / $3.75 (intro; $1.50 / $7.50 from Jan 2027) | ~$0.009 | ~$2.70 (then ~$5.40) |
| Google | Gemini 3.1 Pro (top) | $2 / $12 | ~$0.026 | ~$7.70 |

Notes:
- **Thinking tokens are billed as output** on all three providers. A model that reasons at length can write 2–5× the 800 output tokens assumed here, so mid- and top-tier costs may be 2–3× higher in practice. Measure with real disputes.
- All three offer **batch processing at about half price** and **cached input at about 90 % off**. That applies to jury briefs, which aren't urgent.
- **Choose on quality, not price:** even the top tier costs less than a meal a month at this volume. `ai_assist` should hide the provider behind one interface, so switching is a setting.

Sources: [Claude pricing (claude-api reference, Oct 2026)](https://platform.claude.com/docs/en/about-claude/pricing) · [CloudZero: OpenAI pricing](https://www.cloudzero.com/blog/openai-pricing/) · [OpenAI ChatGPT rate card](https://help.openai.com/en/articles/20001415-chatgpt-rate-card-enterprise-token-based-pricing) · [Puter: OpenAI API pricing](https://developer.puter.com/tutorials/openai-api-pricing/) · [MorphLLM: Gemini API pricing](https://www.morphllm.com/gemini-api-pricing) · [CloudZero: Gemini pricing](https://www.cloudzero.com/blog/gemini-pricing/) · [MaxiMem: Gemini rate changes](https://www.maximem.ai/blog/gemini-api-pricing-2026)

With 1,000 users, 100 disputes a month is a generous guess. Plan: build behind the kill switch, compare models from each provider on past disputes, then choose.
