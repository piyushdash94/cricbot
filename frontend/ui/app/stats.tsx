"use client";

// Stats workspace: season and all-time leaders, records, phases, player profiles and head to
// head, served by /api/stats/* (backend/apis/stats.py) over the local DuckDB archive.
// Every endpoint returns the same "blocks" (tables or notes), so one renderer covers them all.

import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

type Cell = string | number | null;
type Block = { type: "table"; title: string; columns: string[]; rows: Cell[][] } | { type: "text"; text: string };
type StatsStatus = { available: boolean; hint?: string; sources?: { cricsheet: number; espn: number }; years?: number[] };
type StatsTab = "leaders" | "records" | "phases" | "players" | "h2h";
type Phase = "powerplay" | "middle" | "death";

const TABS: { id: StatsTab; label: string }[] = [
  { id: "leaders", label: "Leaders" },
  { id: "records", label: "Records" },
  { id: "phases", label: "Phases" },
  { id: "players", label: "Players" },
  { id: "h2h", label: "Head to head" },
];
const TEAMS = ["CSK", "DC", "GT", "KKR", "LSG", "MI", "PBKS", "RCB", "RR", "SRH", "DCH", "GL", "KTK", "PWI", "RPS"];
const LABELS: Record<string, string> = {
  player: "Player", team: "Team", runs: "R", inns: "Inns", outs: "Out", avg: "Avg", sr: "SR", hs: "HS", wkts: "W",
  overs: "O", econ: "Econ", best: "Best", balls: "B", dot_pct: "Dot %", boundaries: "4s+6s", year: "Year",
  score: "Score", figures: "Figures", match: "Match", match_id: "Match id", balls_to_50: "Balls to 50", played: "P",
  no_result: "NR", phase: "Phase", first: "First", last: "Last", bat_inns: "Bat inns", bowl_inns: "Bowl inns", player_id: "Id",
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
const COMPACT_COLUMNS = ["player", "runs", "wkts", "avg", "sr", "econ", "hs", "best"];

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
  const [team2, setTeam2] = useState("CSK");

  useEffect(() => {
    fetch(`${API_BASE}/api/stats/status`).then((r) => r.ok ? r.json() : Promise.reject())
      .then(setStatus).catch(() => setStatus({ available: false, hint: "The stats API is not reachable." }));
  }, []);

  const yearParam = year === "all" ? "" : `year=${year}&`;
  const span = year === "all" ? "All time" : `IPL ${year}`;
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
        <select id="stats-year" value={year} onChange={(event) => setYear(event.target.value)}>
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
    {tab === "h2h" && <div className="innings-filter stats-h2h">
      <select aria-label="First team" value={team1} onChange={(e) => setTeam1(e.target.value)}>{TEAMS.map((t) => <option key={t}>{t}</option>)}</select>
      <span>v</span>
      <select aria-label="Second team" value={team2} onChange={(e) => setTeam2(e.target.value)}>{TEAMS.map((t) => <option key={t}>{t}</option>)}</select>
    </div>}
    {tab === "players" && <form className="innings-filter stats-search" onSubmit={submitPlayer}>
      <input value={playerQuery} onChange={(e) => setPlayerQuery(e.target.value)} placeholder="Search a player: Bumrah, Gayle, Kohli…" aria-label="Search players" />
      <button className="active" disabled={playerQuery.trim().length < 2}>Search</button>
    </form>}

    <div className="workspace-body">
      {tab === "leaders" && <div className="overview-grid">
        <Blocks path={`/api/stats/batting?${yearParam}limit=10`} onPlayer={openPlayer} compact />
        <Blocks path={`/api/stats/bowling?${yearParam}limit=10`} onPlayer={openPlayer} compact />
      </div>}
      {tab === "records" && <Blocks path={`/api/stats/records?${yearParam}limit=5`} onPlayer={openPlayer} />}
      {tab === "phases" && <Blocks path={`/api/stats/phase?phase=${phase}&role=${role}&${yearParam}min_balls=${year === "all" ? 300 : 60}&limit=15`} onPlayer={openPlayer} />}
      {tab === "players" && (playerId !== null
        ? <><button className="back-link stats-back" onClick={() => setPlayerId(null)}>← All results</button>
          <Blocks path={`/api/stats/players/${playerId}${year === "all" ? "" : `?year=${year}`}`} /></>
        : playerSearch.length >= 2
          ? <PlayerResults query={playerSearch} onPick={setPlayerId} />
          : <div className="empty-state"><strong>Find a player</strong><p>Season-by-season numbers and powerplay / middle / death splits. Tip: click any name in Leaders.</p></div>)}
      {tab === "h2h" && (team1 === team2
        ? <div className="empty-state"><strong>Pick two different teams</strong></div>
        : <Blocks path={`/api/stats/h2h?team1=${team1}&team2=${team2}${year === "all" ? "" : `&year=${year}`}`} />)}
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
