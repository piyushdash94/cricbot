"""Score Pandit on the IPL question set (backend/eval/qna_ipl.jsonl).

    python -m backend.eval.run_qna --planner rules                 # keyword planner, no LLM, seconds
    python -m backend.eval.run_qna --planner hybrid                # what the app does: LLM plan, rules fallback
    python -m backend.eval.run_qna --planner hybrid --answer       # also write answers (LLM synthesis)
    python -m backend.eval.run_qna --only season_leader,record     # some categories

Scores per question:
  route   stats vs match path matches `expected_route`
  tool    the expected stats tool was among the planned calls
  args    every expected argument (year, team, sort, phase, role) was passed as expected
  answer  the reply contains one string from every `checks` group (needs --answer for the LLM;
          without it, the answer is Pandit's no-LLM summary of the same tables)

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
    rows = run(items, a.planner, a.answer, db, a.sleep)
    table = report(rows)
    print("\n" + table)
    failures = [r for r in rows if not r["route_ok"] or r["answer_ok"] is False or r["tool_ok"] is False]
    if failures:
        print("\nMisses:")
        for r in failures:
            print(f"- {r['id']}: route_ok={r['route_ok']} tool_ok={r['tool_ok']} args_ok={r['args_ok']} "
                  f"answer_ok={r['answer_ok']} calls={r['calls']} reply={r['reply'][:160]!r}")
    out = Path(a.out or Path(db).parent / f"eval_qna_{a.planner}{'_answer' if a.answer else ''}.json")
    out.write_text(json.dumps({"planner": a.planner, "answer": a.answer, "summary": table, "rows": rows}, indent=1),
                   encoding="utf-8")
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
