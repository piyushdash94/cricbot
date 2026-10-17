"""Build the IPL question-answer evaluation set (backend/eval/qna_ipl.jsonl).

    python -m backend.eval.build_qna            # writes qna_ipl.jsonl from published facts + the archive

Two kinds of gold answer, kept apart on purpose:

* verified = "published": the answer is typed in below from public records (Orange and
  Purple Cap winners, champions, runners-up, all-time records up to IPL 2025). The build
  checks each one against the archive and stops if they disagree, so these questions test
  the data as well as Pandit.
* verified = "archive": the answer is read from the archive with the same verified
  queries Pandit uses. These test that Pandit picks the right query and reports it
  faithfully; they cannot catch a data error.

Each item has `checks`: groups of acceptable strings, and the reply must contain one string
from every group (case-insensitive; numbers must match as whole numbers).
Items with `expected_route = "match"` must NOT be answered from the stats archive.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from backend.db import queries
from backend.agent.stats import db_path

OUT = Path(__file__).with_name("qna_ipl.jsonl")
HELDOUT = Path(__file__).with_name("qna_ipl_heldout.jsonl")

ORANGE = {2008: ("Shaun Marsh", 616), 2009: ("Matthew Hayden", 572), 2010: ("Sachin Tendulkar", 618),
          2011: ("Chris Gayle", 608), 2012: ("Chris Gayle", 733), 2013: ("Michael Hussey", 733),
          2014: ("Robin Uthappa", 660), 2015: ("David Warner", 562), 2016: ("Virat Kohli", 973),
          2017: ("David Warner", 641), 2018: ("Kane Williamson", 735), 2019: ("David Warner", 692),
          2020: ("KL Rahul", 670), 2021: ("Ruturaj Gaikwad", 635), 2022: ("Jos Buttler", 863),
          2023: ("Shubman Gill", 890), 2024: ("Virat Kohli", 741), 2025: ("Sai Sudharsan", 759)}
PURPLE = {2008: ("Sohail Tanvir", 22), 2009: ("RP Singh", 23), 2010: ("Pragyan Ojha", 21),
          2011: ("Lasith Malinga", 28), 2012: ("Morne Morkel", 25), 2013: ("Dwayne Bravo", 32),
          2014: ("Mohit Sharma", 23), 2015: ("Dwayne Bravo", 26), 2016: ("Bhuvneshwar Kumar", 23),
          2017: ("Bhuvneshwar Kumar", 26), 2018: ("Andrew Tye", 24), 2019: ("Imran Tahir", 26),
          2020: ("Kagiso Rabada", 30), 2021: ("Harshal Patel", 32), 2022: ("Yuzvendra Chahal", 27),
          2023: ("Mohammed Shami", 28), 2024: ("Harshal Patel", 24), 2025: ("Prasidh Krishna", 25)}
FINALS = {2008: ("RR", "CSK"), 2009: ("DCH", "RCB"), 2010: ("CSK", "MI"), 2011: ("CSK", "RCB"),
          2012: ("KKR", "CSK"), 2013: ("MI", "CSK"), 2014: ("KKR", "PBKS"), 2015: ("MI", "CSK"),
          2016: ("SRH", "RCB"), 2017: ("MI", "RPS"), 2018: ("CSK", "SRH"), 2019: ("MI", "CSK"),
          2020: ("MI", "DC"), 2021: ("CSK", "KKR"), 2022: ("GT", "RR"), 2023: ("CSK", "GT"),
          2024: ("KKR", "SRH"), 2025: ("RCB", "PBKS")}
TEAM_NAMES = {"CSK": ["CSK", "Chennai"], "MI": ["MI", "Mumbai"], "RCB": ["RCB", "Royal Challengers"],
              "KKR": ["KKR", "Kolkata"], "SRH": ["SRH", "Sunrisers"], "RR": ["RR", "Rajasthan"],
              "DC": ["DC", "Delhi"], "PBKS": ["PBKS", "Punjab"], "GT": ["GT", "Gujarat Titans"],
              "LSG": ["LSG", "Lucknow"], "DCH": ["DCH", "Deccan"], "RPS": ["RPS", "Rising Pune"]}

ORANGE_Q = ["Who won the Orange Cap in IPL {y}?", "Who scored the most runs in IPL {y}?",
            "Top run scorer of the {y} IPL season?"]
PURPLE_Q = ["Who won the Purple Cap in IPL {y}?", "Who took the most wickets in IPL {y}?",
            "Leading wicket-taker in the {y} IPL?"]
CHAMPION_Q = ["Who won IPL {y}?", "Which team won the {y} IPL title?", "IPL {y} champions?"]


def surname(name: str) -> str:
    return name.split()[-1]


def team(code: str) -> list[str]:
    return TEAM_NAMES.get(code, [code])


class Archive:
    def __init__(self, db: str):
        self.db = db

    def table(self, *argv: str, title: str | None = None) -> dict[str, Any]:
        blocks = [b for b in queries.run(list(argv), db=self.db) if b["type"] == "table"]
        block = next((b for b in blocks if title is None or b["title"].startswith(title)), blocks[0])
        return block

    def rows(self, *argv: str, title: str | None = None) -> list[dict[str, Any]]:
        block = self.table(*argv, title=title)
        return [dict(zip(block["columns"], row)) for row in block["rows"]]

    def total(self, *argv: str, title: str | None = None) -> dict[str, Any]:
        return next(r for r in self.rows(*argv, title=title) if str(r["year"]) == "Total")


def item(qid: str, question: str, category: str, route: str, tool: str | None, args: dict[str, Any],
         checks: list[list[str]], verified: str, note: str = "") -> dict[str, Any]:
    return {"id": qid, "question": question, "category": category, "expected_route": route,
            "expected_tool": tool, "expected_args": args, "checks": checks, "verified": verified,
            **({"note": note} if note else {})}


def build(db: str) -> tuple[list[dict[str, Any]], list[str]]:
    a = Archive(db)
    items: list[dict[str, Any]] = []
    problems: list[str] = []

    def check_published(label: str, expected: str, got: str) -> None:
        if expected.casefold() not in str(got).casefold():
            problems.append(f"{label}: published {expected!r}, archive {got!r}")

    # --- published: season leaders, champions, runners-up, records -------------------------
    for i, (year, (name, runs)) in enumerate(ORANGE.items()):
        top = a.rows("top-batters", "--year", str(year), "--limit", "1")[0]
        check_published(f"orange {year}", surname(name), top["player"])
        check_published(f"orange {year} runs", str(runs), str(top["runs"]))
        items.append(item(f"orange-{year}", ORANGE_Q[i % 3].format(y=year), "season_leader", "stats",
                          "batting_leaders", {"year": str(year)}, [[surname(name)], [str(runs)]], "published"))
    for i, (year, (name, wkts)) in enumerate(PURPLE.items()):
        top = a.rows("top-bowlers", "--year", str(year), "--limit", "1")[0]
        check_published(f"purple {year}", surname(name), top["player"])
        check_published(f"purple {year} wkts", str(wkts), str(top["wkts"]))
        items.append(item(f"purple-{year}", PURPLE_Q[i % 3].format(y=year), "season_leader", "stats",
                          "bowling_leaders", {"year": str(year)}, [[surname(name)], [str(wkts)]], "published"))
    finals = {r["year"]: r for r in a.rows("champions", title="IPL finals")}
    for i, (year, (champ, runner)) in enumerate(FINALS.items()):
        check_published(f"champion {year}", champ, finals[year]["champion"])
        check_published(f"runner-up {year}", runner, finals[year]["runner_up"])
        items.append(item(f"champion-{year}", CHAMPION_Q[i % 3].format(y=year), "champions", "stats",
                          "champions", {"year": str(year)}, [team(champ)], "published"))
    for year in (2011, 2016, 2019, 2023, 2025):
        items.append(item(f"runner-up-{year}", f"Who were the runners-up in IPL {year}?", "champions", "stats",
                          "champions", {"year": str(year)}, [team(FINALS[year][1])], "published"))

    records = [
        ("record-team-total", "What is the highest team total in IPL history?", ["SRH", "Sunrisers"], "287"),
        ("record-individual", "What is the highest individual score in the IPL?", ["Gayle"], "175"),
        ("record-figures", "What are the best bowling figures in IPL history?", ["Joseph"], "12"),
        ("record-fastest-fifty", "Who has the fastest fifty in the IPL?", ["Jaiswal"], "13"),
        ("record-innings-sixes", "Most sixes in a single IPL innings?", ["Gayle"], "17"),
    ]
    rec = {b["title"].split(" (")[0]: b for b in queries.run(["records", "--year", "2008-2025", "--limit", "1"], db=db)
           if b["type"] == "table"}
    first = {k: " ".join(str(v) for v in b["rows"][0]) for k, b in rec.items()}
    check_published("highest total", "287/3", first["Highest team totals"])
    check_published("highest score", "175*", first["Highest individual scores"])
    check_published("best figures", "6/12", first["Best bowling figures"])
    check_published("fastest fifty", "Jaiswal", first["Fastest fifties"])
    check_published("innings sixes", "17", first["Most sixes in an innings"])
    for qid, question, who, value in records:
        items.append(item(qid, question, "record", "stats", "records", {}, [who, [value]], "published",
                          "Unchanged through IPL 2025."))
    gayle = a.rows("top-batters", "--year", "2012", "--sort", "sixes", "--limit", "1")[0]
    check_published("sixes 2012", "59", str(gayle["6s"]))
    items.append(item("sixes-season-2012", "Who hit the most sixes in IPL 2012?", "season_leader", "stats",
                      "batting_leaders", {"year": "2012", "sort": "sixes"}, [["Gayle"], ["59"]], "published"))
    sixes = a.rows("top-batters", "--sort", "sixes", "--limit", "1")[0]
    check_published("career sixes", "Gayle", sixes["player"])
    items.append(item("sixes-career", "Who has hit the most sixes in IPL history?", "career_leader", "stats",
                      "batting_leaders", {"sort": "sixes"}, [["Gayle"], [str(sixes["6s"])]], "published",
                      "Gayle's total is fixed (retired); nobody has passed it."))
    k = a.rows("top-batters", "--year", "2008-2025", "--limit", "1")[0]
    check_published("runs to 2025", "8661", str(k["runs"]))
    items.append(item("runs-to-2025", "Who scored the most IPL runs from 2008 to 2025?", "career_leader", "stats",
                      "batting_leaders", {"year": "2008-2025"}, [["Kohli"], ["8661"]], "published"))
    c = a.rows("top-bowlers", "--year", "2008-2025", "--limit", "1")[0]
    check_published("wickets to 2025", "221", str(c["wkts"]))
    items.append(item("wickets-to-2025", "Who took the most IPL wickets between 2008 and 2025?", "career_leader",
                      "stats", "bowling_leaders", {"year": "2008-2025"}, [["Chahal"], ["221"]], "published"))

    # --- archive: answers read with the same queries Pandit uses ----------------------------
    titles = {r["team"]: r for r in a.rows("champions", title="Titles by team")}
    for code, phrasing in (("CSK", "How many IPL titles have CSK won?"), ("MI", "How many times have Mumbai Indians won the IPL?"),
                           ("KKR", "How many IPL trophies does KKR have?")):
        items.append(item(f"titles-{code}", phrasing, "champions", "stats", None, {},
                          [[str(titles[code]["titles"])]], "archive", "champions or team_seasons both answer it."))

    for code, year in (("RCB", 2025), ("MI", 2024), ("CSK", 2022), ("KKR", 2024), ("GT", 2022), ("SRH", 2016)):
        row = next(r for r in a.rows("team", code, "--year", str(year)) if str(r["year"]) == str(year))
        items.append(item(f"team-{code}-{year}", f"How many matches did {code} win in IPL {year}?", "team_season",
                          "stats", "team_seasons", {"team": code, "year": str(year)}, [[str(row["won"])]], "archive"))
    for code in ("RCB", "DC"):
        total = a.total("team", code)
        items.append(item(f"team-{code}-all", f"What is {code}'s overall win-loss record in the IPL?", "team_season",
                          "stats", "team_seasons", {"team": code}, [[str(total["won"])], [str(total["lost"])]], "archive"))

    for t1, t2, phrasing in (("MI", "CSK", "How many times has MI beaten CSK in the IPL?"),
                             ("RCB", "CSK", "RCB vs CSK head to head record"),
                             ("KKR", "MI", "KKR against MI: who has won more?"),
                             ("SRH", "RR", "Head to head between SRH and RR"),
                             ("DC", "PBKS", "How have Delhi Capitals done against Punjab Kings?")):
        total = a.total("h2h", t1, t2)
        won = [v for k, v in total.items() if k.endswith(" won")]
        items.append(item(f"h2h-{t1}-{t2}", phrasing, "head_to_head", "stats", "head_to_head",
                          {"team1": t1, "team2": t2}, [[str(won[0])], [str(won[1])]], "archive"))

    for bat, bowl in (("Virat Kohli", "Jasprit Bumrah"), ("MS Dhoni", "Rashid Khan"), ("Rohit Sharma", "Sunil Narine"),
                      ("David Warner", "Bhuvneshwar Kumar"), ("AB de Villiers", "Ravindra Jadeja"),
                      ("Shubman Gill", "Yuzvendra Chahal")):
        total = a.total("matchup", bat, bowl)
        items.append(item(f"matchup-{surname(bat)}-{surname(bowl)}".lower(),
                          f"How has {bat} fared against {bowl}?", "matchup", "stats", "batter_vs_bowler",
                          {}, [[str(total["runs"])], [str(total["balls"])]], "archive"))

    for ground, query, phrasing, column in (
            ("Wankhede", "wankhede", "What is the average first innings score at the Wankhede?", "avg_1st_inns"),
            ("Chinnaswamy", "chinnaswamy", "Average first-innings total at Chinnaswamy Stadium?", "avg_1st_inns"),
            ("Eden", "eden", "How many matches have been won chasing at Eden Gardens?", "chasing_won"),
            ("Chepauk", "chidambaram", "How often does the team batting first win at Chepauk?", "bat_first_won"),
            ("Arun Jaitley", "kotla", "Highest IPL total at the Kotla?", "highest_total")):
        row = a.rows("venues", query, "--limit", "1")[0]
        items.append(item(f"venue-{query}", phrasing, "venue", "stats", "venue_stats", {},
                          [[str(row[column])]], "archive"))

    for phase, role, year, phrasing in (("death", "bowl", 2024, "Best death-overs bowler by economy in IPL 2024?"),
                                        ("powerplay", "bat", 2025, "Who had the best powerplay strike rate in IPL 2025?"),
                                        ("death", "bat", 2024, "Best death-overs batter in IPL 2024?"),
                                        ("powerplay", "bowl", 2023, "Most economical powerplay bowler of IPL 2023?"),
                                        ("middle", "bowl", 2022, "Best middle-overs bowler in IPL 2022?")):
        row = a.rows("phase", phase, "--role", role, "--year", str(year), "--limit", "1")[0]
        items.append(item(f"phase-{phase}-{role}-{year}", phrasing, "phase", "stats", "phase_leaders",
                          {"phase": phase, "role": role, "year": str(year)}, [[surname(row["player"])]], "archive"))

    for name, role, phrasing in (("Virat Kohli", "bat", "How many IPL runs has Virat Kohli scored?"),
                                 ("MS Dhoni", "bat", "MS Dhoni's IPL career runs?"),
                                 ("Jasprit Bumrah", "bowl", "How many IPL wickets does Jasprit Bumrah have?"),
                                 ("Sunil Narine", "bowl", "Sunil Narine IPL wickets in total?"),
                                 ("Rashid Khan", "bowl", "How many wickets has Rashid Khan taken in the IPL?"),
                                 ("Shubman Gill", "bat", "Shubman Gill's IPL batting record?"),
                                 ("Rishabh Pant", "bat", "Rishabh Pant career stats in the IPL"),
                                 ("Ravindra Jadeja", "bowl", "Ravindra Jadeja's IPL bowling numbers?")):
        title = f"{a.rows('players', name)[0]['player']}: {'batting' if role == 'bat' else 'bowling'} by year"
        total = a.total("player", name, title=title)
        value = total["runs"] if role == "bat" else total["wkts"]
        items.append(item(f"player-{surname(name).lower()}-{role}", phrasing, "player", "stats", "player_profile",
                          {}, [[surname(name)], [str(value)]], "archive"))

    for qid, question, argv, sort_col, args in (
            ("sixes-2024", "Who hit the most sixes in IPL 2024?", ("top-batters", "--year", "2024", "--sort", "sixes"), "6s",
             {"year": "2024", "sort": "sixes"}),
            ("csk-runs-2023", "Who was CSK's top run-scorer in IPL 2023?", ("top-batters", "--year", "2023", "--team", "CSK"),
             "runs", {"year": "2023", "team": "CSK"}),
            ("mi-wickets-all", "Who has taken the most wickets for Mumbai Indians?", ("top-bowlers", "--team", "MI"), "wkts",
             {"team": "MI"}),
            ("hundreds-all", "Who has the most hundreds in IPL history?", ("top-batters", "--sort", "hundreds"), "100s",
             {"sort": "hundreds"}),
            ("economy-2024", "Which bowler had the best economy in IPL 2024?", ("top-bowlers", "--year", "2024", "--sort", "econ"),
             "econ", {"year": "2024", "sort": "econ"}),
            ("fifties-2022", "Who scored the most fifties in IPL 2022?", ("top-batters", "--year", "2022", "--sort", "fifties"),
             "50s", {"year": "2022", "sort": "fifties"}),
            ("fours-2023", "Most fours in IPL 2023?", ("top-batters", "--year", "2023", "--sort", "fours"), "4s",
             {"year": "2023", "sort": "fours"}),
            ("rcb-wickets-2025", "Who took the most wickets for RCB in 2025?", ("top-bowlers", "--year", "2025", "--team", "RCB"),
             "wkts", {"year": "2025", "team": "RCB"})):
        row = a.rows(*argv, "--limit", "1")[0]
        tool = "batting_leaders" if argv[0] == "top-batters" else "bowling_leaders"
        items.append(item(qid, question, "leaderboard", "stats", tool, args,
                          [[surname(row["player"])], [str(row[sort_col])]], "archive"))

    seasons = a.rows("seasons")
    fastest = max((r for r in seasons if r["run_rate"]), key=lambda r: r["run_rate"])
    items.append(item("season-run-rate", "Which IPL season had the highest run rate?", "season", "stats",
                      "season_summary", {}, [[str(fastest["year"])], [str(fastest["run_rate"])]], "archive"))
    s2024 = next(r for r in seasons if r["year"] == 2024)
    items.append(item("season-sixes-2024", "How many sixes were hit in IPL 2024?", "season", "stats",
                      "season_summary", {"year": "2024"}, [[str(s2024["sixes"])]], "archive"))

    # --- match path: must not be answered from the stats archive -----------------------------
    for i, question in enumerate(("Who won the 2016 IPL final?", "Show me the scorecard of the 2019 final",
                                  "Where did the momentum shift in the 2025 final?",
                                  "Ball by ball of the 2017 final", "What happened in the last over of the 2019 final?",
                                  "RCB vs PBKS 2025 final result", "Who was player of the match in the 2023 final?",
                                  "How did CSK chase in the 2018 final?", "Show the 2024 final between KKR and SRH",
                                  "Turning point of the 2008 final")):
        items.append(item(f"match-{i + 1:02d}", question, "match", "match", None, {}, [], "n/a",
                          "Single-match question; belongs to the match path."))

    # --- ambiguous names: the answer should ask which player ----------------------------------
    items.append(item("ambiguous-sharma", "How did Sharma do in 2024?", "ambiguous", "stats", "player_profile", {},
                      [["Rohit Sharma"], ["Mohit Sharma", "Sandeep Sharma", "Ishant Sharma"]], "n/a",
                      "Several Sharmas; Pandit should ask which one."))
    items.append(item("ambiguous-singh", "What are Singh's IPL stats?", "ambiguous", "stats", "player_profile", {},
                      [["which", "Which", "mean"]], "n/a", "Many Singhs; Pandit should ask which one."))
    return items, problems


def build_heldout(db: str) -> list[dict[str, Any]]:
    """New wording, written after the rules were tuned and never used to tune them: an honest test."""
    a = Archive(db)
    items: list[dict[str, Any]] = []

    def add(qid: str, question: str, category: str, tool: str | None, checks: list[list[str]], verified: str,
            route: str = "stats") -> None:
        items.append(item(f"ho-{qid}", question, category, route, tool, {}, checks, verified))

    for qid, q, year, kind in (("orange-2014", "who topped the batting charts in 2014?", 2014, "bat"),
                               ("orange-2019", "orange cap holder 2019", 2019, "bat"),
                               ("orange-2010", "most runs ipl 2010 season", 2010, "bat"),
                               ("purple-2021", "highest wicket taker ipl 2021", 2021, "bowl"),
                               ("purple-2018", "which bowler grabbed the most scalps in the 2018 ipl", 2018, "bowl"),
                               ("purple-2012", "Purple cap 2012?", 2012, "bowl")):
        name, value = (ORANGE if kind == "bat" else PURPLE)[year]
        add(qid, q, "season_leader", "batting_leaders" if kind == "bat" else "bowling_leaders",
            [[surname(name)], [str(value)]], "published")
    for qid, q, year, who in (("champ-2014", "which franchise lifted the trophy in 2014?", 2014, 0),
                              ("champ-2021", "IPL 2021 winner", 2021, 0),
                              ("runner-2024", "who finished runner up in the 2024 ipl", 2024, 1),
                              ("champ-2009", "who were the champions in 2009", 2009, 0)):
        add(qid, q, "champions", "champions", [team(FINALS[year][who])], "published")
    add("rec-total", "biggest score by a team in IPL", "record", "records", [["287"]], "published")
    add("rec-figures", "best figures ever in an ipl game", "record", "records", [["Joseph"], ["12"]], "published")
    add("rec-score", "highest score by a batter in ipl history", "record", "records", [["Gayle"], ["175"]], "published")
    add("kohli-2016", "Kohli's numbers in 2016", "player", "player_profile", [["973"]], "published")

    def lead(qid: str, q: str, argv: tuple[str, ...], col: str, tool: str) -> None:
        row = a.rows(*argv, "--limit", "1")[0]
        add(qid, q, "leaderboard", tool, [[surname(row["player"])], [str(row[col])]], "archive")
    lead("maximums-2024", "who's hit the most maximums in IPL 2024", ("top-batters", "--year", "2024", "--sort", "sixes"),
         "6s", "batting_leaders")
    lead("centuries", "most centuries in ipl", ("top-batters", "--sort", "hundreds"), "100s", "batting_leaders")
    lead("econ-2023", "most economical bowler ipl 2023", ("top-bowlers", "--year", "2023", "--sort", "econ"), "econ",
         "bowling_leaders")
    lead("csk-wkts", "top wicket taker for csk all time", ("top-bowlers", "--team", "CSK"), "wkts", "bowling_leaders")
    lead("kkr-runs-2024", "leading run getter for kkr in 2024", ("top-batters", "--year", "2024", "--team", "KKR"), "runs",
         "batting_leaders")

    def career(qid: str, q: str, name: str, role: str, col: str) -> None:
        player = a.rows("players", name)[0]["player"]
        total = a.total("player", name, title=f"{player}: {'batting' if role == 'bat' else 'bowling'} by year")
        add(qid, q, "player", "player_profile", [[str(total[col])]], "archive")
    career("bumrah-econ", "what's bumrah's economy in the ipl", "Jasprit Bumrah", "bowl", "econ")
    career("rohit-runs", "how many runs has rohit sharma made in ipl", "Rohit Sharma", "bat", "runs")
    career("russell-sr", "andre russell ipl strike rate", "Andre Russell", "bat", "sr")
    career("chahal-wkts", "chahal wickets total", "Yuzvendra Chahal", "bowl", "wkts")

    for qid, q, bat, bowl in (("dhoni-bumrah", "dhoni vs bumrah", "MS Dhoni", "Jasprit Bumrah"),
                              ("warner-rashid", "how does warner go against rashid khan", "David Warner", "Rashid Khan"),
                              ("pollard-narine", "pollard against narine", "Kieron Pollard", "Sunil Narine")):
        total = a.total("matchup", bat, bowl)
        never = [["0", "never", "not faced", "no balls", "hasn't faced", "no record"]]
        add(qid, q, "matchup", "batter_vs_bowler", [[str(total["runs"])]] if total["balls"] else never, "archive")
    for qid, q, t1, t2 in (("rcb-kkr", "rcb v kkr all time results", "RCB", "KKR"),
                           ("csk-mi", "csk vs mi, who leads the rivalry?", "CSK", "MI")):
        total = a.total("h2h", t1, t2)
        add(qid, q, "head_to_head", "head_to_head", [[str(v)] for k, v in total.items() if k.endswith(" won")], "archive")
    for qid, q, code, year in (("gt-2023", "how many games did gujarat titans win in 2023", "GT", 2023),
                               ("pbks-2025", "punjab kings record in 2025", "PBKS", 2025),
                               ("lsg-2024", "lsg 2024 season", "LSG", 2024)):
        row = next(r for r in a.rows("team", code, "--year", str(year)) if str(r["year"]) == str(year))
        add(qid, q, "team_season", "team_seasons", [[str(row["won"])]], "archive")
    eden = a.rows("venues", "eden", "--limit", "1")[0]
    add("eden-chase", "is eden gardens a good chasing ground? how many wins chasing", "venue", "venue_stats",
        [[str(eden["chasing_won"])]], "archive")
    wank = a.rows("venues", "wankhede", "--limit", "1")[0]
    add("wankhede-avg", "average 1st innings score wankhede", "venue", "venue_stats", [[str(wank["avg_1st_inns"])]], "archive")
    chin = a.rows("venues", "chinnaswamy", "--limit", "1")[0]
    add("chinnaswamy-high", "highest total at chinnaswamy", "venue", "venue_stats", [[str(chin["highest_total"])]], "archive")
    pp = a.rows("phase", "powerplay", "--role", "bowl", "--year", "2024", "--limit", "1")[0]
    add("pp-bowl-2024", "who's the best powerplay bowler of 2024", "phase", "phase_leaders", [[surname(pp["player"])]], "archive")
    dh = a.rows("phase", "death", "--role", "bat", "--year", "2025", "--limit", "1")[0]
    add("death-bat-2025", "best death overs hitter 2025", "phase", "phase_leaders", [[surname(dh["player"])]], "archive")
    s23 = next(r for r in a.rows("seasons") if r["year"] == 2023)
    add("sixes-2023", "how many sixes were there in ipl 2023 overall", "season", "season_summary", [[str(s23["sixes"])]],
        "archive")
    for i, q in enumerate(("scorecard for csk v gt 2023 final", "how did rcb lose the 2016 final",
                           "show me the eliminator in 2024", "commentary from the last over of the 2019 final",
                           "who was man of the match in the 2022 final")):
        add(f"match-{i + 1}", q, "match", None, [], "n/a", route="match")
    add("patel", "patel's ipl wickets", "ambiguous", "player_profile", [["Axar Patel", "Harshal Patel", "Munaf Patel"]], "n/a")
    return items


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None)
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)
    items, problems = build(a.db or db_path())
    if problems:
        raise SystemExit("Published answers disagree with the archive:\n  " + "\n  ".join(problems))
    with open(a.out, "w", encoding="utf-8") as fh:
        for row in items:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    heldout = build_heldout(a.db or db_path())
    with open(HELDOUT, "w", encoding="utf-8") as fh:
        for row in heldout:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {len(heldout)} held-out questions to {HELDOUT}")
    counts: dict[str, int] = {}
    for row in items:
        counts[row["verified"]] = counts.get(row["verified"], 0) + 1
    print(f"wrote {len(items)} questions to {a.out}: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
