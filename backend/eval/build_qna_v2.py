"""Build the v2 question set (backend/eval/qna_ipl_v2.jsonl): robustness, not coverage.

    python -m backend.eval.build_qna_v2

Four kinds of question, each scored by its own metric in run_qna.py:

* paraphrase groups (expect = "answer", `group` set): one fact asked three ways, including word
  order traps ("Rashid Khan bowling to Dhoni"). Scores accuracy, and whether all three get the
  same answer from the same plan (paraphrase consistency). Each lists the `entities` (players,
  teams, years) a correct plan must use, for entity accuracy.
* clarify (expect = "clarify"): a surname several IPL players share. Right answer: ask which one.
* clear names (expect = "answer"): a name that looks short or shared but means one player to a
  fan ("Rashid", "Hardik"). Right answer: answer, without asking.
* out of scope (expect = "refuse"): WPL, Tests, other leagues, auctions, predictions. Right
  answer: say it is outside the IPL archive, without presenting IPL numbers as the answer.

Gold answers are read from the archive with the verified queries (or are published records,
marked so), like build_qna.py.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from backend.agent.stats import db_path

from .build_qna import FINALS, Archive, surname, team

OUT = Path(__file__).with_name("qna_ipl_v2.jsonl")
HELDOUT = Path(__file__).with_name("qna_ipl_v2_heldout.jsonl")
REFUSAL = ["not in", "only cover", "only have", "only the ipl", "ipl only", "outside", "don't have", "do not have",
           "no data", "not available", "can't", "cannot", "couldn't find", "isn't in", "not part of", "not covered",
           "doesn't cover", "does not cover", "unable"]


def row(qid: str, question: str, expect: str, category: str, checks: list[list[str]], verified: str,
        group: str | None = None, entities: dict[str, list] | None = None, tool: str | None = None,
        note: str = "") -> dict[str, Any]:
    return {"id": qid, "question": question, "expect": expect, "category": category, "group": group,
            "entities": entities or {}, "expected_tool": tool, "checks": checks, "verified": verified,
            "expected_route": "stats" if expect in ("answer", "clarify") else "any", "expected_args": {},
            **({"note": note} if note else {})}


def build(db: str) -> list[dict[str, Any]]:
    a = Archive(db)
    items: list[dict[str, Any]] = []

    def group(gid: str, phrasings: list[str], checks: list[list[str]], verified: str, entities: dict[str, list],
              tool: str, category: str) -> None:
        for i, q in enumerate(phrasings, 1):
            items.append(row(f"{gid}-{i}", q, "answer", category, checks, verified, gid, entities, tool))

    def career(name: str, role: str) -> dict[str, Any]:
        player = a.rows("players", name)[0]["player"]
        return a.total("player", name, title=f"{player}: {'batting' if role == 'bat' else 'bowling'} by year")

    def season_runs(name: str, year: int) -> int:
        player = a.rows("players", name)[0]["player"]
        rows = a.rows("player", name, "--year", str(year), title=f"{player}: batting by year")
        return next(r for r in rows if str(r["year"]) == str(year))["runs"]

    # --- 20 paraphrase groups -------------------------------------------------------------
    group("kohli-2016", ["How many runs did Virat Kohli score in IPL 2016?", "Kohli's run tally in the 2016 season?",
                         "2016 IPL: total runs by V Kohli"],
          [["973"]], "published", {"players": ["Virat Kohli"], "years": [2016]}, "player_profile", "Player season")
    group("bumrah-wkts", ["How many IPL wickets has Jasprit Bumrah taken?", "Bumrah wicket count in the IPL",
                          "total IPL scalps for Bumrah"],
          [[str(career("Jasprit Bumrah", "bowl")["wkts"])]], "archive", {"players": ["Jasprit Bumrah"]},
          "player_profile", "Player career")
    group("orange-2023", ["Who won the Orange Cap in 2023?", "top run-getter of IPL 2023",
                          "which batter scored the most runs in the 2023 IPL"],
          [["Gill"], ["890"]], "published", {"years": [2023]}, "batting_leaders", "Season leader")
    group("purple-2020", ["Purple Cap winner 2020", "who took the most wickets in IPL 2020",
                          "leading wicket-taker, 2020 season"],
          [["Rabada"], ["30"]], "published", {"years": [2020]}, "bowling_leaders", "Season leader")
    group("champ-2016", ["Who won IPL 2016?", "2016 IPL champions", "which side lifted the IPL trophy in 2016"],
          [team(FINALS[2016][0])], "published", {"years": [2016]}, "champions", "Champions")
    titles = {r["team"]: r for r in a.rows("champions", title="Titles by team")}
    group("mi-titles", ["How many IPL titles have Mumbai Indians won?", "MI trophy count",
                        "number of IPL championships for Mumbai"],
          [[str(titles["MI"]["titles"])]], "archive", {"teams": ["MI"]}, "team_seasons", "Team titles")
    h2h = a.total("h2h", "MI", "CSK")
    won = {k.split()[0]: v for k, v in h2h.items() if k.endswith(" won")}
    group("mi-csk", ["MI vs CSK head to head", "how many times has CSK beaten MI",
                     "Chennai against Mumbai: the record"],
          [[str(won["CSK"])]], "archive", {"teams": ["CSK", "MI"]}, "head_to_head", "Head to head")
    m = a.total("matchup", "MS Dhoni", "Rashid Khan")
    group("dhoni-rashid", ["How has Dhoni done against Rashid Khan?", "MS Dhoni vs Rashid Khan",
                           "Rashid Khan bowling to Dhoni: the numbers"],
          [[str(m["runs"])], [str(m["balls"])]], "archive", {"players": ["MS Dhoni", "Rashid Khan"]},
          "batter_vs_bowler", "Batter v bowler")
    death = a.rows("phase", "death", "--role", "bowl", "--year", "2024", "--limit", "1")[0]
    group("death-2024", ["Best death-overs bowler in 2024", "who was most economical at the death in IPL 2024",
                         "top bowler in overs 16 to 20, 2024 season"],
          [[surname(death["player"])]], "archive", {"years": [2024]}, "phase_leaders", "Phases")
    group("top-total", ["highest team total in IPL history", "biggest IPL innings total ever",
                        "what's the record team score in the IPL"],
          [["287"]], "published", {}, "records", "Records")
    wank = a.rows("venues", "wankhede", "--limit", "1")[0]
    group("wankhede", ["average first innings score at the Wankhede", "Wankhede: what do teams batting first average?",
                       "typical 1st-innings total at Wankhede Stadium"],
          [[str(wank["avg_1st_inns"])]], "archive", {}, "venue_stats", "Venues")
    rcb = next(r for r in a.rows("team", "RCB", "--year", "2025") if str(r["year"]) == "2025")
    group("rcb-2025", ["How many matches did RCB win in 2025?", "RCB's 2025 win count",
                       "Royal Challengers Bengaluru wins in IPL 2025"],
          [[str(rcb["won"])]], "archive", {"teams": ["RCB"], "years": [2025]}, "team_seasons", "Team season")
    group("sixes-2024", ["most sixes in IPL 2024", "who hit the most maximums in 2024",
                         "biggest six-hitter of the 2024 season"],
          [["Sharma"], ["42"]], "archive", {"years": [2024]}, "batting_leaders", "Leaderboard")
    group("chahal-wkts", ["Chahal's IPL wickets", "Yuzvendra Chahal wicket tally",
                          "how many batters has Chahal dismissed in the IPL"],
          [[str(career("Yuzvendra Chahal", "bowl")["wkts"])]], "archive", {"players": ["Yuzvendra Chahal"]},
          "player_profile", "Player career")
    group("top-score", ["highest individual score in the IPL", "best ever IPL knock by runs",
                        "record individual innings in the IPL"],
          [["Gayle"], ["175"]], "published", {}, "records", "Records")
    seasons = a.rows("seasons")
    fastest = max((r for r in seasons if r["run_rate"]), key=lambda r: r["run_rate"])
    group("run-rate", ["which IPL season had the highest run rate", "highest scoring season by run rate",
                       "in which year was IPL scoring fastest, by run rate"],
          [[str(fastest["year"])]], "archive", {}, "season_summary", "Season totals")
    kkr = a.rows("top-batters", "--year", "2024", "--team", "KKR", "--limit", "1")[0]
    group("kkr-2024", ["KKR's top run scorer in 2024", "who scored the most runs for Kolkata in IPL 2024",
                       "leading batter for Knight Riders, 2024"],
          [[surname(kkr["player"])], [str(kkr["runs"])]], "archive", {"teams": ["KKR"], "years": [2024]},
          "batting_leaders", "Leaderboard")
    pp = a.rows("phase", "powerplay", "--role", "bowl", "--year", "2023", "--limit", "1")[0]
    group("pp-2023", ["best powerplay bowler of IPL 2023", "most economical bowler in the first six overs, 2023",
                      "2023 powerplay: who bowled best"],
          [[surname(pp["player"])]], "archive", {"years": [2023]}, "phase_leaders", "Phases")
    kb = a.total("matchup", "Virat Kohli", "Jasprit Bumrah")
    group("kohli-bumrah", ["How has Kohli batted against Bumrah?", "Bumrah vs Kohli",
                           "Virat Kohli facing Jasprit Bumrah, all time"],
          [[str(kb["runs"])], [str(kb["balls"])]], "archive", {"players": ["Virat Kohli", "Jasprit Bumrah"]},
          "batter_vs_bowler", "Batter v bowler")
    russell = career("Andre Russell", "bat")
    group("russell-sr", ["Andre Russell's IPL strike rate", "how fast does Russell score in the IPL",
                         "Russell career SR in IPL"],
          [[str(russell["sr"])]], "archive", {"players": ["Andre Russell"]}, "player_profile", "Player career")

    # --- clarify vs answer ------------------------------------------------------------------
    for qid, q, names in (("sharma", "How did Sharma do in 2024?", ["Rohit Sharma", "Mohit Sharma", "Sandeep Sharma", "Ishant Sharma", "Karn Sharma", "Abhishek Sharma"]),
                          ("patel", "Patel's IPL wickets", ["Axar Patel", "Harshal Patel", "Munaf Patel", "Parthiv Patel"]),
                          ("singh", "What are Singh's career stats?", ["Harbhajan Singh", "Yuvraj Singh", "RP Singh", "Arshdeep Singh", "Mandeep Singh"]),
                          ("pandya", "Pandya's batting record in the IPL", ["Hardik Pandya", "Krunal Pandya"]),
                          ("khan", "How many wickets does Khan have?", ["Rashid Khan", "Zaheer Khan", "Avesh Khan"]),
                          ("kumar", "Kumar's economy rate", ["Bhuvneshwar Kumar", "Praveen Kumar", "Vinay Kumar"]),
                          ("yadav", "Yadav career numbers", ["Umesh Yadav", "Suryakumar Yadav", "Kuldeep Yadav"]),
                          ("iyer", "Iyer's runs in 2024", ["Shreyas Iyer", "Venkatesh Iyer"])):
        items.append(row(f"clarify-{qid}", q, "clarify", "Ambiguous name", [names], "n/a",
                         note="Several IPL players share this name: the right answer is a question back."))
    for qid, q, name, role, field, year in (("rashid", "Rashid's IPL economy", "Rashid Khan", "bowl", "econ", None),
                                            ("hardik", "Hardik's runs in 2024", "Hardik Pandya", "bat", "runs", 2024),
                                            ("rohit", "Rohit's IPL runs", "Rohit Sharma", "bat", "runs", None),
                                            ("bhuvi", "Bhuvneshwar's IPL wickets", "Bhuvneshwar Kumar", "bowl", "wkts", None),
                                            ("sky", "Suryakumar's runs in 2023", "Suryakumar Yadav", "bat", "runs", 2023),
                                            ("axar", "Axar Patel's wickets in 2024", "Axar Patel", "bowl", "wkts", 2024),
                                            ("abd", "de Villiers strike rate in IPL", "AB de Villiers", "bat", "sr", None),
                                            ("dhoni", "Dhoni's IPL runs", "MS Dhoni", "bat", "runs", None)):
        if year:
            player = a.rows("players", name)[0]["player"]
            title = f"{player}: {'batting' if role == 'bat' else 'bowling'} by year"
            value = next(r for r in a.rows("player", name, "--year", str(year), title=title) if str(r["year"]) == str(year))[field]
        else:
            value = career(name, role)[field]
        items.append(row(f"clear-{qid}", q, "answer", "Clear name", [[surname(name)], [str(value)]], "archive",
                         entities={"players": [name], **({"years": [year]} if year else {})}, tool="player_profile",
                         note="A fan reads this as one player: answer without asking."))

    # --- out of scope -------------------------------------------------------------------------
    for qid, q in (("wpl-runs", "Who scored the most runs in WPL 2024?"),
                   ("wpl-winner", "Who won the Women's Premier League in 2025?"),
                   ("test-avg", "What is Virat Kohli's Test batting average?"),
                   ("odi-wc", "Who won the 2023 ODI World Cup?"),
                   ("bbl", "Most wickets in the Big Bash League 2024"),
                   ("psl", "PSL 2025 winner"),
                   ("t20i", "How many T20I runs has Rohit Sharma scored for India?"),
                   ("auction", "How much did Rishabh Pant sell for in the IPL auction?"),
                   ("predict", "Who will win IPL 2027?"),
                   ("age", "How old is MS Dhoni?"),
                   ("live", "What's the score in today's IPL match?"),
                   ("football", "Which club has won the most ISL titles?")):
        items.append(row(f"scope-{qid}", q, "refuse", "Out of scope", [REFUSAL], "n/a",
                         note="Outside the IPL ball-by-ball archive: say so, do not answer with IPL numbers."))
    return items


def build_heldout(db: str) -> list[dict[str, Any]]:
    """37 new questions written after the v2 fixes and never used to tune them: new wordings, new
    players, new shared surnames, new kinds of off-topic question. Scored once, as the honest number."""
    a = Archive(db)
    items: list[dict[str, Any]] = []

    def group(gid: str, phrasings: list[str], checks: list[list[str]], verified: str, entities: dict[str, list],
              tool: str, category: str) -> None:
        for i, q in enumerate(phrasings, 1):
            items.append(row(f"h-{gid}-{i}", q, "answer", category, checks, verified, gid, entities, tool))

    def career(name: str, role: str) -> dict[str, Any]:
        player = a.rows("players", name)[0]["player"]
        return a.total("player", name, title=f"{player}: {'batting' if role == 'bat' else 'bowling'} by year")

    def season(name: str, role: str, year: int, field: str) -> Any:
        player = a.rows("players", name)[0]["player"]
        title = f"{player}: {'batting' if role == 'bat' else 'bowling'} by year"
        return next(r for r in a.rows("player", name, "--year", str(year), title=title) if str(r["year"]) == str(year))[field]

    group("gayle-sixes", ["How many sixes has Chris Gayle hit in the IPL?", "Gayle's IPL maximums",
                          "total sixes struck by Chris Gayle"],
          [[str(career("Chris Gayle", "bat")["6s"])]], "archive", {"players": ["Chris Gayle"]}, "player_profile",
          "Player career")
    group("buttler-2022", ["Jos Buttler runs in IPL 2022", "how many did Buttler score in the 2022 season",
                           "Buttler's 2022 IPL aggregate"],
          [["863"]], "published", {"players": ["Jos Buttler"], "years": [2022]}, "player_profile", "Player season")
    group("champ-2010", ["who were IPL champions in 2010", "2010 IPL title winner", "which team won the 2010 edition"],
          [team(FINALS[2010][0])], "published", {"years": [2010]}, "champions", "Champions")
    group("purple-2013", ["Purple Cap 2013", "most wickets in IPL 2013", "top wicket-taker of the 2013 season"],
          [["Bravo"], ["32"]], "published", {"years": [2013]}, "bowling_leaders", "Season leader")
    h2h = a.total("h2h", "KKR", "SRH")
    won = {k.split()[0]: v for k, v in h2h.items() if k.endswith(" won")}
    group("kkr-srh", ["KKR vs SRH head to head", "how many times have Kolkata beaten Hyderabad",
                      "Sunrisers against Knight Riders record"],
          [[str(won["KKR"])]], "archive", {"teams": ["KKR", "SRH"]}, "head_to_head", "Head to head")
    m = a.total("matchup", "Rohit Sharma", "Sunil Narine")
    group("rohit-narine", ["Rohit Sharma against Sunil Narine", "Narine vs Rohit",
                           "Sunil Narine bowling to Rohit Sharma"],
          [[str(m["runs"])], [str(m["balls"])]], "archive", {"players": ["Rohit Sharma", "Sunil Narine"]},
          "batter_vs_bowler", "Batter v bowler")
    venue = a.rows("venues", "chinnaswamy", "--limit", "1")[0]
    group("chinnaswamy", ["average first innings score at Chinnaswamy", "Chinnaswamy: what's par batting first?",
                          "typical first-innings total at the Bengaluru stadium"],
          [[str(venue["avg_1st_inns"])]], "archive", {}, "venue_stats", "Venues")
    group("narine-econ", ["Sunil Narine's IPL economy rate", "how economical has Narine been in the IPL",
                          "Narine career economy"],
          [[str(career("Sunil Narine", "bowl")["econ"])]], "archive", {"players": ["Sunil Narine"]}, "player_profile",
          "Player career")

    for qid, q, names in (("chahar", "Chahar's wickets in the IPL", ["Deepak Chahar", "Rahul Chahar"]),
                          ("pathan", "How did Pathan do in 2010?", ["Yusuf Pathan", "Irfan Pathan"]),
                          ("marsh", "Marsh's IPL runs", ["Shaun Marsh", "Mitchell Marsh"])):
        items.append(row(f"h-clarify-{qid}", q, "clarify", "Ambiguous name", [names], "n/a"))
    for qid, q, name, role, field, year in (("shubman", "Shubman's runs in 2023", "Shubman Gill", "bat", "runs", 2023),
                                            ("ruturaj", "Ruturaj's runs in 2021", "Ruturaj Gaikwad", "bat", "runs", 2021),
                                            ("jaddu", "Jaddu's IPL wickets", "Ravindra Jadeja", "bowl", "wkts", None),
                                            ("kuldeep", "Kuldeep's IPL wickets", "Kuldeep Yadav", "bowl", "wkts", None),
                                            ("mishra", "Mishra career economy", "Amit Mishra", "bowl", "econ", None)):
        value = season(name, role, year, field) if year else career(name, role)[field]
        items.append(row(f"h-clear-{qid}", q, "answer", "Clear name", [[surname(name)], [str(value)]], "archive",
                         entities={"players": [name], **({"years": [year]} if year else {})}, tool="player_profile"))
    for qid, q in (("sa20", "Who topped the run charts in SA20 2025?"),
                   ("mandhana", "Smriti Mandhana's highest score"),
                   ("sachin-odi", "What was Sachin Tendulkar's ODI strike rate?"),
                   ("t20wc", "Who won the 2024 T20 World Cup?"),
                   ("pay", "How much is Virat Kohli paid by RCB?"),
                   ("clt20", "Which team won the Champions League T20 in 2014?")):
        items.append(row(f"h-scope-{qid}", q, "refuse", "Out of scope", [REFUSAL], "n/a"))
    return items


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--heldout", action="store_true", help="write the held-out set instead")
    a = ap.parse_args(argv)
    items = (build_heldout if a.heldout else build)(a.db or db_path())
    if a.heldout and a.out == str(OUT):
        a.out = str(HELDOUT)
    assert len({i["id"] for i in items}) == len(items)
    with open(a.out, "w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    kinds: dict[str, int] = {}
    for item in items:
        kinds[item["expect"]] = kinds.get(item["expect"], 0) + 1
    print(f"wrote {len(items)} questions to {a.out}: {kinds}; {len({i['group'] for i in items if i['group']})} paraphrase groups")


if __name__ == "__main__":
    main()
