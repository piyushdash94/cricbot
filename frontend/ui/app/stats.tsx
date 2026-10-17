"use client";

// Stats workspace: season and all-time leaders, records, phases, player profiles, matchups, teams
// and grounds, served by /api/stats/* (backend/apis/stats.py) over the local DuckDB archive.
// Every endpoint returns the same "blocks" (tables or notes), so one renderer covers them all.

import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

type Cell = string | number | null;
type Block = { type: "table"; title: string; columns: string[]; rows: Cell[][] } | { type: "text"; text: string };
type StatsStatus = { available: boolean; hint?: string; sources?: { cricsheet: number; espn: number }; years?: number[] };
type StatsTab = "leaders" | "records" | "phases" | "players" | "matchups" | "teams" | "grounds";
type Phase = "powerplay" | "middle" | "death";

const TABS: { id: StatsTab; label: string }[] = [
  { id: "leaders", label: "Leaders" },
  { id: "records", label: "Records" },
  { id: "phases", label: "Phases" },
  { id: "players", label: "Players" },
  { id: "matchups", label: "Matchups" },
  { id: "teams", label: "Teams" },
  { id: "grounds", label: "Grounds" },
];
// Tabs whose tables are already season by season: they always cover every season.
const ALL_SEASON_TABS: StatsTab[] = ["matchups", "teams", "grounds"];
const BAT_SORTS: [string, string][] = [["runs", "Runs"], ["sixes", "Sixes"], ["fours", "Fours"], ["sr", "Strike rate"],
  ["avg", "Average"], ["fifties", "Fifties"], ["hundreds", "Hundreds"], ["hs", "Highest score"]];
const BOWL_SORTS: [string, string][] = [["wkts", "Wickets"], ["econ", "Economy"], ["avg", "Average"], ["sr", "Strike rate"],
  ["4w", "4-wicket hauls"]];
const TEAMS = ["CSK", "DC", "GT", "KKR", "LSG", "MI", "PBKS", "RCB", "RR", "SRH", "DCH", "GL", "KTK", "PWI", "RPS"];
const LABELS: Record<string, string> = {
  player: "Player", team: "Team", runs: "R", inns: "Inns", outs: "Out", avg: "Avg", sr: "SR", hs: "HS", wkts: "W",
  overs: "O", econ: "Econ", best: "Best", balls: "B", dot_pct: "Dot %", boundaries: "4s+6s", year: "Year",
  score: "Score", figures: "Figures", match: "Match", match_id: "Match id", balls_to_50: "Balls to 50", played: "P",
  no_result: "NR", phase: "Phase", first: "First", last: "Last", bat_inns: "Bat inns", bowl_inns: "Bowl inns", player_id: "Id",
  won: "W", lost: "L", win_pct: "Win %", finish: "Finish", champion: "Champion", runner_up: "Runner-up", result: "Result",
  ground: "Ground", titles: "Titles", years: "Years", avg_1st_inns: "Avg 1st inns", bat_first_won: "Won bat 1st",
  chasing_won: "Won chasing", highest_total: "Highest", run_rate: "RR", matches: "M", with_balls: "With balls",
  avg_1st: "Avg 1st", sixes: "6s", "4w+": "4w+", "4s": "4s", "6s": "6s", "50s": "50s", "100s": "100s",
  league_1st: "League 1st", vs_league: "vs league", league_rr: "League RR", chase_win_pct: "Chase win %",
  boundary_pct: "Bdry %", league_boundary_pct: "League bdry %", league_dot_pct: "League dot %",
  wkts_per_inns: "W / inns", league_wkts: "League W", dismissal: "How out", pct: "%", league_pct: "League %",
  fours: "4s",
};

async function getBlocks(path: string): Promise<Block[]> {
  const response = await fetch(`${API_BASE}${path}`);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `Stats request failed (${response.status})`);
  }
  return (await response.json()).blocks as Block[];
}

function cell(value: Cell) {
  return value === null || value === "" ? "–" : String(value);
}

// ------------------------------------------------------------------ building blocks

// Leaderboard cards are narrow: keep the columns a reader scans for (the full set is in the API).
const COMPACT_COLUMNS = ["player", "runs", "wkts", "avg", "sr", "econ", "hs", "best", "6s", "4s", "50s", "100s", "4w+"];

function compactBlock(block: Extract<Block, { type: "table" }>) {
  const keep = block.columns.map((column, index) => [column, index] as const).filter(([column]) => COMPACT_COLUMNS.includes(column));
  return { ...block, columns: keep.map(([column]) => column), rows: block.rows.map((row) => keep.map(([, index]) => row[index])) };
}

function BlockCard({ block: raw, onPlayer, compact }: { block: Block; onPlayer?: (name: string) => void; compact?: boolean }) {
  if (raw.type === "text") return <p className="stats-note">{raw.text}</p>;
  const block = compact ? compactBlock(raw) : raw;
  const nameColumn = block.columns[0] === "player" ? 0 : -1;
  return <article className={`detail-card stats-card${compact ? " compact" : ""}`}>
    <div className="card-title"><h3>{block.title}</h3></div>
    {block.rows.length === 0
      ? <p className="stats-note">No rows for this selection.</p>
      : <div className="table-wrap"><table>
        <thead><tr>{block.columns.map((column) => <th key={column}>{LABELS[column] ?? column}</th>)}</tr></thead>
        <tbody>{block.rows.map((row, index) => <tr key={index}>{row.map((value, col) => <td key={col}>
          {col === nameColumn && onPlayer && typeof value === "string"
            ? <button className="stats-player" onClick={() => onPlayer(value)}>{value}</button>
            : col === 0 ? <strong>{cell(value)}</strong> : col === 1 ? <b>{cell(value)}</b> : cell(value)}
        </td>)}</tr>)}</tbody>
      </table></div>}
  </article>;
}

// Results are keyed by the path they answer, so "loading" is derived: the previous table stays
// visible (dimmed) until the new one arrives, and no state is set synchronously in the effect.
function useBlocks(path: string | null) {
  const [result, setResult] = useState<{ path: string | null; blocks: Block[] | null; error: string }>({ path: null, blocks: null, error: "" });
  useEffect(() => {
    if (!path) return;
    let live = true;
    getBlocks(path)
      .then((blocks) => { if (live) setResult({ path, blocks, error: "" }); })
      .catch((err: Error) => { if (live) setResult({ path, blocks: null, error: err.message }); });
    return () => { live = false; };
  }, [path]);
  const loading = path !== null && result.path !== path;
  return { blocks: result.blocks, error: loading ? "" : result.error, loading };
}

function Blocks({ path, onPlayer, compact }: { path: string | null; onPlayer?: (name: string) => void; compact?: boolean }) {
  const { blocks, error, loading } = useBlocks(path);
  if (loading && !blocks) return <div className="detail-loading"><span /><strong>Crunching the numbers…</strong></div>;
  if (error) return <div className="error-state"><strong>Stats unavailable</strong><p>{error}</p></div>;
  return <div className={`scorecard-stack${loading ? " stats-refreshing" : ""}`}>{blocks?.map((block, index) => <BlockCard key={index} block={block} onPlayer={onPlayer} compact={compact} />)}</div>;
}

// ------------------------------------------------------------------ chat card

// Pandit's stats answers carry the tables they were built from, so every number in the reply
// can be checked in place. Long tables start collapsed.
export type StatsCard = {
  type: "stats"; title: string; question: string; blocks: Block[];
  calls: { name: string; args: Record<string, unknown>; command: string; error: string | null }[];
};
const CHAT_ROWS = 5;

function ChatTable({ block }: { block: Extract<Block, { type: "table" }> }) {
  const [open, setOpen] = useState(false);
  // Year-by-year tables end with a Total row: collapsed, show the latest seasons and the total.
  const totalled = String(block.rows[block.rows.length - 1]?.[0]) === "Total";
  const rows = open ? block.rows : totalled ? block.rows.slice(-CHAT_ROWS) : block.rows.slice(0, CHAT_ROWS);
  const ranked = ["player", "team", "ground"].includes(block.columns[0]) && block.rows.length > 1;
  return <div className="chat-stats-table">
    <h4>{block.title}</h4>
    {block.rows.length === 0 ? <p className="stats-note">No rows.</p> : <div className="table-wrap"><table className={ranked ? "ranked" : undefined}>
      <thead><tr>{ranked && <th>#</th>}{block.columns.filter((c) => c !== "match_id").map((column) => <th key={column}>{LABELS[column] ?? column}</th>)}</tr></thead>
      <tbody>{rows.map((row, index) => <tr key={index}>{ranked && <td>{index + 1}</td>}
        {row.filter((_, col) => block.columns[col] !== "match_id").map((value, col) => <td key={col}>{col === 0 ? <strong>{cell(value)}</strong> : cell(value)}</td>)}</tr>)}</tbody>
    </table></div>}
    {block.rows.length > CHAT_ROWS && <button className="chat-stats-more" onClick={() => setOpen(!open)}>{open ? "Show fewer" : `Show all ${block.rows.length} rows`}</button>}
  </div>;
}

export function StatsChatCard({ card, onOpenStats }: { card: StatsCard; onOpenStats?: () => void }) {
  return <article className="chat-card chat-stats">
    {card.blocks.map((block, index) => block.type === "text"
      ? <p key={index} className="stats-note">{block.text}</p>
      : <ChatTable key={index} block={block} />)}
    <footer>
      <small title={card.calls.map((call) => call.command).join("\n")}>From the ball-by-ball archive · {card.calls.map((call) => call.name.replace(/_/g, " ")).join(", ")}</small>
      {onOpenStats && <div><button onClick={onOpenStats}>Open Stats</button></div>}
    </footer>
  </article>;
}

// An ambiguous name comes back as choices; each re-asks the question about one player.
export type ClarifyCard = { type: "clarify"; title: string; name: string; options: { label: string; question: string }[] };
// A question outside the archive (WPL, Tests, other leagues...): say so, offer IPL questions.
export type ScopeCard = { type: "scope"; title: string; topic: string; suggestions: string[] };

export function ClarifyChatCard({ card, onAsk, disabled }: { card: ClarifyCard; onAsk: (question: string) => void; disabled?: boolean }) {
  return <article className="chat-card chat-choice" aria-label={card.title}>
    <h4>{card.title}</h4>
    <div className="chat-chips">{card.options.map((option) =>
      <button key={option.label} disabled={disabled} onClick={() => onAsk(option.question)}>{option.label}</button>)}</div>
  </article>;
}

export function ScopeChatCard({ card, onAsk, disabled }: { card: ScopeCard; onAsk: (question: string) => void; disabled?: boolean }) {
  return <article className="chat-card chat-scope">
    <h4><span aria-hidden="true">⌀</span>{card.title}<small>{card.topic}</small></h4>
    <p className="stats-note">Try one of these instead:</p>
    <div className="chat-chips">{card.suggestions.map((question) =>
      <button key={question} disabled={disabled} onClick={() => onAsk(question)}>{question}</button>)}</div>
  </article>;
}

// ------------------------------------------------------------------ panel

export function StatsPanel({ season, seasons, matchOpen, onBackToMatch, onClose }: {
  season: number; seasons: number[]; matchOpen: boolean; onBackToMatch: () => void; onClose: () => void;
}) {
  const [status, setStatus] = useState<StatsStatus | null>(null);
  const [tab, setTab] = useState<StatsTab>("leaders");
  const [yearChoice, setYear] = useState<string | null>(null);   // null: follow the rail's season
  const year = yearChoice ?? String(season);
  const [phase, setPhase] = useState<Phase>("death");
  const [role, setRole] = useState<"bat" | "bowl">("bowl");
  const [playerQuery, setPlayerQuery] = useState("");
  const [playerSearch, setPlayerSearch] = useState("");
  const [playerId, setPlayerId] = useState<number | null>(null);
  const [team1, setTeam1] = useState("MI");
  const [team2, setTeam2] = useState("");          // "": season by season; a code: head to head
  const [batSort, setBatSort] = useState("runs");
  const [bowlSort, setBowlSort] = useState("wkts");
  const [leaderTeam, setLeaderTeam] = useState("");
  const [batterInput, setBatterInput] = useState("Virat Kohli");
  const [bowlerInput, setBowlerInput] = useState("Jasprit Bumrah");
  const [pair, setPair] = useState<[string, string]>(["Virat Kohli", "Jasprit Bumrah"]);
  const [ground, setGround] = useState("Wankhede Stadium");

  useEffect(() => {
    fetch(`${API_BASE}/api/stats/status`).then((r) => r.ok ? r.json() : Promise.reject())
      .then(setStatus).catch(() => setStatus({ available: false, hint: "The stats API is not reachable." }));
  }, []);

  const yearParam = year === "all" ? "" : `year=${year}&`;
  const everySeason = ALL_SEASON_TABS.includes(tab);
  const span = everySeason ? "Every season" : year === "all" ? "All time" : `IPL ${year}`;
  const teamParam = leaderTeam ? `team=${leaderTeam}&` : "";
  const years = status?.years?.length ? [...status.years].reverse() : seasons;

  // A leaderboard name opens that player's profile (exact name first, else the closest match).
  const openPlayer = useCallback(async (name: string) => {
    setTab("players");
    setPlayerQuery(name);
    setPlayerSearch(name);
    try {
      const rows = (await getBlocks(`/api/stats/players?q=${encodeURIComponent(name)}`))[0];
      if (rows?.type === "table" && rows.rows.length) {
        const exact = rows.rows.find((row) => String(row[1]).toLowerCase() === name.toLowerCase()) ?? rows.rows[0];
        setPlayerId(Number(exact[0]));
      }
    } catch { /* the search list shows the error */ }
  }, []);

  function submitPlayer(event: FormEvent) {
    event.preventDefault();
    setPlayerId(null);
    setPlayerSearch(playerQuery.trim());
  }

  if (status && !status.available) {
    return <div className="stats-panel"><div className="stats-hero"><div className="hero-actions"><button className="back-link" onClick={onClose}>← Close</button></div></div><div className="workspace-empty">
      <span>∑</span><h3>Stats archive not built yet</h3>
      <p>Run this once in the project root (about 20 seconds), then reload:</p>
      <code className="stats-command">python -m backend.db.cricsheet_load --sync</code>
      <p>Or pick a match from the archive to explore it.</p>
    </div></div>;
  }

  return <div className="stats-panel">
    <div className="stats-hero">
      <div className="hero-actions">
        <button className="back-link" onClick={onClose}>← Close</button>
        {matchOpen && <button className="share-button" onClick={onBackToMatch}>Back to match</button>}
      </div>
      <p className="section-kicker">IPL STATS</p>
      <h2>{span}</h2>
      <div className="stats-year">
        <label htmlFor="stats-year">Season</label>
        <select id="stats-year" value={everySeason ? "all" : year} disabled={everySeason} title={everySeason ? "These tables are already season by season" : undefined} onChange={(event) => setYear(event.target.value)}>
          <option value="all">All time</option>
          {years.map((y) => <option key={y} value={String(y)}>{y}</option>)}
        </select>
        {status?.sources && <span className="coverage">{status.sources.cricsheet.toLocaleString()} matches · Cricsheet{status.sources.espn ? ` + ${status.sources.espn} ESPN` : ""}</span>}
      </div>
    </div>
    <div className="detail-tabs" role="tablist" aria-label="Stats sections">{TABS.map((t) =>
      <button role="tab" aria-selected={tab === t.id} className={tab === t.id ? "active" : ""} key={t.id} onClick={() => setTab(t.id)}>{t.label}</button>)}
    </div>

    {tab === "phases" && <div className="innings-filter">
      {(["powerplay", "middle", "death"] as Phase[]).map((p) => <button key={p} className={phase === p ? "active" : ""} onClick={() => setPhase(p)}>{p[0].toUpperCase() + p.slice(1)}</button>)}
      <span className="filter-gap" />
      <button className={role === "bat" ? "active" : ""} onClick={() => setRole("bat")}>Batting</button>
      <button className={role === "bowl" ? "active" : ""} onClick={() => setRole("bowl")}>Bowling</button>
    </div>}
    {tab === "leaders" && <div className="innings-filter stats-controls">
      <label>Batting by <select aria-label="Rank batters by" value={batSort} onChange={(e) => setBatSort(e.target.value)}>{BAT_SORTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
      <label>Bowling by <select aria-label="Rank bowlers by" value={bowlSort} onChange={(e) => setBowlSort(e.target.value)}>{BOWL_SORTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
      <label>For <select aria-label="Team" value={leaderTeam} onChange={(e) => setLeaderTeam(e.target.value)}><option value="">All teams</option>{TEAMS.map((t) => <option key={t}>{t}</option>)}</select></label>
    </div>}
    {tab === "teams" && <div className="innings-filter stats-h2h">
      <select aria-label="Team" value={team1} onChange={(e) => { setTeam1(e.target.value); if (e.target.value === team2) setTeam2(""); }}><option value="">All teams</option>{TEAMS.map((t) => <option key={t}>{t}</option>)}</select>
      {team1 && <><span>against</span>
        <select aria-label="Opponent" value={team2} onChange={(e) => setTeam2(e.target.value)}><option value="">Season by season</option>{TEAMS.filter((t) => t !== team1).map((t) => <option key={t}>{t}</option>)}</select></>}
    </div>}
    {tab === "matchups" && <form className="innings-filter stats-search stats-pair" onSubmit={(e) => { e.preventDefault(); setPair([batterInput.trim(), bowlerInput.trim()]); }}>
      <input value={batterInput} onChange={(e) => setBatterInput(e.target.value)} placeholder="Batter" aria-label="Batter" />
      <span>v</span>
      <input value={bowlerInput} onChange={(e) => setBowlerInput(e.target.value)} placeholder="Bowler" aria-label="Bowler" />
      <button className="active" disabled={batterInput.trim().length < 2 || bowlerInput.trim().length < 2}>Compare</button>
    </form>}
    {tab === "grounds" && <GroundPicker value={ground} onChange={setGround} />}
    {tab === "players" && <form className="innings-filter stats-search" onSubmit={submitPlayer}>
      <input value={playerQuery} onChange={(e) => setPlayerQuery(e.target.value)} placeholder="Search a player: Bumrah, Gayle, Kohli…" aria-label="Search players" />
      <button className="active" disabled={playerQuery.trim().length < 2}>Search</button>
    </form>}

    <div className="workspace-body">
      {tab === "leaders" && <div className="overview-grid">
        <Blocks path={`/api/stats/batting?${yearParam}${teamParam}sort=${batSort}&limit=10`} onPlayer={openPlayer} compact />
        <Blocks path={`/api/stats/bowling?${yearParam}${teamParam}sort=${bowlSort}&limit=10`} onPlayer={openPlayer} compact />
      </div>}
      {tab === "records" && <Blocks path={`/api/stats/records?${yearParam}limit=5`} onPlayer={openPlayer} />}
      {tab === "phases" && <Blocks path={`/api/stats/phase?phase=${phase}&role=${role}&${yearParam}min_balls=${year === "all" ? 300 : 60}&limit=15`} onPlayer={openPlayer} />}
      {tab === "players" && (playerId !== null
        ? <><button className="back-link stats-back" onClick={() => setPlayerId(null)}>← All results</button>
          <Blocks path={`/api/stats/players/${playerId}${year === "all" ? "" : `?year=${year}`}`} /></>
        : playerSearch.length >= 2
          ? <PlayerResults query={playerSearch} onPick={setPlayerId} />
          : <div className="empty-state"><strong>Find a player</strong><p>Season-by-season numbers and powerplay / middle / death splits. Tip: click any name in Leaders.</p></div>)}
      {tab === "matchups" && <Blocks path={`/api/stats/matchup?batter=${encodeURIComponent(pair[0])}&bowler=${encodeURIComponent(pair[1])}`} />}
      {tab === "teams" && <Blocks path={!team1 ? "/api/stats/champions" : team2 ? `/api/stats/h2h?team1=${team1}&team2=${team2}` : `/api/stats/teams/${team1}`} />}
      {tab === "grounds" && <GroundReport name={ground} />}
    </div>
  </div>;
}

function PlayerResults({ query, onPick }: { query: string; onPick: (id: number) => void }) {
  const { blocks, error, loading } = useBlocks(`/api/stats/players?q=${encodeURIComponent(query)}`);
  const table = blocks?.[0];
  if (loading) return <div className="detail-loading"><span /><strong>Searching players…</strong></div>;
  if (error) return <div className="error-state"><strong>Search failed</strong><p>{error}</p></div>;
  if (!table || table.type !== "table" || !table.rows.length) return <div className="empty-state"><strong>No players match “{query}”</strong></div>;
  return <article className="detail-card stats-card"><div className="card-title"><h3>{table.title}</h3></div>
    <div className="stats-results">{table.rows.map((row) => <button key={String(row[0])} onClick={() => onPick(Number(row[0]))}>
      <strong>{cell(row[1])}</strong><span>{cell(row[4])}–{cell(row[5])}</span><small>{cell(row[2])} bat · {cell(row[3])} bowl innings</small>
    </button>)}</div>
  </article>;
}

// ------------------------------------------------------------------ grounds

function GroundPicker({ value, onChange }: { value: string; onChange: (name: string) => void }) {
  const { blocks } = useBlocks("/api/stats/venues?limit=40");
  const table = blocks?.find((block) => block.type === "table");
  const names = table && table.type === "table" ? table.rows.map((row) => String(row[0])) : [value];
  return <div className="innings-filter stats-h2h">
    <select aria-label="Ground" value={value} onChange={(e) => onChange(e.target.value)}>
      {(names.includes(value) ? names : [value, ...names]).map((name) => <option key={name}>{name}</option>)}
    </select>
    {table && table.type === "table" && <span className="stats-note">{table.rows.length} grounds, by matches played</span>}
  </div>;
}

type Table = Extract<Block, { type: "table" }>;

// Par score by season against the league in the same season: one bar per season, right of the
// line when the ground scored above the league, left when below.
function ParChart({ table }: { table: Table }) {
  const col = (name: string) => table.columns.indexOf(name);
  const seasons = table.rows.filter((row) => row[0] !== "Total" && typeof row[col("vs_league")] === "number");
  const scale = Math.max(10, ...seasons.map((row) => Math.abs(Number(row[col("vs_league")]))));
  return <article className="detail-card stats-card">
    <div className="card-title"><h3>Par score against the league</h3><small>first innings, same season</small></div>
    <div className="par-chart" role="list">{seasons.map((row) => {
      const diff = Number(row[col("vs_league")]);
      const width = `${(Math.abs(diff) / scale) * 50}%`;
      return <div role="listitem" className="par-row" key={String(row[0])} title={`${row[0]}: ${row[col("avg_1st_inns")]} here vs ${row[col("league_1st")]} league (${row[col("matches")]} matches)`}>
        <span>{String(row[0])}</span>
        <div className="par-track"><i className={diff >= 0 ? "up" : "down"} style={diff >= 0 ? { left: "50%", width } : { right: "50%", width }} /></div>
        <b className={diff >= 0 ? "up" : "down"}>{diff > 0 ? "+" : ""}{diff}</b>
        <small>{String(row[col("matches")])} m</small>
      </div>;
    })}</div>
  </article>;
}

function GroundReport({ name }: { name: string }) {
  const { blocks, error, loading } = useBlocks(`/api/stats/grounds/${encodeURIComponent(name)}`);
  if (loading && !blocks) return <div className="detail-loading"><span /><strong>Reading the ground…</strong></div>;
  if (error) return <div className="error-state"><strong>Ground report unavailable</strong><p>{error}</p></div>;
  const tables = (blocks ?? []).filter((block): block is Table => block.type === "table");
  const notes = (blocks ?? []).filter((block) => block.type === "text");
  return <div className={`scorecard-stack${loading ? " stats-refreshing" : ""}`}>
    {notes.map((note, index) => note.type === "text" && <p key={index} className="ground-verdict">{note.text}</p>)}
    {tables[0] && <ParChart table={tables[0]} />}
    {tables.slice(1).map((table, index) => <BlockCard key={index} block={table} />)}
    {tables[0] && <BlockCard block={tables[0]} />}
  </div>;
}
