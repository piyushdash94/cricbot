# IPL question-answer evaluation

How well does Pandit answer stats questions? Two question sets, one runner.

| File | Questions | Purpose |
|---|---|---|
| `qna_ipl.jsonl` | 130 | Development set. The planner rules and prompt were improved against it, so scores on it are optimistic. |
| `qna_ipl_heldout.jsonl` | 43 | Held-out set: new wording ("maximums", "scalps", "lifted the trophy"), new players and teams. Written after the rules were tuned and never used to tune them. **This is the honest score.** |

Both are built from public records and the archive by `build_qna.py`:

```bash
python -m backend.eval.build_qna          # rebuilds both files from data/cricbot.duckdb
```

## What a question looks like

```json
{"id": "orange-2016", "question": "Who scored the most runs in IPL 2016?", "category": "season_leader",
 "expected_route": "stats", "expected_tool": "batting_leaders", "expected_args": {"year": "2016"},
 "checks": [["Kohli"], ["973"]], "verified": "published"}
```

`checks` are groups of acceptable strings: the reply must contain one string from every
group (numbers must match as whole numbers, so 973 does not match 9736).

`verified` says where the gold answer comes from:

- **published** (82 questions): typed in from public records: every Orange and Purple Cap
  winner, champion and runner-up from 2008 to 2025, all-time records, career totals to 2025.
  The build checks each against the archive and stops if they disagree. All of them agree,
  so these questions also test the data.
- **archive** (73): read from the archive with the same verified queries Pandit uses (team
  seasons, head to head, matchups, venues, phases, career totals including 2026). They test
  that Pandit picks the right query and reports it faithfully, not the data itself.
- **n/a** (18): single-match questions that must stay on the match path, and ambiguous names
  ("Sharma", "Patel") where the right answer is a question back.

## Running it

```bash
python -m backend.eval.run_qna --planner rules                       # keyword rules, no LLM (seconds)
python -m backend.eval.run_qna --planner llm                         # LLM tool calling only
python -m backend.eval.run_qna --planner hybrid --answer             # the app: LLM plan, rules fallback, LLM answers
python -m backend.eval.run_qna --dataset backend/eval/qna_ipl_heldout.jsonl --planner hybrid --answer
python -m backend.eval.run_qna --planner hybrid --answer --every 3   # a third of the set, quicker
```

Each question is scored on **route** (stats vs match path), **tool** (the expected query was
planned), **args** (year, team, sort, phase, role as expected) and **answer** (the checks).
Without `--answer` the answer is Pandit's no-LLM summary of the same tables. Groq rate limits
are waited out rather than falling back to OpenRouter, so results describe the configured
models. Results are written to `data/eval_qna_<planner>.json`.

## Results

Measured on 17 Oct 2026 with Groq (`gpt-oss-20b` planning, `gpt-oss-120b` writing answers).
"Answer" counts the stats questions whose reply passed every check; "route" counts all questions.

**Held-out set (43 questions; the honest score)**

| Planner | Answer written by | Route | Answer |
|---|---|---|---|
| Keyword rules | summary of the tables (no LLM) | 36/43 (84%) | 30/38 (79%) |
| LLM tool calling only | summary of the tables (no LLM) | 39/43 (91%) | 32/36 (89%)* |
| **Hybrid: LLM plan, rules fallback (the app)** | **LLM, under the grounding check** | **43/43 (100%)** | **35/38 (92%)** |

All 14 published answers on the held-out set were right end to end.

\* Four questions in the LLM-only run hit Groq's rate limit and fell back to no plan; they are left
out (two were stats questions, two single-match questions that passed only by default). Counting
them as wrong gives 32/38 (84%).

**Development set (130 questions; optimistic, because it was used for tuning)**

| Planner | Answer written by | Route | Answer |
|---|---|---|---|
| Keyword rules, first version | summary | 114/130 (88%) | 94/120 (78%) |
| Keyword rules after tuning on this set | summary | 130/130 | 120/120 (100%), overfit |
| LLM tool calling only | summary | 129/130 (99%) | 114/120 (95%) |

### What the held-out run caught, and what changed after it

Three of the 43 answers were wrong:

1. "chahal wickets total": the planner asked for **Jasprit Bumrah**, copying an example in its own
   prompt, and the reply said "Jasprit Chahal ... 187 wickets". The number really was in the table,
   so the numeric grounding check passed. Wrong player, right-looking answer.
2. "pollard against narine": the planner invented "Chris Pollard v Shakib Al Hasan, 2024".
3. "who's hit the most maximums": right plan, but the answer said the tables have no "maximums".

Fixes: plans are now **grounded in the question** too (a player the planner names must be named
in the question, and a year must be one the question gives, or it is replaced or dropped); a short
cricket glossary went into the answer prompt; a wrong first name with a clear surname resolves to
the surname ("Chris Pollard" is Kieron Pollard). Because the held-out set prompted these fixes, a
re-run on it is no longer a clean test. A fresh set of new questions would be.

### Cost

A full `--answer` run is about 200,000 Groq tokens: one free-tier model's daily allowance
(200k tokens a day per model, 8k a minute). Use `--every 3` for a quick check, and do not run
evaluations on the key the app is using on a day you want to demo it. When one model's
allowance runs out, Pandit moves to the next model in `GROQ_PLAN_MODEL` / `GROQ_MODEL`
(e.g. `qwen/qwen3.8-27b`), so results from a long run can mix models.

## Reading the numbers

- **Keyword rules overfit.** They are perfect on the set they were tuned on and much weaker on
  new wording. That gap is the reason the LLM planner exists.
- **The LLM planner generalises** to slang and new phrasing, but it makes its own mistakes, such as
  ignoring a year range or ranking strike rates with no minimum balls. Validation (year format,
  team codes, a minimum sample for rate stats) and the rule fallback catch some of them.
- **Answers stay grounded.** A reply with a number not in the tables is replaced by a summary
  built from the tables, so a wrong answer comes from a wrong plan, not from invented figures.
- The sets are small (130 + 43), so one question moves a category by several points. Treat
  differences of a few percent as noise.

## v2: robustness set (88 questions)

`qna_ipl_v2.jsonl`, built by `build_qna_v2.py`, tests how Pandit behaves rather than what it
covers:

| Part | Questions | Right behaviour |
|---|---|---|
| Paraphrase groups | 20 groups × 3 | Same correct answer whatever the wording, including order traps ("Bumrah vs Kohli", "Rashid Khan bowling to Dhoni") |
| Shared surnames | 8 | Ask which player ("Sharma", "Pandya", "Iyer") |
| Clear short names | 8 | Answer without asking ("Rashid", "Hardik", "Rohit's runs") |
| Out of scope | 12 | Say it is outside the IPL archive (WPL, Tests, T20Is, BBL, PSL, auctions, predictions, age, live scores, football) |

Metrics: accuracy per wording; groups right all three ways; groups with a consistent outcome;
groups with an identical plan; **entity accuracy** (the plan used exactly the expected players,
teams and years, which would have caught Chahal → Bumrah); clarify rate; needless-question rate;
refusal rate, and how many out-of-scope questions ran IPL queries anyway.

```bash
python -m backend.eval.run_qna --dataset backend/eval/qna_ipl_v2.jsonl --planner rules            # the app without LLM keys
python -m backend.eval.run_qna --dataset backend/eval/qna_ipl_v2.jsonl --planner hybrid           # LLM plans, tables answer (~130k tokens)
python -m backend.eval.run_qna --dataset backend/eval/qna_ipl_v2.jsonl --planner hybrid --answer  # the full app
python -m backend.eval.build_qna_v2 --heldout                                                    # rebuild the v2 held-out set
```

Runs save after every question, so a stopped run keeps what it finished.

### Baseline, 17 Oct 2026 (the app without LLM keys)

| Metric | Score |
|---|---|
| Paraphrase questions answered correctly | 38/60 (63%) |
| Groups right all three ways | 6/20 |
| Groups with a consistent outcome | 7/20 |
| Groups with the same plan | 6/20 |
| Entity accuracy | 37/56 (66%) |
| Asks when the name is shared | 8/8 |
| Answers when the name is clear | 3/8 |
| Refuses out-of-scope questions | 4/12, all by the generic "couldn't find" reply |
| Out-of-scope questions answered with IPL numbers | 5 |

With the LLM planner (gpt-oss-20b), the first 45 questions (15 groups) before Groq's daily token
limit stopped the run: 36/45 correct (rules: 31/45), 8/15 groups right all three ways (rules: 5/15).

What it found:

- **Out-of-scope questions get IPL answers.** "Most runs in WPL 2024" returns IPL 2024's top
  scorer, "Kohli's Test average" returns his IPL average, "ISL titles" returns IPL titles. Nothing
  checks scope before planning.
- **First names and short names are not recognised in free text** ("Hardik", "Rohit", "Bhuvneshwar",
  "Suryakumar"), and **"Rashid" resolves to AU Rashid** (Adil Rashid, 3 IPL innings) instead of
  Rashid Khan: only full names and surnames are indexed.
- **Word order flips batter and bowler**: "Bumrah vs Kohli" is planned as Bumrah batting.
- **The LLM misreads some wordings**: "maximums" planned as highest score, "MS Dhoni vs Rashid
  Khan" as two profiles instead of the matchup, a 2024 death-overs question without the year.
- A rule-fallback bug, fixed: a venue's `name` argument was treated as a player name, so the
  rules' venue plan overrode a correct LLM head-to-head plan for "Chennai against Mumbai".

Completed baseline with the LLM planner (the remaining 43 questions ran on 18 Oct on the same
pre-fix code): **61/88**, against 53/88 for rules.

### After the v2 fixes, 18 Oct 2026

What changed, one fix per finding:

| Finding | Fix |
|---|---|
| Out-of-scope questions get IPL answers | `stats.out_of_scope` runs before planning: WPL/women's, Tests/ODIs/T20Is/World Cups, other leagues (BBL, PSL, CPL, SA20, CLT20...), auctions and pay, predictions and future seasons, personal details, live scores, other sports. The reply says what Pandit covers, with IPL questions to try, and runs no queries. |
| First names not recognised; "Rashid" = AU Rashid | The player index also keys full first names ("rashid", "hardik", "suryakumar") and a short list of fan nicknames ("SKY", "Bhuvi", "ABD", "Jaddu"). The usual 3x-innings dominance rule decides; a shared first name ("Chris") is skipped rather than asked about, a shared surname still asks. |
| Word order flips batter and bowler | `orient_matchups`: "X bowling to Y" and "X facing Y" decide when the question says; otherwise the player who bats more than he bowls is the batter. |
| LLM misreads | Planner prompt: "Bumrah vs Kohli -> batter Kohli", maximums = sixes, scalps = wickets, first six overs = powerplay, overs 16-20 = death, keep the year. Plan grounding now restores a year the LLM dropped, keeps a bare shared surname bare (so the answer asks instead of guessing "Hardik"), accepts a player named by first name, and drops calls the archive cannot run (a player passed as a team). The hybrid prefers the rules plan when the LLM misses a matchup. |
| Summary picks the wrong table | The batting or bowling table is chosen by what the question asks ("strike rate" is batting); the asked column is listed first so it is never cut; team title questions count the Champions rows. |
| Paraphrases the rules missed | "run-getter", "maximums", "six-hitter", "scalps", "tally", "aggregate", "biggest", "record team score", "knock", "overs 16 to 20", "first six overs", "who bowled best", city and short team names ("Kolkata", "Knight Riders"; "at Chennai" stays a ground). |

| Metric | Rules before | Rules after | LLM planner before | LLM planner after |
|---|---|---|---|---|
| Paraphrase questions answered correctly | 38/60 | 60/60 | 45/60 | 58/60 |
| Groups right all three ways | 6/20 | 20/20 | 12/20 | 18/20 |
| Groups with the same plan | 6/20 | 20/20 | n/a | 14/20 |
| Entity accuracy | 37/56 | 56/56 | n/a | 56/56 |
| Asks when the name is shared | 8/8 | 8/8 | 6/8 | 8/8 |
| Answers when the name is clear | 3/8 | 8/8 | 6/8 | 8/8 |
| Refuses out-of-scope questions | 4/12 | 12/12 | 4/12 | 12/12 |
| All questions | 53/88 (60%) | 88/88 (100%) | 61/88 (69%) | 86/88 (98%) |

"LLM planner before" for the first 45 questions comes from the run log, which did not record
plans, so plan-based metrics are n/a. The two LLM misses after the fixes ("highest team total" planned as
a run leaderboard, "most sixes in IPL 2024" as the season summary) were planned correctly in the
earlier run: the same prompt at temperature 0 does not always give the same plan across Groq's
models, which is what "same plan 14/20" measures.

**These after-fix numbers are on the questions the fixes were made for**, so they say the fixes
work, not how well Pandit generalises. That is what the held-out set below is for.

### v2 held-out set (38 questions, `qna_ipl_v2_heldout.jsonl`)

Written after the fixes, scored once before any further change: 8 new paraphrase groups (Gayle's
sixes, Buttler 2022, champions 2010, Purple Cap 2013, KKR v SRH, Rohit v Narine, Chinnaswamy,
Narine's economy), 3 new shared surnames (Chahar, Pathan, Marsh), 5 new clear names (Shubman,
Ruturaj, Jaddu, Kuldeep, Mishra), 6 new kinds of off-topic question (SA20, a WPL player by name,
Sachin's ODI strike rate, the T20 World Cup, a player's pay, the Champions League T20).
"Mishra" was first labelled as shared; the archive has one dominant Mishra (Amit, 219 innings),
so it was relabelled a clear name before the run.

| Metric | Rules, first run | LLM planner, first run | Rules after the fixes below |
|---|---|---|---|
| Paraphrase questions answered correctly | 19/24 | 19/24 | 24/24 |
| Groups right all three ways | 5/8 | 5/8 | 8/8 |
| Entity accuracy | 22/26 | 24/26 | 25/26 |
| Shared surnames asked | 3/3 | 3/3 | 3/3 |
| Clear names answered | 5/5 | 5/5 | 5/5 |
| Out of scope refused | 3/6 | 3/6 | 5/6 |
| **All questions** | **30/38 (79%)** | **30/38 (79%)** | 37/38 (97%) |

**79% is the honest number for unseen wordings.** The misses were general gaps, so they were
fixed (and the set is now a development set; a new held-out set is needed for the next honest
number): the asked column could be cut from a long summary row (Gayle's sixes were in the table,
not in the sentence); "aggregate", "economical" and other stat words did not count as a question
about one player; the LLM's matchup was lost to two profiles; an LLM call with a player name as
a team failed instead of falling back; "paid" and "Champions League T20" were not in scope rules.

Still open: an out-of-scope question that names only a non-IPL player ("Smriti Mandhana's highest
score") has no scope word to catch, so it is answered with the IPL leaderboard. Fixing that needs
"is this person in the archive?" before answering a person question, not more keywords.

The v1 sets after these changes (rules): development 130/130, held-out 34/38 answers (89%; was 79%).
