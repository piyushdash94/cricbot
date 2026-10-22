"use client";

// Match analysis graphics, drawn from the match's own deliveries plus the league's par for each over
// (/api/stats/par) and the pitch profile (/api/stats/pitch/{id}). Plain SVG and CSS: no chart library.
//
// Why par per over and not the innings' own average: league scoring rises from ~7.5 runs in over 1 to
// ~11.5 in over 20, so comparing each over with the innings average just redraws the phase shape (death
// overs always "up", middle overs always "down"). Comparing with the league's par for that over shows
// what actually happened.

import { useEffect, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

export type ChartBall = { inning: number; label: string; runs: number; wicket: boolean };
export type Side = { inning: number; team: string; color: string; runs: number; wickets: number };
type Over = { over: number; runs: number; wickets: number; total: number };
type Cell = string | number | null;
type Block = { type: "table"; title: string; columns: string[]; rows: Cell[][] } | { type: "text"; text: string };

async function blocks(path: string): Promise<Block[] | null> {
  try {
    const response = await fetch(`${API_BASE}${path}`);
    return response.ok ? (await response.json()).blocks as Block[] : null;
  } catch {
    return null;
  }
}

/** Runs and wickets per over for one innings, from its deliveries ("3.4" is over 4). */
export function oversOf(balls: ChartBall[], inning: number): Over[] {
  const byOver = new Map<number, { runs: number; wickets: number }>();
  for (const ball of balls) {
    const at = Number.parseFloat(ball.label);
    if (ball.inning !== inning || !Number.isFinite(at)) continue;
    const over = Math.floor(at) + 1;
    const row = byOver.get(over) ?? { runs: 0, wickets: 0 };
    row.runs += ball.runs || 0;
    row.wickets += ball.wicket ? 1 : 0;
    byOver.set(over, row);
  }
  let total = 0;
  return [...byOver.entries()].sort((a, b) => a[0] - b[0]).map(([over, row]) => {
    total += row.runs;
    return { over, ...row, total };
  });
}

/** League runs per innings for each over number in a season (null when the archive is not built). */
export function usePar(season: number | undefined) {
  const [par, setPar] = useState<{ season: number; runs: Map<number, number> } | null>(null);
  useEffect(() => {
    if (!season) return;
    let live = true;
    void blocks(`/api/stats/par?year=${season}`).then((result) => {
      const table = result?.find((block) => block.type === "table");
      if (!live || !table || table.type !== "table") return;
      setPar({ season, runs: new Map(table.rows.map((row) => [Number(row[0]), Number(row[1])])) });
    });
    return () => { live = false; };
  }, [season]);
  return par && par.season === season ? par.runs : null;
}

// ------------------------------------------------------------------ runs per over against par

export function OverChart({ sides, balls, par, season }: { sides: Side[]; balls: ChartBall[]; par: Map<number, number> | null; season?: number }) {
  const series = sides.map((side) => ({ side, overs: oversOf(balls, side.inning) })).filter((s) => s.overs.length);
  if (!series.length) return <p className="card-copy">The over-by-over chart needs delivery data, which this match does not have.</p>;
  const top = Math.max(20, ...series.flatMap((s) => s.overs.map((o) => o.runs)), ...(par ? [...par.values()] : []));
  const slot = 19;
  const height = 110;
  const y = (runs: number) => height - (runs / top) * (height - 14);
  return <div className="over-chart">
    {series.map(({ side, overs }) => <figure key={side.inning}>
      <figcaption><strong style={{ color: side.color }}>{side.team}</strong> {side.runs}/{side.wickets}
        {par && <small>{overs.filter((o) => o.runs >= (par.get(o.over) ?? 0)).length} of {overs.length} overs at or above par</small>}</figcaption>
      <svg viewBox={`0 0 ${slot * 20 + 4} ${height + 16}`} role="img" aria-label={`${side.team} runs per over against par`}>
        <rect x={2} y={0} width={slot * 6} height={height} className="phase-band" />
        <rect x={2 + slot * 15} y={0} width={slot * 5} height={height} className="phase-band" />
        {overs.map((o) => {
          const x = 2 + (o.over - 1) * slot;
          const p = par?.get(o.over);
          const above = p === undefined || o.runs >= p;
          return <g key={o.over}>
            <title>{`Over ${o.over}: ${o.runs} runs${o.wickets ? `, ${o.wickets} wicket${o.wickets > 1 ? "s" : ""}` : ""}${p !== undefined ? ` (par ${p.toFixed(1)})` : ""}`}</title>
            <rect x={x + 3} y={y(o.runs)} width={slot - 6} height={height - y(o.runs)} rx={2}
              fill={side.color} opacity={above ? 0.95 : 0.35} />
            {p !== undefined && <line x1={x + 1} x2={x + slot - 1} y1={y(p)} y2={y(p)} className="par-tick" />}
            {Array.from({ length: o.wickets }, (_, i) => <circle key={i} cx={x + slot / 2} cy={Math.max(5, y(o.runs) - 6 - i * 8)} r={3.2} className="wicket-dot" />)}
          </g>;
        })}
        {[1, 6, 11, 16, 20].map((over) => <text key={over} x={2 + (over - 0.5) * slot} y={height + 12} className="axis">{over}</text>)}
      </svg>
    </figure>)}
    <p className="card-copy">Bars are runs in each over (faded when below par). The tick is par: the league’s average for that over
      {season ? ` in IPL ${season}` : ""}{par ? "" : " (build the stats archive to see it)"}. Red dots are wickets; shaded overs are the powerplay and the death.</p>
  </div>;
}

// ------------------------------------------------------------------ worm

export function Worm({ sides, balls, par }: { sides: Side[]; balls: ChartBall[]; par: Map<number, number> | null }) {
  const series = sides.map((side) => ({ side, overs: oversOf(balls, side.inning) })).filter((s) => s.overs.length);
  if (!series.length) return null;
  const parLine = par ? Array.from({ length: 20 }, (_, i) => i + 1).reduce<number[]>((acc, over) => [...acc, (acc.at(-1) ?? 0) + (par.get(over) ?? 0)], []) : [];
  const target = series.length > 1 ? series[0].side.runs + 1 : null;
  const top = Math.max(...series.map((s) => s.overs.at(-1)?.total ?? 0), target ?? 0, parLine.at(-1) ?? 0) + 10;
  const W = 400, H = 170;
  const x = (over: number) => 24 + (over / 20) * (W - 32);
  const y = (runs: number) => H - 18 - (runs / top) * (H - 30);
  return <div className="worm">
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Cumulative runs by over">
      {[50, 100, 150, 200, 250].filter((r) => r < top).map((r) => <g key={r}><line x1={24} x2={W - 8} y1={y(r)} y2={y(r)} className="grid" /><text x={2} y={y(r) + 3} className="axis">{r}</text></g>)}
      {[0, 6, 10, 15, 20].map((o) => <text key={o} x={x(o) - 3} y={H - 4} className="axis">{o}</text>)}
      {parLine.length > 0 && <polyline points={[`${x(0)},${y(0)}`, ...parLine.map((r, i) => `${x(i + 1)},${y(r)}`)].join(" ")} className="par-line" />}
      {target && <g><line x1={24} x2={W - 8} y1={y(target)} y2={y(target)} className="target-line" /><text x={W - 70} y={y(target) - 4} className="axis">target {target}</text></g>}
      {series.map(({ side, overs }) => <g key={side.inning}>
        <polyline points={[`${x(0)},${y(0)}`, ...overs.map((o) => `${x(o.over)},${y(o.total)}`)].join(" ")} fill="none" stroke={side.color} strokeWidth={2.4} />
        {overs.filter((o) => o.wickets).map((o) => <circle key={o.over} cx={x(o.over)} cy={y(o.total)} r={3.4} fill={side.color} className="worm-wicket"><title>{`${side.team}: ${o.wickets} wicket${o.wickets > 1 ? "s" : ""} in over ${o.over} (${o.total})`}</title></circle>)}
      </g>)}
    </svg>
    <p className="card-copy">Runs by the end of each over. {par ? "Dashed grey is a par innings for the season; " : ""}{target ? "the dashed line is the target; " : ""}dots mark overs with a wicket.</p>
  </div>;
}

// ------------------------------------------------------------------ player impact

export function ImpactBars({ rows, teamOf, colorOf }: {
  rows: { player: string; team?: string; role: string; impact: number }[];
  teamOf: (player: string) => string | undefined; colorOf: (team?: string) => string;
}) {
  // The API sends the best seven and the costliest three, high to low.
  const shown = rows.slice(0, 10);
  const peak = Math.max(1, ...shown.map((row) => Math.abs(row.impact)));
  return <div className="impact-bars">
    {shown.map((row) => {
      const team = row.team || teamOf(row.player);
      const width = `${(Math.abs(row.impact) / peak) * 50}%`;
      return <div key={`${row.player}-${row.role}`} className="impact-bar" title={`${row.player}: ${row.impact > 0 ? "+" : ""}${row.impact.toFixed(1)} runs against match par (${row.role === "bowl" ? "bowling" : "batting"})`}>
        <span className="who"><i style={{ background: colorOf(team) }} />{row.player}<small>{row.role === "bowl" ? "bowl" : "bat"}</small></span>
        <span className="track"><b className={row.impact >= 0 ? "pos" : "neg"} style={row.impact >= 0 ? { left: "50%", width } : { right: "50%", width }} /></span>
        <strong className={row.impact >= 0 ? "pos" : "neg"}>{row.impact > 0 ? "+" : ""}{row.impact.toFixed(1)}</strong>
      </div>;
    })}
    <p className="card-copy">Runs above match par: a batter’s runs against the match’s scoring rate, a bowler’s runs saved, with each wicket worth 12 runs.
      It ignores the phase and the match situation; a win-probability measure would weigh both.</p>
  </div>;
}

// ------------------------------------------------------------------ pitch profile

export function PitchCard({ matchSlug }: { matchSlug: string }) {
  const id = matchSlug.match(/(\d+)$/)?.[1];
  const [data, setData] = useState<{ id: string; blocks: Block[] | null } | null>(null);
  useEffect(() => {
    if (!id) return;
    let live = true;
    void blocks(`/api/stats/pitch/${id}`).then((result) => { if (live) setData({ id, blocks: result }); });
    return () => { live = false; };
  }, [id]);
  const result = data && data.id === id ? data.blocks : null;
  if (!result || result[0]?.type !== "table") return null;
  const tables = result.filter((b): b is Extract<Block, { type: "table" }> => b.type === "table");
  const verdict = result.find((b) => b.type === "text" && b.text.startsWith("Verdict:"));
  const phase = tables[0];
  const kinds = tables.find((t) => t.columns[0] === "kind");
  const cues = tables.find((t) => t.columns[0] === "cue");
  const col = (t: Extract<Block, { type: "table" }>, name: string) => t.columns.indexOf(name);
  const pair = (here: Cell, league: Cell, max: number) => <span className="pair-bars">
    <i style={{ width: `${(Number(here) / max) * 100}%` }} className="here" /><i style={{ width: `${(Number(league) / max) * 100}%` }} className="league" />
  </span>;
  const rrMax = Math.max(...phase.rows.flatMap((r) => [Number(r[1]), Number(r[2])]).filter(Number.isFinite), 1);
  const econMax = kinds ? Math.max(...kinds.rows.flatMap((r) => [Number(r[col(kinds, "econ_here")]), Number(r[col(kinds, "econ_league")])]).filter(Number.isFinite), 1) : 1;
  return <article className="detail-card pitch-card">
    <div className="card-title"><div><p className="section-kicker">PITCH PROFILE</p><h3>How the pitch played</h3></div></div>
    {verdict && verdict.type === "text" && <p className="pitch-verdict">{verdict.text.replace(/^Verdict: /, "").split(". Each part")[0]}.</p>}
    <div className="pitch-grid">
      <div><h4>Run rate by phase <small>this match · league</small></h4>
        {phase.rows.map((r) => <div className="pitch-row" key={String(r[0])}><span>{String(r[0])}</span>{pair(r[1], r[2], rrMax)}<b>{String(r[1] ?? "–")}</b><small>{String(r[2])}</small></div>)}</div>
      {kinds && <div><h4>Economy, pace v spin <small>this match · league</small></h4>
        {kinds.rows.map((r) => <div className="pitch-row" key={String(r[0])}><span>{String(r[0])}</span>{pair(r[col(kinds, "econ_here")], r[col(kinds, "econ_league")], econMax)}
          <b>{String(r[col(kinds, "econ_here")] ?? "–")}</b><small>{String(r[col(kinds, "wickets")] ?? 0)} wkts</small></div>)}</div>}
    </div>
    {cues && <div className="pitch-cues"><h4>From the commentary</h4>{cues.rows.map((r) => <span key={String(r[0])} title={String(r[2])}>{String(r[0])} <b>×{String(r[1])}</b></span>)}</div>}
    <p className="card-copy">Outcomes against the league in the same season, not a pitch reading: they mix the surface with the bowlers, the batters and the match situation. Commentary cues count phrases such as “turned away sharply”.</p>
  </article>;
}
