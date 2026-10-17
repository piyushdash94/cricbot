"""Score Pandit on the IPL question set (backend/eval/qna_ipl.jsonl).

    python -m backend.eval.run_qna --planner rules                 # keyword planner, no LLM, seconds
    python -m backend.eval.run_qna --planner hybrid                # what the app does: LLM plan, rules fallback
    python -m backend.eval.run_qna --planner hybrid --answer       # also write answers (LLM synthesis)
    python -m backend.eval.run_qna --only season_leader,record     # some categories
    python -m backend.eval.run_qna --dataset backend/eval/qna_ipl_v2.jsonl --planner rules   # v2 robustness set

Scores per question:
  route   stats vs match path matches `expected_route`
  tool    the expected stats tool was among the planned calls
  args    every expected argument (year, team, sort, phase, role) was passed as expected
  answer  the reply contains one string from every `checks` group (needs --answer for the LLM;
          without it, the answer is Pandit's no-LLM summary of the same tables)

The v2 set (build_qna_v2.py) adds an `expect` per question and four robustness metrics:
  paraphrase consistency  the same fact asked three ways: all right, same outcome, same plan
  entity accuracy         the plan used exactly the expected players, teams and years
  clarify vs answer       asks "which one?" for a shared surname, and does not ask for a clear name
  refusals                says a question is outside the IPL archive instead of answering it
Every v2 question runs through the real app graph. With --planner rules that is the app with no
LLM keys (keyword plan, table summary); without --answer an LLM run plans with the LLM but
answers with the table summary, which saves tokens.

Rate limits (Groq free tier: 8k tokens/min per model) are waited out rather than falling back to
another provider, so the numbers describe the configured model. Results go to data/eval_qna_<run>.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from backend.agent import stats
from backend.agent.entities import resolve_entities
from backend.agent.llm import LLMRouter, load_env_file
from backend.db import store

DATASET = Path(__file__).with_name("qna_ipl.jsonl")


class PatientRouter(LLMRouter):
    """Retry a rate-limited provider instead of moving to the next one (evaluation only)."""

    def __init__(self, wait_limit: float = 90.0):
        super().__init__()
        self.wait_limit = wait_limit
        self.waited = 0.0

    def _retry(self, fn, *args, **kwargs):
        for attempt in range(6):
            try:
                return fn(*args, **kwargs)
            except RuntimeError as exc:
                text = str(exc)
                if "429" not in text and "rate limit" not in text.casefold():
                    raise
                match = re.search(r"try again in ([\d.]+)s", text)
                delay = min(self.wait_limit, float(match.group(1)) + 1 if match else 10.0 * (attempt + 1))
                self.waited += delay
                time.sleep(delay)
        return fn(*args, **kwargs)

    def plan(self, messages, tools):
        return self._retry(super().plan, messages, tools)

    def chat(self, messages, **kwargs):
        return self._retry(super().chat, messages, **kwargs)


def contains(reply: str, wanted: str) -> bool:
    if re.fullmatch(r"[\d.]+", wanted):
        return re.search(rf"(?<![\d.]){re.escape(wanted)}(?!\d|\.\d)", reply.replace(",", "")) is not None
    return wanted.casefold() in reply.casefold()


def answer_ok(reply: str, checks: list[list[str]]) -> bool:
    return bool(checks) and all(any(contains(reply, option) for option in group) for group in checks)


def args_ok(expected: dict[str, Any], tool: str | None, calls: list[dict[str, Any]]) -> bool | None:
    if not tool:
        return None
    if not expected:
        return any(c["name"] == tool for c in calls)
    for call in calls:
        if call["name"] != tool:
            continue
        got = dict(call.get("args") or {})
        if tool == "head_to_head":                         # team order does not matter
            pair = sorted(str(got.get(k) or "").upper() for k in ("team1", "team2"))
            want = sorted(str(expected.get(k) or "").upper() for k in ("team1", "team2"))
            if pair == want and all(str(got.get(k) or "") == str(v) for k, v in expected.items() if k == "year"):
                return True
            continue
        if all(str(got.get(k) or "").strip().upper() == str(v).upper() for k, v in expected.items()):
            return True
    return False


class PlanOnlyRouter:
    """Plans with the LLM, but refuses to write answers, so the app falls back to its table summary."""

    def __init__(self, inner: LLMRouter):
        self.inner, self.providers = inner, inner.providers

    def plan(self, messages, tools):
        return self.inner.plan(messages, tools)

    def chat(self, messages, **kwargs):
        raise RuntimeError("answers written from the tables in this run")

    def status(self):
        return self.inner.status()


ASK = re.compile(r"\bwhich\b|\bdo you mean\b|\bcould be\b|\bclarify\b", re.I)


def asked(reply: str) -> bool:
    return bool(ASK.search(reply)) and "?" in reply


def planned_entities(calls: list[dict[str, Any]], index: Any) -> dict[str, set]:
    from backend.db.queries import Q
    out: dict[str, set] = {"players": set(), "teams": set(), "years": set()}
    for call in calls:
        args = call.get("args") or {}
        for k in ("name", "batter", "bowler"):
            if args.get(k):
                player, _ = index.lookup(str(args[k]))
                out["players"].add((player.name if player else str(args[k])).split()[-1].casefold())
        for k in ("team", "team1", "team2"):
            if args.get(k):
                out["teams"].add(Q.team(str(args[k])))
        year = str(args.get("year") or "")
        if re.fullmatch(r"\d{4}(-\d{4})?", year):
            out["years"].update(int(y) for y in year.split("-"))
    return out


def entity_check(item: dict[str, Any], calls: list[dict[str, Any]], index: Any) -> tuple[bool | None, str]:
    want = item.get("entities") or {}
    if not want or not calls:
        return (None, "") if not want else (False, "no plan")
    got = planned_entities(calls, index)
    expected = {"players": {p.split()[-1].casefold() for p in want.get("players", [])},
                "teams": set(want.get("teams", [])), "years": set(want.get("years", []))}
    wrong = [f"{k}: wanted {sorted(expected[k])}, planned {sorted(got[k])}" for k in expected if expected[k] != got[k]]
    return (not wrong), "; ".join(wrong)


def signature(calls: list[dict[str, Any]]) -> str:
    return " | ".join(sorted(f"{c['name']}({', '.join(f'{k}={v}' for k, v in sorted((c.get('args') or {}).items()) if v not in (None, ''))})"
                             for c in calls))


def run_v2(items: list[dict[str, Any]], planner: str, answer: bool, sleep: float,
           out: Path | None = None) -> list[dict[str, Any]]:
    """Every question through the real graph, scored by what it expects."""
    from backend.agent.service import CricbotAgent

    os.environ["PANDIT_PLANNER"] = planner
    if planner == "rules":
        router: Any = LLMRouter([])                               # the app with no LLM keys
    else:
        router = PatientRouter() if answer else PlanOnlyRouter(PatientRouter())
    agent = CricbotAgent(llm=router)
    index = stats.player_index()
    rows = []
    for n, item in enumerate(items, 1):
        started = time.time()
        result = agent.answer(item["question"])
        step = next(e for e in result["trace"] if e["node"] == "plan_question")["patch"]
        calls, used = step.get("calls") or [], step.get("planner") or ""
        reply = result["reply"].replace("\u202f", " ")
        expect = item["expect"]
        if expect == "clarify":
            names = item["checks"][0]
            ok = asked(reply) and sum(1 for name in names if name.casefold() in reply.casefold()) >= 2
        elif expect == "refuse":
            ok = any(phrase in reply.casefold() for phrase in item["checks"][0])
        else:
            ok = answer_ok(reply, item["checks"]) and not asked(reply)
        entity_ok, entity_note = entity_check(item, calls, index) if expect == "answer" else (None, "")
        row = {
            "id": item["id"], "category": item["category"], "verified": item["verified"], "question": item["question"],
            "expect": expect, "group": item.get("group"), "planner": used, "calls": calls, "signature": signature(calls),
            "reply": reply, "validation": result.get("validation", ""), "provider": result.get("provider") or "",
            "route_ok": None, "tool_ok": None if not item.get("expected_tool") else any(c["name"] == item["expected_tool"] for c in calls),
            "args_ok": None, "answer_ok": ok, "entity_ok": entity_ok, "entity_note": entity_note,
            "asked": asked(reply), "leaked": expect == "refuse" and bool(calls),
            "seconds": round(time.time() - started, 2), "note": "",
        }
        rows.append(row)
        print(f"[{n:3}/{len(items)}] {'ok ' if ok else 'BAD'} {item['id']:<22} {used[:30]:<30} {reply[:90]!r}", flush=True)
        if out:                                       # save as we go: a stopped run keeps what it finished
            out.write_text(json.dumps({"planner": planner, "answer": answer, "partial": n < len(items), "rows": rows},
                                      indent=1), encoding="utf-8")
        if sleep:
            time.sleep(sleep)
    return rows


def report_v2(rows: list[dict[str, Any]]) -> str:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["group"]:
            groups[r["group"]].append(r)
    full = [g for g in groups.values() if len(g) > 1]
    para = [r for r in rows if r["group"]]
    clear = [r for r in rows if r["category"] == "Clear name"]
    lines = ["| Metric | Score | What it means |", "|---|---|---|",
             f"| Paraphrase questions answered correctly | {rate([r['answer_ok'] for r in para])} | each of the three wordings scored on its own |",
             f"| Groups with all three right | {rate([all(r['answer_ok'] for r in g) for g in full])} | robust: wording did not matter |",
             f"| Groups with a consistent outcome | {rate([len({r['answer_ok'] for r in g}) == 1 for g in full])} | all right or all wrong |",
             f"| Groups with the same plan | {rate([len({r['signature'] for r in g}) == 1 for g in full])} | identical queries and arguments for all three |",
             f"| Entity accuracy | {rate([r['entity_ok'] for r in rows])} | plan used exactly the expected players, teams and years |",
             f"| Asks when the name is shared | {rate([r['answer_ok'] for r in rows if r['expect'] == 'clarify'])} | e.g. \"Sharma\", \"Pandya\" |",
             f"| Answers when the name is clear | {rate([r['answer_ok'] for r in clear])} | e.g. \"Rashid\", \"Hardik\"; asking here is a miss |",
             f"| Refuses out-of-scope questions | {rate([r['answer_ok'] for r in rows if r['expect'] == 'refuse'])} | WPL, Tests, other leagues, auctions, predictions |",
             f"| ...of which ran IPL stats queries anyway | {sum(r['leaked'] for r in rows)} | answered from IPL data for a non-IPL question |",
             f"| All questions | {rate([r['answer_ok'] for r in rows])} | |"]
    return "\n".join(lines)


def run(items: list[dict[str, Any]], planner: str, answer: bool, db: str, sleep: float) -> list[dict[str, Any]]:
    from backend.agent.service import CricbotAgent

    router = PatientRouter() if planner != "rules" else None
    if answer and router:
        os.environ["PANDIT_PLANNER"] = planner
    agent = CricbotAgent(llm=router) if (answer and router) else None
    index = stats.player_index()
    rows = []
    for n, item in enumerate(items, 1):
        started = time.time()
        entities = resolve_entities(item["question"])
        reply, validation, provider = "", "", ""
        if agent and item["expected_route"] == "stats":
            # One pass through the real graph: its plan_question node is the plan being scored.
            result = agent.answer(item["question"])
            step = next(e for e in result["trace"] if e["node"] == "plan_question")["patch"]
            calls, used, note = step.get("calls") or [], step.get("planner") or "", ""
            route = step.get("route") or "match"
            if route == "stats":
                reply, validation, provider = result["reply"], result["validation"], result.get("provider") or ""
        else:
            calls, used, note = stats.plan(item["question"], entities, router, None, index, mode=planner)
            route = "stats" if calls else "match"
            if route == "stats" and item["expected_route"] == "stats":
                reply, validation = stats.summary(stats.run_calls(calls, db=db, index=index), item["question"]), "summary"
        row = {
            "id": item["id"], "category": item["category"], "verified": item["verified"], "question": item["question"],
            "planner": used, "calls": calls, "reply": reply, "validation": validation, "provider": provider,
            "route_ok": route == item["expected_route"],
            "tool_ok": None if not item["expected_tool"] else any(c["name"] == item["expected_tool"] for c in calls),
            "args_ok": args_ok(item["expected_args"], item["expected_tool"], calls),
            "answer_ok": None if item["expected_route"] == "match" else answer_ok(reply, item["checks"]),
            "seconds": round(time.time() - started, 2), "note": note,
        }
        rows.append(row)
        mark = "ok " if row["route_ok"] and row["answer_ok"] in (True, None) else "BAD"
        print(f"[{n:3}/{len(items)}] {mark} {item['id']:<28} {used[:34]:<34} {reply[:90]!r}", flush=True)
        if sleep:
            time.sleep(sleep)
    return rows


def rate(values: list[bool | None]) -> str:
    scored = [v for v in values if v is not None]
    return f"{sum(scored)}/{len(scored)} ({100 * sum(scored) / len(scored):.0f}%)" if scored else "-"


def report(rows: list[dict[str, Any]]) -> str:
    lines = ["| Group | Questions | Route | Tool | Args | Answer |", "|---|---|---|---|---|---|"]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[f"category: {row['category']}"].append(row)
        groups[f"verified: {row['verified']}"].append(row)
        groups["ALL"].append(row)
    for name in sorted(groups, key=lambda g: (g != "ALL", g)):
        group = groups[name]
        lines.append(f"| {name} | {len(group)} | {rate([r['route_ok'] for r in group])} | {rate([r['tool_ok'] for r in group])} "
                     f"| {rate([r['args_ok'] for r in group])} | {rate([r['answer_ok'] for r in group])} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--planner", choices=["rules", "llm", "hybrid"], default="hybrid")
    ap.add_argument("--answer", action="store_true", help="write answers with the LLM (otherwise the no-LLM summary)")
    ap.add_argument("--only", help="comma-separated categories")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--every", type=int, default=1, help="every Kth question (a quicker sample)")
    ap.add_argument("--sleep", type=float, default=0.0, help="seconds between questions")
    ap.add_argument("--db")
    ap.add_argument("--dataset", default=str(DATASET))
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    load_env_file()
    db = a.db or str(store.DEFAULT_DB)
    os.environ["CRICBOT_DB"] = db
    items = [json.loads(line) for line in Path(a.dataset).read_text(encoding="utf-8").splitlines() if line.strip()]
    if a.only:
        wanted = set(a.only.split(","))
        items = [i for i in items if i["category"] in wanted]
    items = items[:: max(1, a.every)]
    items = items[: a.limit] if a.limit else items
    v2 = bool(items) and "expect" in items[0]
    out = Path(a.out or Path(db).parent / f"eval_qna{'_v2' if v2 else ''}_{a.planner}{'_answer' if a.answer else ''}.json")
    rows = run_v2(items, a.planner, a.answer, a.sleep, out) if v2 else run(items, a.planner, a.answer, db, a.sleep)
    table = report_v2(rows) if v2 else report(rows)
    print("\n" + table)
    failures = [r for r in rows if (r["route_ok"] is False) or r["answer_ok"] is False or r["tool_ok"] is False
                or r.get("entity_ok") is False]
    if failures:
        print("\nMisses:")
        for r in failures:
            extra = f" entity: {r['entity_note']}" if r.get("entity_note") else ""
            print(f"- {r['id']}: answer_ok={r['answer_ok']} tool_ok={r['tool_ok']} calls={r['calls']}{extra} "
                  f"reply={r['reply'][:160]!r}")
    out.write_text(json.dumps({"planner": a.planner, "answer": a.answer, "summary": table, "rows": rows}, indent=1),
                   encoding="utf-8")
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
