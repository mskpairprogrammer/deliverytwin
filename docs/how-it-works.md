# How Delivery Twin works

The AI reads, asks and explains; code computes every figure a decision rests on. This page walks the demo request,
*"We need one view of every customer across the business"*, through the four stages with the formulas from the code.
All data is synthetic.

The figures are the dashboard with no AI key, run as of **2026-10-20**. The target date is fixed (2027-03-31), so the
runway, and with it the confidence figures, moves by a fraction of a point each day.

| Stage | What code computes | Where |
|---|---|---|
| SHAPE | Similar projects, effort range (P50 / P80 / P90) | `intake.py` |
| COMMIT | Required FTE, chance of fitting capacity, on-time confidence | `intake.py` |
| TRACK | Forecast from delivery signals, and why it moved | `workflow.py`, `track_ai.py` |
| INTERVENE | Recovery options, their on-time odds and cost | `portfolio.py`, `recovery.py` |

## 1. SHAPE: from request to estimate

**Fingerprint.** The request and the four clarifying answers (phased cutover, event sync, PII and consent controls,
reuse MDM) become a six-part fingerprint: structure, complexity, systems, roles, dependencies and delivery pattern.

**Closest analogs.** Each completed project scores the mean of six dimension scores: 1 or 0 for the same structure
and delivery pattern, 1 − gap ÷ 2 for complexity (low / medium / high), and the Jaccard overlap (shared ÷ either) of
systems, roles and dependencies. With documents attached, 25% of the score comes from words they share with the project.

| Analog | Structure | Complexity | Systems | Roles | Dependencies | Pattern | Similarity |
|---|---|---|---|---|---|---|---|
| HIST-008 Customer Data Lakehouse | 1 | 1 | 0.89 | 0.80 | 1 | 1 | **95%** |
| HIST-010 Customer Segmentation Feed | 1 | 1 | 0.62 | 1 | 1 | 1 | **94%** |
| HIST-003 Customer Identity Resolution | 1 | 1 | 0.75 | 0.80 | 1 | 1 | **92%** |

**Analog ROM.** Median base effort of the three, plus their median integration buffer (19%), rounded to 50 h:
P50 3,902 h × 1.19 = **4,650 h**, P80 4,837 h × 1.19 = **5,750 h**.

**Forecast (the proposed baseline).** A wider set of similar projects (scored 40% shared technologies, 15%
integration count, 15% team size, 10% delivery model, 20% wording) is fitted as a lognormal of their *actual* effort,
weighted by similarity squared:

```
μ = Σ w·ln(actual hours) ÷ Σ w        σ = weighted spread of ln(actual hours)
P50 = e^μ      P80 = e^(μ + 0.842σ)      P90 = e^(μ + 1.282σ)
```

Demo: **P50 5,600 h, P80 6,150 h, P90 6,500 h**. With an AI model configured, the AI forecast replaces it, but only
after validation; a rejected forecast falls back to this one and says so.

## 2. COMMIT: is the date defensible?

| Figure | Formula | Demo |
|---|---|---|
| Runway | weeks from today to the target date | 23.1 weeks to 2027-03-31 |
| Capacity | allocated FTE × weeks × 40 h | 7.0 FTE → 6,480 h |
| Required FTE | P80 ÷ (weeks × 40 h) | 6.6 FTE |
| Fits capacity | lognormal through P50 / P80 / P90: P(effort ≤ capacity) | 90% |
| Historical | median on-time confidence of the three analogs | 69% |
| **On-time confidence** | 75% × fits capacity + 25% × historical | **84.8%** |

The commitment is **feasible** only when required FTE ≤ allocated FTE; an infeasible one cannot be approved. The AI
challenge reviews the plan before approval but cannot call an infeasible plan ready.

**Approval** stores the baseline and records who approved it in `dt_approvals`: on Azure App Service the signed-in
Entra ID user, locally "Local user (no sign-in)".

## 3. TRACK: what moved the forecast

Each sprint, the forecast is the approved baseline confidence minus a penalty per signal, kept between 5% and 95%:

| Signal | Source | Points lost per unit |
|---|---|---|
| Lead's production support | UKG Pro WFM | 0.8 per h/week |
| Unplanned scope | Azure DevOps | 0.2 per story point |
| Blocking change open | ServiceNow | 0.6 per day |
| Approved time off | UKG Pro WFM | 0.1 per hour |
| Priority-1 incidents | ServiceNow | 2 per open incident |
| P0 portfolio preemption | Planview | 0.12 per preempted hour |

Projected slip = (100 − on-time %) × 0.45 days. The variance view splits the drop across these same signals, so
the explanation always adds up to the forecast.

Demo, after two healthy sprints: 2 h/week support (−1.6) and 4 unplanned points (−0.8) take the 84.8% baseline to
82.4, shown as **82%**, with 8 projected slip days.

**P0 preemption.** A P0 regulatory project takes 2.5 FTE in Sprints 3 and 4: 2.5 × 2 sprints × 80 h = 400 h.
82.4 − 0.12 × 400 = **34%**.

## 4. INTERVENE: recovery options

Under a P0, the options win back the 400 preempted hours with three levers, and code scores each one:

```
on-time % = forecast without the P0 − 0.12 × hours still lost − 0.02 × hours deferred to the next PI
cost      = backfill FTE × 160 h × $150 (mock blended rate)
```

Borrowed FTE come from the P3 project PRJ-024 at 160 h each; contractor backfill is assumed 80% productive.

| Option | Levers | Hours still lost | On-time | Cost |
|---|---|---|---|---|
| **Composed re-plan** (recommended) | defer 120 h, borrow 1.5 FTE, backfill 0.5 FTE | 0 | 82.4 − 0.02 × 120 = **80%** | **+$12,000** |
| Re-sequence sprint backlog | defer 400 h | 0 | 82.4 − 0.02 × 400 = 74% | $0 |
| Cross-portfolio reallocation | borrow 2.0 FTE (320 h) | 80 | 82.4 − 0.12 × 80 = 73% | $0 |
| Backfill preempted FTE | backfill 2.5 FTE (320 h effective) | 80 | 73% | +$60,000 |

Options rank by on-time %, then lower cost, then earlier finish. A person applies one; the choice and its approver
are recorded in `dt_approvals`.

**Tune.** *"No extra budget"* lets the AI propose new levers; code scores each proposal and feeds the score back.
Result: borrow 2.0 FTE, defer 80 h, no contractors → 82.4 − 0.02 × 80 = **81% at $0**. Without a usable AI, a
deterministic search finds the cheapest lever mix that meets the target.

**Without a P0**, a what-if moves FTE, the target date, deferred scope and budget. Code rates it with a
capacity-versus-load model: capacity ratio = (FTE ÷ current FTE) × (days to new date ÷ days to current date) ÷
remaining load (each deferred story point removes 0.5%), moved on the log-odds scale with sensitivity 3. Here the AI
may also predict the on-time %; the dashboard shows that prediction with the model's figure next to it as a check.

## The AI checks

| AI step | Check | If it fails |
|---|---|---|
| Clarifying questions | Allowed values only; suggestions may cite only projects the model was given | Standard questions |
| Effort forecast | P50 ≤ P80 ≤ P90, within half the smallest and twice the largest similar actual | Statistical forecast |
| Source quotes | Each quote must appear word for word in the request or the named document | Relabeled "AI inference", with the reason |
| COMMIT challenge | Verdict allowed (never "ready" when infeasible); **number check** | Rules-based review |
| TRACK briefing | Explains every driver; **number check** | Computed briefing |
| INTERVENE scenarios and predictions | Levers within capacity, date, scope and budget limits; **number check** | Scenario dropped, or rules-based options |

**Number check:** every number the AI writes must appear in the figures it was given, as given, whole or to one
decimal. Dates, list lengths and the size of a change also count, so "down 50 points" or "slip grew by 16 days"
pass. A made-up figure turns the answer into a labeled fallback. Each fallback says why, so a rules-based result is
never presented as AI.
