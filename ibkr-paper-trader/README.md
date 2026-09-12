# IBKR paper trading agent

An autonomous trading agent for an **IBKR paper account**. A weekly *strategist*
writes a numbered rule set; a daily *trader* applies those rules and nothing
else; a risk engine written in ordinary code screens every proposed order before
it reaches the broker. Everything is recorded so that after six months you have
evidence rather than an anecdote.

It cannot reach a live account. That is enforced three separate ways, described
under *Safety* below.

**[PLAYBOOK.md](PLAYBOOK.md)** is the other half of this: the gate that must be
passed before real money is connected and how that is staged, what happens in a
crash, and the pre-committed plan for buying one. Read it before you run
anything.

---

## Read this before anything else

You should assume this will lose to simply holding the benchmark.

A language model reasoning over price history has no established edge. What it
reliably produces is fluent, confident justification for more or less arbitrary
decisions. The whole design here exists to make that visible rather than to hide
it: the benchmark comparison is printed every time you run a report, the trader
must cite the rule authorising each order, and the risk engine refuses anything
outside limits you set in advance.

Three things to be honest with yourself about:

- **Paper fills flatter you.** No slippage, no queue position, no partial fills
  in a fast market. Any edge you appear to find is partly fiction.
- **The sample is tiny.** Six months of daily decisions is not a sample size.
  Any result short of about a year is noise.
- **The real risk isn't paper money.** It is that a lucky run convinces you to
  connect this to a live account. Decide *now*, in writing, what result would
  make you take that seriously — and set the bar higher than "it made money".
  The gate is written out in [PLAYBOOK.md §1](PLAYBOOK.md#1-graduation-paper-to-live):
  seven criteria, all of which must hold, including one — rule-set stability —
  that most runs will fail. Read it now, not after a good quarter.

---

## What you need

- A computer that stays on when the agent should run (this does not run in the
  cloud — IBKR requires software on your own machine).
- Python 3.11 or newer.
- **IB Gateway** (lighter than TWS, and the right choice for this). Download it
  from IBKR's site.
- A **separate paper trading username**. In IBKR Account Management go to
  Settings → Paper Trading Account and create a dedicated paper username and
  password. Use that one here. Never put your live credentials into this.
- An Anthropic API key, as `ANTHROPIC_API_KEY`.

Running cost is small: two model calls a week for the strategist and one a day
for the trader, each a few thousand tokens. Expect single-digit dollars a month.

---

## Setup

**1. Install**

```
cd ibkr-paper-trader
python -m venv .venv
.venv\Scripts\activate          # Windows.  macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

**2. Configure IB Gateway**

Log into IB Gateway with your **paper** username, then:

- Configure → Settings → API → Settings
- Tick **Enable ActiveX and Socket Clients**
- Untick **Read-Only API**
- Socket port: **4002**
- Trusted IPs: add `127.0.0.1`
- Configure → Settings → API → Precautions: tick **Bypass Order Precautions for
  API Orders** (otherwise the Gateway pops up dialogs nobody is there to click)

IB Gateway logs itself out roughly once a day. Under Configure → Settings →
Lock and Exit, set auto-restart so it comes back without you.

**3. Configure the agent**

```
copy config.example.yaml config.yaml     # macOS/Linux: cp
```

Open `config.yaml` and read it — every limit the agent operates under is in
there, and nothing is enforced that is not written there. The defaults are
deliberately conservative: long-only, eight liquid ETFs, 20% maximum in any one
holding, four orders a day, and a full stop at a 20% drawdown.

**4. Set your API key**

```
setx ANTHROPIC_API_KEY "sk-ant-..."       # Windows, then open a new terminal
export ANTHROPIC_API_KEY="sk-ant-..."     # macOS/Linux
```

**5. Check it works**

```
python run.py check
```

This connects, confirms the account is a paper account, prints your balance,
positions and today's market data, and places nothing. If this doesn't work,
nothing else will.

---

## Running it

```
python run.py check              Connect and print state. Places nothing.
python run.py strategy           Write or revise the rules. Weekly. Places nothing.
python run.py trade --dry-run    Full daily cycle, but sends no orders.
python run.py trade              Full daily cycle, sends orders.
python run.py report             Performance against buy-and-hold.
python run.py halt               Kill switch on.
python run.py resume             Kill switch off.
```

Start with `strategy`, then read `strategy/STRATEGY.md`. It is a plain-English
document — if the rules in it look vague or silly, that tells you something
before any money is at stake, even fake money. Then run `trade --dry-run` for a
few days and watch what it proposes and what the risk engine throws out.

**Scheduling it (Windows Task Scheduler, no extra software).** Two tasks:

- Daily, weekdays at 16:45 UK time (after the LSE close):
  `...\.venv\Scripts\python.exe run.py trade`, starting in the project folder.
- Weekly, Sunday morning:
  `...\.venv\Scripts\python.exe run.py strategy`

Set both to "Run whether user is logged on or not" and tick "Run task as soon as
possible after a scheduled start is missed".

---

## How it works

```
IB Gateway (paper)
      |
      |  positions, cash, 400 days of daily bars
      v
  DATA GUARD  ──►  suspect prices stop the entire run, ladder included
      |
      v
  market snapshot  ──►  returns, moving averages, realised vol, 52w position
      |
      ├─────────────────────────────►  CRASH LADDER  ──►  pure arithmetic.
      |                                      |            No model. Not blocked
      |                                      |            by the drawdown halt.
      v                                      v
  STRATEGY.md  ──►  the trader model  ──►  proposed orders, each citing a rule
                                              |
                                              v
                                        RISK ENGINE  ──► rejects anything
                                              |            outside your limits;
                                              |            the crash reserve is
                                              |            invisible to it
                                              v
                                     limit orders to IBKR
                                              |
                                              v
                                     journal.sqlite (everything)
```

**Why two models.** If one call both forms the view and places the trades, it
rewrites yesterday's thesis to justify whatever it wants to do today. That is
the standard failure mode of an LLM trader and it is invisible in the output,
because the rationale always sounds reasonable. So: the strategist runs weekly,
sees the journal and the performance record, and writes numbered rules. The
daily trader sees those rules and today's numbers, and must attach a `rule_id`
to every order. An order citing a rule that does not exist is rejected in code.
The model cannot quietly change its mind.

**Why the risk engine never reads the rationale.** It takes proposed orders and
a portfolio state and compares them against numbers from `config.yaml`. It has
no idea what the strategy is and cannot be argued with, because there is nothing
in it that reads argument. Limits are applied against a running simulation of
the batch, so cumulative caps (cash floor, position count, daily turnover) hold
across several orders, not just one at a time.

**Why limit orders only.** Market orders are never sent, paper or not. Each
order goes in as a day limit 25 basis points through the last price, and a limit
price the model supplies more than 2% away from the last price is rejected.

**Why the crash ladder is code, not a prompt.** An LLM asked "is this the
bottom?" during a crash produces articulate narrative containing no information
— and it will be *more* persuasive than usual, because crisis commentary is
abundant in its training data. So the ladder consults no model at any point. It
buys a pre-committed fraction of a ring-fenced reserve at pre-committed
benchmark drawdowns, and that is all it does. It is also deliberately **not**
blocked by the drawdown halt: a 20% NAV drawdown is roughly when the second rung
fires, so a halt that stopped it would disable the plan exactly when it was
written for. The full reasoning is in [PLAYBOOK.md §3](PLAYBOOK.md#3-capitalising-the-crash-ladder).

**Why a data guard that stops everything.** During a fast market a bad print and
a real collapse are indistinguishable at the moment they arrive. Losing one day
costs almost nothing — the ladder deploys over weeks and no daily strategy is
worth a day. Trading on a corrupt price can cost a great deal. So a suspect
snapshot stops the run, the ladder included, and asks for a human.

---

## Safety

Three independent guards, any one of which aborts the run:

1. `mode` in config must be `paper`. There is no other accepted value anywhere
   in the code.
2. The port must be 4002 or 7497 (the paper ports). 4001 and 7496 — the live
   ports — are rejected by config validation with an explicit error.
3. **After connecting, the account IBKR reports must start with `DU`.** IBKR
   paper accounts are `DU...`; live accounts are `U...`. This is the guard that
   actually matters, because unlike the first two it does not depend on what
   you typed — it depends on what IBKR says. If a misconfigured Gateway hands
   back a live account, the program stops before sending anything.

Then the layered runtime defences: the data guard, the cash floor, the
ring-fenced crash reserve the strategy cannot see, the drawdown halt, and the
`HALT` file. [PLAYBOOK.md §2](PLAYBOOK.md#2-survival-not-being-caught-out) sets
out what each one actually does — and, more usefully, what none of them can do.

On the `HALT` file: `python run.py halt` creates it and no order
is placed while it exists, regardless of what any model decides. It is checked
before the program even connects. You can also just create the file by hand.

And the drawdown stop: if NAV falls below the configured percentage from its
recorded peak, **strategy** trading halts and stays halted until you intervene.
The crash ladder keeps running — halting at the bottom is the classic way to
convert a temporary drawdown into a permanent loss.

---

## What gets recorded

`data/journal.sqlite` — open it with any SQLite viewer, or query it directly:

| Table | What's in it |
|---|---|
| `runs` | Every invocation: when, which kind, NAV at the time, whether halted |
| `snapshots` | The exact market data the model saw on each run |
| `decisions` | The model's assessment and full structured output, with token counts |
| `orders` | Every order proposed — placed, rejected (with the reason), or dry-run |
| `equity` | Daily NAV, cash, and the benchmark price, for the performance comparison |
| `strategy_versions` | Every version of the rule set, with the reasoning for each change |
| `ladder_state` | Which crash-ladder rungs have fired, and the reserve base |
| `ladder_events` | Every ladder decision: fired, held, skipped, re-armed, with the drawdown at the time |

Rejected orders are recorded as carefully as placed ones. If the risk engine is
constantly rejecting the model's proposals, that is the single most useful
signal this system produces — it means the strategy and the limits disagree, and
one of them is wrong.

`strategy/versions/` keeps every strategy the agent has ever written, so you can
see how its thinking drifted.

---

## Tests

```
python -m pytest tests/ -q
```

The risk engine and crash ladder tests are the ones that matter — they are what
makes the claims in the Safety section true rather than aspirational. If you
change a limit's behaviour, change the test first.

The ladder tests deserve particular care: that code runs perhaps twice a decade,
under conditions nobody will be calm enough to debug in. `test_tail.py` and the
crash cases in `test_session.py` cover progressive falls, sudden ones that clear
several rungs at once, choppy markets that must not re-fire a spent rung, the
cash floor holding in a crash, and — most importantly — the ladder still firing
when the drawdown halt has stopped everything else.

---

## Known limitations

- **Market data.** Paper accounts inherit your live account's subscriptions.
  With `market_data_type: 3` you get delayed data, and historical daily bars
  generally work regardless. If a symbol comes back with few bars, `check` flags
  it and the model is told how many bars it has.
- **One currency.** Everything assumes a single base currency (GBP by default)
  and a universe quoted in it. Mixed-currency universes are not handled.
- **No intraday.** Daily bars, one decision a day. This is on purpose — an LLM
  making intraday decisions is a worse idea, not a better one.
- **Fills are not chased.** Orders are day limits. If one doesn't fill, it is
  cancelled at the start of the next cycle rather than re-priced.
- **The benchmark comparison ignores dividends** on both sides, so it is
  roughly fair between the two but understates both.
- **The friction haircut is an estimate**, not measured slippage. Until real
  fills exist, the numbers in `friction:` are an assumption you are making about
  yourself. Phase 2 of the transition exists to check it.
- **The crash ladder cannot dodge a gap.** Nothing here can. If the market opens
  20% lower on overnight news, you are in it — a daily system cannot react to
  something that happened between decisions.
- **The reserve rebuild is manual**, by design. Selling back into a recovery has
  tax and timing consequences that belong to a person, not a cron job. The
  report tells you where you stand and prompts the decision.
