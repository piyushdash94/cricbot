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
| LLM tool calling only | summary of the tables (no LLM) | 39/43 (91%) | 32/38 (84%) |
| **Hybrid: LLM plan, rules fallback (the app)** | **LLM, under the grounding check** | **43/43 (100%)** | **35/38 (92%)** |

All 14 published answers on the held-out set were right end to end.

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
