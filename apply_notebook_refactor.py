"""
Applies the scorecard refactor to Criketmatchbot.ipynb.

Run this from the repo root, next to the notebook:

    python apply_notebook_refactor.py

It removes the superseded scorecard cells and rewires the surviving ones to
import from scorecard_helpers.py / scorecard_renderer.py. A backup is written
to Criketmatchbot.ipynb.bak before anything is changed.

The script matches cells by their content, not by index, so it stays correct
even if you have added or moved cells since.
"""

import json
import shutil
import sys

NOTEBOOK = "Criketmatchbot.ipynb"
BACKUP = NOTEBOOK + ".bak"

# Cells to delete, identified by a distinctive substring of their source.
DELETE_MARKERS = [
    "def compact_match_summary(",
    "def estimate_bowler_runs(",
    "### Check Commentary sample and include it with scorecard",
]

# The renderer demo cell replaces every remaining print_compact_scorecard cell.
RENDERER_CELL = """\
# Compact scorecard via the shared modules
from scorecard_renderer import build_compact_scorecard

compact = build_compact_scorecard(info, scorecard, top_n=3, max_chars=1900)
print(compact)
print(f"\\n[{len(compact)} chars]")
"""

COMMENTARY_CELL = '''\
# Commentary sample via the shared helpers
from scorecard_helpers import (
    extract_commentary_text,
    extract_player_name_from_id,
    extract_over,
)


def print_commentary_sample(commentary, limit=10):
    comments = commentary.get("content", {}).get("comments", [])
    printed = 0

    print("\\n\U0001F3CF COMMENTARY SAMPLE\\n")

    for c in comments:
        text = extract_commentary_text(c)
        if not text:
            continue

        over = extract_over(c)
        runs = c.get("totalRuns", "-")
        batter = extract_player_name_from_id(c, "batsmanPlayerId")
        bowler = extract_player_name_from_id(c, "bowlerPlayerId")

        flags = []
        if c.get("isFour"):
            flags.append("4")
        if c.get("isSix"):
            flags.append("6")
        if c.get("isWicket"):
            flags.append("W")
        flag_text = f" [{' '.join(flags)}]" if flags else ""

        print(f"Over {over} | {bowler} to {batter} | Runs: {runs}{flag_text}")
        print(text)
        print("-" * 100)

        printed += 1
        if printed >= limit:
            break

    if printed == 0:
        print("No readable commentary found.")


print_commentary_sample(commentary, limit=10)
'''


def make_cell(source):
    """Build a fresh code cell with no stored output."""
    lines = source.splitlines(keepends=True)
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": lines,
    }


def cell_text(cell):
    return "".join(cell.get("source", []))


def main():
    try:
        with open(NOTEBOOK, encoding="utf-8") as f:
            nb = json.load(f)
    except FileNotFoundError:
        sys.exit(f"{NOTEBOOK} not found. Run this from the repo root.")

    shutil.copyfile(NOTEBOOK, BACKUP)

    original_count = len(nb["cells"])
    new_cells = []
    renderer_done = False
    commentary_done = False
    deleted = 0

    for cell in nb["cells"]:
        text = cell_text(cell)

        if any(marker in text for marker in DELETE_MARKERS):
            deleted += 1
            continue

        # Collapse every print_compact_scorecard variant into one demo cell.
        if "def print_compact_scorecard(" in text:
            if renderer_done:
                deleted += 1
                continue
            new_cells.append(make_cell(RENDERER_CELL))
            renderer_done = True
            continue

        # Collapse every print_commentary_sample variant into one demo cell.
        if "def print_commentary_sample(" in text:
            if commentary_done:
                deleted += 1
                continue
            new_cells.append(make_cell(COMMENTARY_CELL))
            commentary_done = True
            continue

        new_cells.append(cell)

    nb["cells"] = new_cells

    with open(NOTEBOOK, "w", encoding="utf-8") as f:
        json.dump(nb, f, indent=1)
        f.write("\n")

    print(f"Backup written to {BACKUP}")
    print(f"Cells: {original_count} -> {len(new_cells)} ({deleted} removed)")
    print(f"Renderer cell rewired: {renderer_done}")
    print(f"Commentary cell rewired: {commentary_done}")


if __name__ == "__main__":
    main()
