# Playbook

Three things this document settles, in advance, in writing:

1. **[Graduation](#1-graduation-paper-to-live)** — what must be true before any
   real money is connected, and how it is staged when it is.
2. **[Survival](#2-survival-not-being-caught-out)** — what happens in a bubble
   burst, a black swan, or an ordinary bad month, so the account is not caught
   out.
3. **[Capitalising](#3-capitalising-the-crash-ladder)** — the pre-committed plan
   for buying a severe fall, which is the only real edge available here.

Writing them down now is the whole point. Every one of these decisions becomes
harder and worse the moment it is actually needed.

---

## 1. Graduation: paper to live

### The trap this is designed to avoid

The risk is not that the paper account loses money. It is that it *makes* money
for four months, that feels like evidence, and real capital follows a result
that was noise. Six months of daily decisions is somewhere between 6 and 30
genuinely independent bets. You cannot distinguish skill from luck at that
sample size, and neither can anyone else.

So the gate below is deliberately hard to pass and deliberately hard to argue
with. If you find yourself wanting to relax a criterion, that is the criterion
doing its job.

### The gate

**All seven.** Not most. Run `python run.py report` — it prints the first four
directly.

| # | Criterion | Why |
|---|---|---|
| 1 | **250+ trading days** recorded, unbroken | A year. Below this you are reading noise. |
| 2 | **Beats buy-and-hold after friction**, by 2+ percentage points annualised | The report applies the friction haircut. Paper fills are free; real ones are not. A margin under 2pp is inside the error bars. |
| 3 | **Maximum drawdown no worse than the benchmark's** | Otherwise the excess return was bought with extra risk, not skill — and you can buy that yourself with leverage, more cheaply. |
| 4 | **Risk-engine rejection rate under 15%** | A high rate means the strategy and your limits disagree. Resolve that disagreement before, not after. |
| 5 | **Four or fewer strategy versions in the final 250 days** | See below. This is the criterion most likely to fail, and the most important. |
| 6 | **The crash ladder has been exercised** — in a real fall, or by a rehearsal you ran deliberately | Code that has never run does not work. You do not find that out during the crash. |
| 7 | **You can state the strategy's edge in one sentence, without using the word "AI"** | If you cannot say why this should work, you have a random number generator with good manners. |

### On criterion 5 — the one that will fail

If the strategist rewrites the rules every week, a 250-day record is not one
strategy tested for a year. It is 50 strategies tested for five days each, and
the track record means nothing at all — you have measured the strategist's
ability to generate plausible rules, not any rule set's ability to make money.

So count the files in `strategy/versions/`. If there are 50, the record is
void regardless of the return. What you want to see is the strategist reviewing
and *leaving the rules alone*, with `changes_from_previous` saying so.

The honest likely outcome is that the strategist converges toward something
close to a passive allocation. That is a real finding, arguably the correct one,
and it is worth the year it took to establish — but the conclusion it supports
is "buy the index", not "go live".

### If the gate passes: the staged transition

Never a switch. Five phases, and you can stop or reverse at any of them.

**Phase 0 — a second paper year at live size.** If the paper account is
£100,000 and you intend to commit £20,000, re-run paper at £20,000 first.
Position sizes, minimum order values and round-lot effects all change. A
strategy that works on £100,000 may be entirely eaten by commission on
£20,000. One quarter minimum.

**Phase 1 — shadow. Three months.** Live account funded, agent connected,
`--dry-run` only. Every day it proposes; every day you compare the proposal
against what the paper instance did. You are testing plumbing, not strategy:
does the Gateway stay up, does the scheduled task fire, does the account
resolve, does the data arrive. Expect to find two or three unglamorous bugs.
This phase has caught them at zero cost.

**Phase 2 — 10% of intended capital. Three months.** Real orders, real fills.
The question is only: **do live fills match the paper record?** Compare realised
slippage against the `friction` model in config. If real friction is materially
worse than modelled, the whole paper record was optimistic and the gate was
passed on false numbers — go back, re-run the comparison with corrected
friction, and expect the answer to change.

**Phase 3 — 33%. Six months.** The first phase where the outcome matters
financially. Monthly review against buy-and-hold. If it trails the benchmark
for two consecutive quarters, stop and return to paper. Write that trigger down
now, because you will not want to honour it later.

**Phase 4 — full allocation.** Only after Phase 3 clears, and never more than
the amount defined below.

### The amount

Decide the ceiling before the first phase, not after a good quarter.

- Never more than **10% of investable assets**, permanently. Not "to start".
- Never money with a use in under five years.
- Never money that is also the business's contingency. See the section on
  TasteHub below — this constraint is doing more work than it appears to.
- The crash reserve is *inside* this allocation, not additional to it.

### What live changes that paper does not tell you

- **Tax.** Every rebalance in a GIA is a CGT disposal. An agent that trades
  monthly generates a reporting burden the paper account never showed you. If
  this can sit inside an ISA, it should — and that caps it at £20,000 a year
  anyway, which is a useful discipline.
- **Stamp duty.** 0.5% on UK share purchases. Irish- and Luxembourg-domiciled
  ETFs (most of the universe here) are exempt, but check each one — this alone
  can exceed the strategy's entire edge.
- **Order precautions.** Live TWS/Gateway applies size and value checks the
  paper account waves through.
- **Your own nerve.** You will watch a live drawdown differently from a paper
  one. That is not weakness, it is information: if you cannot hold the position
  calmly, the allocation is too large regardless of what the backtest says.

---

## 2. Survival: not being caught out

### What actually protects the account

Not cleverness. Structure. The reason this design is hard to blow up has
nothing to do with the model:

- **No leverage.** The single largest cause of permanent loss, removed entirely.
- **No shorting.** The second largest. Losses are bounded at 100%; short losses
  are not bounded at all.
- **No derivatives.** No gamma, no assignment, no margin call at the worst
  possible moment.
- **Position caps.** Nothing above 20% of NAV, so no single instrument can
  destroy the account.
- **A cash floor.** Never fully invested.
- **Limit orders only.** No market order sent into a gap.

Everything below is secondary to that list. If you ever find yourself relaxing
one of those six, you have changed the risk profile far more than any strategy
change could.

### What you cannot protect against, and should stop trying to

**You cannot dodge a gap.** If the market opens 20% lower on news that broke
overnight, you are in it. No stop-loss helps — stops become market orders at
the gap price, which is how people turn a 20% loss into a 35% one. A daily-bar
system that decides once a day cannot react to something that happened between
decisions. Accept this explicitly rather than buying false comfort.

**Correlations go to 1.** The default universe looks diversified — US, world,
UK, Europe, EM, bonds, gilts, gold. In March 2020 and September 2022 nearly all
of it fell together. Diversification is protection against ordinary volatility,
not against a systemic event. Do not let the eight tickers convince you
otherwise.

**The model will sound most confident when it is most wrong.** In a regime it
has no analogue for, an LLM produces its most fluent output, because fluency is
what it optimises. Do not read conviction in the rationale as information.

### The layered defences, and what each actually does

| Layer | Trigger | Effect | What it is genuinely for |
|---|---|---|---|
| Data guard | Implausible move, stale bars, price far from the 5-day median | Stops the entire run, ladder included | A bad print during a fast market is indistinguishable from a collapse. Stop and fetch a human. |
| Cash floor | Always | Buy refused | Never fully invested. |
| Reserve | Always, while rungs are unfired | Strategy cannot spend it | Ammunition for §3, protected from the model. |
| Drawdown halt | NAV 20% below recorded peak | **Strategy** stops. Ladder continues. | Stops narrative-driven trading when something has gone wrong. |
| `HALT` file | You create it | Everything stops, checked before connecting | Your hand on the switch. |

### The drawdown halt is a circuit breaker, not protection

Worth being clear about, because it is easy to misread. Halting at −20% does
not prevent the −20%; it has already happened. What it prevents is the model
making it worse while you are not looking.

It also carries a real danger: **a halt that stops everything locks you out of
the recovery.** Halting at the bottom is the classic route from a temporary
drawdown to a permanent loss. That is exactly why the crash ladder is *not*
gated by it — see §3.

### Manual procedures

Automation stops where judgement starts. These are yours.

**If the halt fires.** Do not simply resume. Work through: was it a market fall
or a strategy failure? Compare against the benchmark's drawdown over the same
window — if the benchmark fell as far, this is the market, and the strategy did
not misbehave. Read the last ten decisions in the journal. Then either resume
deliberately, or reduce the allocation, or stop. Resuming without doing this
converts a circuit breaker into a speed bump.

**If the data guard fires.** Open IBKR and look at the actual chart. Almost
always it is a stale feed or a corporate action (a share split will look exactly
like a 50% collapse). Fix or wait. Never widen `max_daily_move_pct` to make the
error go away — that is disabling the smoke alarm.

**If IB Gateway is down during a crisis.** It will be. Everyone hits IBKR at
once during a crash and connections fail. This is why the ladder deploys over
days rather than in one shot, and why unfilled tranches carry forward: a missed
day costs you very little. If it persists, place the ladder order by hand in
the IBKR app — the tranche sizes are in `config.yaml`, they are simple
arithmetic, and you do not need the software to execute your own plan.

**Quarterly, 20 minutes.** Read the current `STRATEGY.md`. Count the files in
`strategy/versions/`. Run `report`. Check the reserve is intact and the rungs
are armed. Confirm the risk limits still match what you would accept today.

---

## 3. Capitalising: the crash ladder

### The claim

For an unlevered investor with a genuinely long horizon, buying a severe market
fall on a pre-committed schedule is close to the only durable edge available.
Not because you can pick the bottom — you cannot, and the plan does not try —
but because you are being paid to supply liquidity when almost everyone else is
forced to withdraw it. The return does not come from insight. It comes from
having pre-committed to act when acting is psychologically hardest.

Every part of the design follows from that.

### Design

**The trigger is the benchmark's drawdown from its own 252-day high** — not the
portfolio's. You are buying because the *market* fell, not because *you* did
badly. Those come apart, and confusing them is how a bad strategy talks itself
into doubling down.

**The rungs, in `config.yaml`:**

| Benchmark drawdown | Deploys | Of a 25% reserve on a £100k account |
|---|---|---|
| −15% | 20% of reserve | £5,000 |
| −25% | 25% | £6,250 |
| −35% | 30% | £7,500 |
| −50% | 25% | £6,250 |

Deeper falls buy more, because they are rarer and the expected return from
deploying into them is higher. Nothing here is optimised — optimising it would
be fitting to a handful of historical events, which is worse than useless. It is
a shape you can defend in advance, which is the only property that matters.

**No model is consulted. Anywhere in this path.** This is the single most
important constraint in the system. An LLM asked "is this the bottom?" during a
crash will produce articulate, well-structured narrative containing no
information. Worse, it will be *more* persuasive than usual, because crisis
commentary is abundant in training data. The ladder is pure arithmetic in
`trader/tail.py`, and it stays that way.

**It is not blocked by the drawdown halt.** A 20% NAV drawdown is roughly when
the second rung fires. A halt that stopped the ladder would disable the plan at
the exact moment it was written for. The halt stops *narrative*; the ladder is
the opposite of narrative. This is asserted in the tests, because it is the
property most likely to be broken by a well-meaning future change.

**The cash floor still holds.** Even in a crash, the account is never spent to
zero. A tranche the floor blocks carries forward rather than being abandoned.

**Limits are set wide — 150bps through last, and a 60% position cap.** In a
real fall spreads blow out and a 25bps limit simply will not fill. And
concentrating into the broad market is the intent, not an accident.

### The cost, stated honestly

A 25% reserve drags returns in every year nothing happens — which is most
years. On a ~7% real return, holding a quarter in cash costs roughly 1.5–1.75
percentage points annually. Over a decade with one serious crash, the ladder
probably roughly breaks even against just staying invested. Over a decade with
none, it loses.

**That is the premium.** You are not buying free return, you are buying the
ability to act when it counts, and paying an insurance premium for it every
quiet year. If you are not willing to pay that premium for ten years running,
set `reserve_pct: 0` now rather than abandoning the reserve in year three —
which is what people actually do, invariably just before it would have paid.

### What the ladder is not

- Not market timing. It does not predict; it responds to a fall that already
  happened.
- Not a trading strategy. It fires perhaps twice a decade.
- Not a bottom-picker. Rung one at −15% will frequently be under water. That is
  expected and is not a signal to intervene.
- Not automatic on the way back out. See below.

### Coming back out

The reserve does not refill itself, deliberately. Selling back into a recovery
has tax and timing consequences that belong to a person.

The report tells you where you stand. The rule to follow:

- **While the benchmark is more than 5% below its high:** hold. Do not rebuild
  into a falling market. This is the decision you will most want to override,
  usually about two weeks before the low.
- **Once the benchmark is within 5% of its high:** the rungs re-arm
  automatically, but rebuilding the reserve is your decision. Sell back toward
  the target over three or four tranches across a quarter, not in one go.
- **If a full year passes with no rebuild:** either rebuild or lower
  `reserve_pct` to what you actually intend to hold. A reserve you have quietly
  stopped maintaining is worse than no reserve, because you are still counting
  on it.

### Rehearse it

Criterion 6 of the gate. Code that has never run does not work, and you will
not debug it during a crash.

Twice a year, with the paper account: temporarily set the first rung to
`drawdown_pct: 1.0`, run `trade --dry-run`, confirm the ladder proposes the
order you expect at the size you expect, then restore the config. Note the date.
Ten minutes, and it is the difference between a plan and a paragraph.

---

## A note on TasteHub

The plan above concerns a brokerage account. The actual exposure is wider, and
the pieces are correlated in a way worth seeing before a crisis rather than
during one.

A severe recession does not hit one thing. It hits the portfolio, it hits UK
on-trade wine demand, and it hits LWC — which is effectively all of TasteHub's
revenue. Those arrive together, because they have the same cause. A tail-risk
plan that covers only the brokerage account is solving the smaller problem.

Two specific consequences:

**The crash reserve and the business contingency compete for the same cash,
in the same scenario.** The month the ladder wants to deploy is plausibly the
month receivables slow and you would most want liquidity in the business. If
the reserve is money the business might need, the ladder will not fire — you
will override it, correctly, and the plan was fiction. So: the reserve must be
money that is genuinely surplus to twelve months of business contingency, or
`reserve_pct` should be smaller. Decide which, explicitly.

**Revenue concentration is the larger position.** Whatever is allocated here,
single-customer dependency on LWC is a bigger and less diversifiable exposure
than anything in this portfolio. The honest ranking of effort is: diversifying
that first, this second. An afternoon spent on a second UK wholesale
relationship is worth more, in expectation, than a year of this system working
perfectly.

Which is not an argument against building it. It is an argument for knowing
which one is the hobby.
