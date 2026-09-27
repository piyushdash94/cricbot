"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";

type Team = { id: string; name: string; abbr: string; color: string; score: string; score_info: string };
type ArchiveMatch = {
  id: string; season: number; series_slug: string; match_slug: string; title: string;
  date: string; status: string; status_text: string; venue: string; teams: Team[]; series_name: string;
};
type Batter = { name: string; dismissal: string | Record<string, string>; runs: number; balls: number; fours: number; sixes: number; strike_rate: string | number; not_out: boolean };
type Bowler = { name: string; overs: string | number; maidens: number; runs: string | number; wickets: number; economy: string | number; dots: number };
type Innings = { number: number; team: string; runs: number; wickets: number; overs: number; extras: number; batters: Batter[]; bowlers: Bowler[] };
type Ball = { id: string; inning: number; label: string; event: string; runs: number; wicket: boolean; boundary: boolean; title: string; text: string; score: string; win_probability: number | null };
type MatchDetail = {
  match: ArchiveMatch; innings: Innings[]; balls: Ball[];
  ball_coverage: { level: string; label: string; note: string };
  toss: string | Record<string, unknown>; player_awards: string[];
  analytics: {
    impact: { player: string; team: string; role: string; impact: number }[];
    turning_points: { team: string; over: number; swing: number; runs: number; wickets: number }[];
    momentum: { team: string; values: number[] }[];
    pitch: Record<string, { econ?: number } | number | string>;
  };
  sources: { name: string; provider: string; available: boolean }[];
};
type UiAction = {
  type: "open_match" | "select_match" | "set_match_tab" | "set_view" | "navigate" | "theme" | "copy_summary";
  series_slug?: string; match_slug?: string; match_id?: string; tab?: MatchTab; view?: string; section?: string; value?: "light" | "dark";
};
type ChatMessage = { role: "user" | "assistant"; content: string; tools?: string[] };
type GraphNode = { id: string; label: string; kind: string; description: string };
type AgentGraph = { name: string; version: string; framework: string; nodes: GraphNode[]; edges: { from: string; to: string; condition?: string }[]; state_fields?: string[] };
type TraceEvent = {
  sequence: number; node: string; label: string; kind: string; summary: string; patch: Record<string, unknown>;
  snapshot: { phase: string; route: string; tools: string[]; result_count: number; action_count: number; model_used: boolean; validation: string };
};
type AgentResult = { reply: string; tool_calls?: { name: string }[]; ui_actions?: UiAction[] };
type DocsCatalog = {
  title: string; version: string; principles: string[];
  sources: { name: string; use: string; mode: string }[];
  apis: { method: string; path: string; use: string; request?: string; response: string }[];
  tools: { name: string; owner: string; use: string }[];
  states: { name: string; meaning: string }[];
  nodes: GraphNode[]; transitions: { from: string; to: string; condition?: string }[];
  coverage: { level: string; meaning: string }[];
};
type MatchTab = "overview" | "scorecard" | "balls" | "analytics";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
const SEASONS = [2025, 2024, 2023];
const DEFAULT_GRAPH: AgentGraph = {
  name: "Pandit Cricket Assistant", version: "1.1", framework: "LangGraph",
  nodes: [
    ["normalize_request", "Understand request", "input", "Normalize the question, visible match, and recent conversation."],
    ["route_tools", "Select tools", "routing", "Choose archive, player, standings, or analytics retrieval."],
    ["retrieve_facts", "Retrieve cricket facts", "tool", "Run deterministic tools and collect grounded results."],
    ["summarize_context", "Summarize evidence", "summary", "Compress facts and history into bounded context."],
    ["plan_ui", "Plan UI actions", "action", "Choose match and tab updates plus the response branch."],
    ["exact_response", "Exact tool answer", "response", "Return sensitive numbers directly from tools."],
    ["gemma_response", "Gemma synthesis", "model", "Use local Gemma to phrase the evidence naturally."],
    ["validate_response", "Grounding check", "validation", "Reject unsupported numbers and repetition."],
    ["finalize", "Finalize response", "output", "Package the answer, provenance, trace, and UI actions."],
  ].map(([id, label, kind, description]) => ({ id, label, kind, description })),
  edges: [],
};

function scoreLabel(team?: Team) {
  return team?.score && team.score !== "—" ? team.score : "Yet to bat";
}

function dismissal(value: Batter["dismissal"]) {
  if (typeof value === "string") return value;
  return value?.long ?? value?.short ?? "";
}

function formatDate(value: string) {
  if (!value) return "Date unavailable";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

export default function Home() {
  const [dark, setDark] = useState(true);
  const [apiStatus, setApiStatus] = useState<"checking" | "connected" | "offline">("checking");
  const [season, setSeason] = useState(2025);
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [matches, setMatches] = useState<ArchiveMatch[]>([]);
  const [total, setTotal] = useState(0);
  const [archiveSource, setArchiveSource] = useState("");
  const [archiveLoading, setArchiveLoading] = useState(true);
  const [archiveError, setArchiveError] = useState("");
  const [selected, setSelected] = useState<ArchiveMatch | null>(null);
  const [detail, setDetail] = useState<MatchDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [matchTab, setMatchTab] = useState<MatchTab>("overview");
  const [inningFilter, setInningFilter] = useState(0);
  const [docsOpen, setDocsOpen] = useState(false);
  const [docs, setDocs] = useState<DocsCatalog | null>(null);
  const [chatOpen, setChatOpen] = useState(true);
  const [traceOpen, setTraceOpen] = useState(false);
  const [agentStatus, setAgentStatus] = useState<"checking" | "ready" | "offline">("checking");
  const [graph, setGraph] = useState<AgentGraph>(DEFAULT_GRAPH);
  const [trace, setTrace] = useState<TraceEvent[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([
    { role: "assistant", content: "Hey, I’m Pandit. Search an IPL season, open any match, or ask me to find one and I’ll move the screen with you." },
  ]);
  const [chatInput, setChatInput] = useState("");
  const [chatLoading, setChatLoading] = useState(false);
  const detailRef = useRef<HTMLElement>(null);
  const chatEndRef = useRef<HTMLDivElement>(null);

  const loadArchive = useCallback(async (year: number, term: string) => {
    setArchiveLoading(true);
    setArchiveError("");
    try {
      const params = new URLSearchParams({ season: String(year), q: term, limit: "100", offset: "0" });
      const response = await fetch(`${API_BASE}/api/ipl/matches?${params}`);
      if (!response.ok) throw new Error("Archive unavailable");
      const payload = await response.json();
      setMatches(payload.matches ?? []);
      setTotal(payload.pagination?.total ?? 0);
      setArchiveSource(payload.source ?? "");
      setApiStatus("connected");
    } catch {
      setMatches([]);
      setTotal(0);
      setArchiveError("The local API is not responding. Start the backend and try again.");
      setApiStatus("offline");
    } finally {
      setArchiveLoading(false);
    }
  }, []);

  const openMatch = useCallback(async (match: ArchiveMatch, pushHistory = true) => {
    setSelected(match);
    setDetail(null);
    setDetailLoading(true);
    setMatchTab("overview");
    setInningFilter(0);
    if (pushHistory && typeof window !== "undefined") {
      const url = new URL(window.location.href);
      url.searchParams.set("series", match.series_slug);
      url.searchParams.set("match", match.match_slug);
      window.history.pushState({}, "", url);
    }
    window.setTimeout(() => detailRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 40);
    try {
      const response = await fetch(`${API_BASE}/api/ipl/matches/${encodeURIComponent(match.series_slug)}/${encodeURIComponent(match.match_slug)}`);
      if (!response.ok) throw new Error("Match unavailable");
      const payload = await response.json() as MatchDetail;
      setDetail(payload);
      setSelected(payload.match);
    } catch {
      setDetail(null);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => { setSearch(query.trim()); }, 280);
    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    const request = window.setTimeout(() => { void loadArchive(season, search); }, 0);
    return () => window.clearTimeout(request);
  }, [season, search, loadArchive]);

  useEffect(() => {
    if (selected || archiveLoading) return;
    const request = window.setTimeout(() => {
      const params = new URL(window.location.href).searchParams;
      const seriesSlug = params.get("series");
      const matchSlug = params.get("match");
      if (!seriesSlug || !matchSlug) return;
      const linkedSeason = SEASONS.find((year) => seriesSlug.includes(String(year))) ?? season;
      if (linkedSeason !== season) {
        setSeason(linkedSeason);
        return;
      }
      const found = matches.find((item) => item.series_slug === seriesSlug && item.match_slug === matchSlug);
      void openMatch(found ?? { id: matchSlug, season: linkedSeason, series_slug: seriesSlug, match_slug: matchSlug, title: "IPL match", date: "", status: "", status_text: "", venue: "", teams: [], series_name: `IPL ${linkedSeason}` }, false);
    }, 0);
    return () => window.clearTimeout(request);
  }, [archiveLoading, matches, openMatch, season, selected]);

  useEffect(() => {
    Promise.allSettled([
      fetch(`${API_BASE}/api/agent/status`).then((response) => response.ok ? response.json() : Promise.reject()),
      fetch(`${API_BASE}/api/agent/graph`).then((response) => response.ok ? response.json() : Promise.reject()),
      fetch(`${API_BASE}/api/docs/catalog`).then((response) => response.ok ? response.json() : Promise.reject()),
    ]).then(([status, graphResult, docsResult]) => {
      if (status.status === "fulfilled") setAgentStatus(status.value.status === "ready" ? "ready" : "offline");
      else setAgentStatus("offline");
      if (graphResult.status === "fulfilled") setGraph(graphResult.value);
      if (docsResult.status === "fulfilled") setDocs(docsResult.value);
    });
  }, []);

  useEffect(() => { chatEndRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, chatLoading]);

  function applyAction(action: UiAction) {
    if (action.type === "open_match" && action.series_slug && action.match_slug) {
      const found = matches.find((item) => item.series_slug === action.series_slug && item.match_slug === action.match_slug);
      if (found) void openMatch(found);
      else {
        void openMatch({ id: action.match_slug, season, series_slug: action.series_slug, match_slug: action.match_slug, title: "IPL match", date: "", status: "", status_text: "", venue: "", teams: [], series_name: `IPL ${season}` });
      }
    }
    if (action.type === "set_match_tab" && action.tab) {
      setMatchTab(action.tab);
      detailRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    if (action.type === "theme" && action.value) setDark(action.value === "dark");
  }

  function closeMatch() {
    setSelected(null);
    setDetail(null);
    const url = new URL(window.location.href);
    url.searchParams.delete("series");
    url.searchParams.delete("match");
    window.history.pushState({}, "", url);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function sendChat(event?: FormEvent, suggestion?: string) {
    event?.preventDefault();
    const text = (suggestion ?? chatInput).trim();
    if (!text || chatLoading) return;
    const history = messages.slice(-8);
    setMessages((current) => [...current, { role: "user", content: text }]);
    setChatInput("");
    setChatLoading(true);
    setTrace([]);
    try {
      const response = await fetch(`${API_BASE}/api/agent/chat/stream`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: text, history,
          ui_context: {
            season, query: search, selected_match: selected?.id, series_slug: selected?.series_slug,
            match_slug: selected?.match_slug, teams: selected?.teams.map((team) => team.abbr), active_tab: matchTab,
            ball_coverage: detail?.ball_coverage.level, theme: dark ? "dark" : "light",
          },
        }),
      });
      if (!response.ok || !response.body) throw new Error("Agent unavailable");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let result: AgentResult | null = null;
      const consume = (line: string) => {
        if (!line.trim()) return;
        const item = JSON.parse(line) as { event: "trace" | "result"; data: TraceEvent | AgentResult };
        if (item.event === "trace") setTrace((current) => [...current, item.data as TraceEvent]);
        else result = item.data as AgentResult;
      };
      while (true) {
        const { done, value } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        lines.forEach(consume);
        if (done) break;
      }
      consume(buffer);
      if (!result) throw new Error("No result");
      const finalResult = result as AgentResult;
      setMessages((current) => [...current, { role: "assistant", content: finalResult.reply, tools: finalResult.tool_calls?.map((item) => item.name.replace("search_", "")) }]);
      finalResult.ui_actions?.forEach(applyAction);
      setAgentStatus("ready");
    } catch {
      setMessages((current) => [...current, { role: "assistant", content: "I lost the local backend for a moment. Your archive view is safe—once it’s back, send that again and I’ll pick up from here." }]);
      setAgentStatus("offline");
    } finally {
      setChatLoading(false);
    }
  }

  const visibleBalls = useMemo(() => detail?.balls.filter((ball) => !inningFilter || ball.inning === inningFilter) ?? [], [detail, inningFilter]);
  return (
    <div className={`app ${dark ? "dark" : "light"}${chatOpen ? " chat-open" : ""}`}>
      <aside className="sidebar">
        <button className="brand" onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}><span className="brand-mark" /><span>CRIC<span>BOT</span></span></button>
        <nav>
          <button className="nav-item active" onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}><span className="nav-icon">⌕</span><span>IPL archive</span></button>
          <button className="nav-item" onClick={() => detailRef.current?.scrollIntoView({ behavior: "smooth" })}><span className="nav-icon">◉</span><span>Match detail</span></button>
          <button className="nav-item" onClick={() => setChatOpen(true)}><span className="nav-icon">✦</span><span>Pandit</span></button>
          <button className="nav-item" onClick={() => setDocsOpen(true)}><span className="nav-icon">≡</span><span>Docs</span></button>
        </nav>
        <div className="sidebar-foot">
          <div className="api-indicator"><span className={`status-dot ${apiStatus}`} /><div><strong>{apiStatus === "connected" ? "Local API online" : apiStatus === "offline" ? "API offline" : "Checking API"}</strong><small>{archiveSource || "127.0.0.1:8000"}</small></div></div>
        </div>
      </aside>

      <main>
        <header className="topbar">
          <div><p className="eyebrow">IPL INTELLIGENCE</p><h1>Every match, one conversation away.</h1></div>
          <div className="top-actions">
            <button className="docs-button" onClick={() => setDocsOpen(true)}>Docs <span>API + agent</span></button>
            <button className="icon-button" onClick={() => setDark((value) => !value)} aria-label="Toggle color theme">{dark ? "☼" : "☾"}</button>
            <button className="pandit-toggle" onClick={() => setChatOpen((value) => !value)}><span>✦</span> Pandit</button>
          </div>
        </header>

        <section className="archive-hero">
          <div>
            <p className="section-kicker">HISTORICAL MATCH EXPLORER</p>
            <h2>Don’t start with a match.<br /><em>Find the story first.</em></h2>
            <p>Search IPL history by team, venue, result, or season. Open any match for its scorecard, available ball-by-ball detail, and match intelligence.</p>
          </div>
          <div className="archive-stat"><strong>{total || "—"}</strong><span>matches in {season}</span><small>{archiveSource || "Connecting to archive…"}</small></div>
        </section>

        <section className="archive-controls" aria-label="IPL archive controls">
          <div className="season-tabs">{SEASONS.map((year) => <button className={season === year ? "active" : ""} key={year} onClick={() => { setSeason(year); setSelected(null); setDetail(null); }}>{year}</button>)}</div>
          <label className="archive-search"><span>⌕</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search RCB, Chennai, Wankhede, won by…" aria-label="Search historical IPL matches" />{query && <button onClick={() => setQuery("")} aria-label="Clear search">×</button>}</label>
        </section>

        <section className="match-catalog" aria-live="polite">
          <div className="section-heading"><div><p className="section-kicker">{search ? "SEARCH RESULTS" : `IPL ${season}`}</p><h3>{archiveLoading ? "Searching the archive…" : `${total} matches`}</h3></div><span>Click a match to open the full workspace</span></div>
          {archiveError && <div className="error-state"><strong>Archive unavailable</strong><p>{archiveError}</p><button onClick={() => void loadArchive(season, search)}>Try again</button></div>}
          {!archiveError && archiveLoading && <div className="match-grid skeleton-grid">{Array.from({ length: 6 }).map((_, index) => <div className="match-card skeleton" key={index} />)}</div>}
          {!archiveLoading && !archiveError && matches.length === 0 && <div className="empty-state"><strong>No matches found</strong><p>Try a shorter team name, venue, or result phrase.</p></div>}
          {!archiveLoading && <div className="match-grid">{matches.map((match) => <button className={`match-card${selected?.id === match.id ? " selected" : ""}`} key={`${match.season}-${match.id}`} onClick={() => void openMatch(match)}>
            <div className="match-card-top"><span>{formatDate(match.date)}</span><em>{match.status === "live" ? "LIVE" : match.status || "RESULT"}</em></div>
            <div className="match-teams">{match.teams.map((team) => <div key={team.abbr}><i style={{ background: team.color }}>{team.abbr.slice(0, 2)}</i><span><strong>{team.abbr}</strong><small>{team.name}</small></span><b>{scoreLabel(team)}</b></div>)}</div>
            <p>{match.status_text}</p><footer><span>{match.venue}</span><b>Open match →</b></footer>
          </button>)}</div>}
        </section>

        <section className="match-workspace" ref={detailRef}>
          {!selected && <div className="workspace-empty"><span>◉</span><div><p className="section-kicker">MATCH WORKSPACE</p><h3>Select any IPL match</h3><p>The scorecard, deliveries, analytics, and Pandit context will appear here.</p></div></div>}
          {selected && <>
            <div className="match-hero">
              <button className="back-link" onClick={closeMatch}>← Back to archive</button>
              <div className="match-meta"><span>{selected.series_name}</span><span>{formatDate(selected.date)}</span><span>{selected.venue}</span></div>
              <div className="match-scoreboard">{selected.teams.map((team) => <div className="score-team" key={team.abbr}><i style={{ borderColor: team.color }}>{team.abbr}</i><span><small>{team.name}</small><strong>{scoreLabel(team)}</strong><em>{team.score_info}</em></span></div>)}</div>
              <div className="result-strip"><span>Result</span><strong>{selected.status_text}</strong></div>
            </div>
            <div className="detail-tabs">{(["overview", "scorecard", "balls", "analytics"] as MatchTab[]).map((tab) => <button className={matchTab === tab ? "active" : ""} key={tab} onClick={() => setMatchTab(tab)}>{tab === "balls" ? "Ball by ball" : tab}</button>)}</div>
            {detailLoading && <div className="detail-loading"><span /><strong>Building this match workspace…</strong><small>Scorecard → commentary → analytics</small></div>}
            {!detailLoading && !detail && <div className="error-state"><strong>Match detail unavailable</strong><p>The archive card is still usable. Try this match again when the provider responds.</p><button onClick={() => void openMatch(selected, false)}>Retry</button></div>}
            {detail && matchTab === "overview" && <Overview detail={detail} onTab={setMatchTab} />}
            {detail && matchTab === "scorecard" && <Scorecard innings={detail.innings} />}
            {detail && matchTab === "balls" && <BallByBall detail={detail} balls={visibleBalls} filter={inningFilter} onFilter={setInningFilter} />}
            {detail && matchTab === "analytics" && <Analytics detail={detail} />}
          </>}
        </section>
        <footer className="page-footer"><span>Cricbot · local-first IPL intelligence</span><span>Provider coverage is shown on every match</span></footer>
      </main>

      <DocsDrawer open={docsOpen} docs={docs} onClose={() => setDocsOpen(false)} />
      <TraceDrawer open={traceOpen} graph={graph} trace={trace} loading={chatLoading} onClose={() => setTraceOpen(false)} />
      <aside className={`pandit-panel${chatOpen ? " open" : ""}`} aria-label="Pandit cricket assistant">
        <header className="pandit-header"><span className="pandit-avatar">P<i>✦</i></span><div><strong>Pandit</strong><small><i className={agentStatus} />{agentStatus === "ready" ? "Local Gemma ready" : agentStatus === "offline" ? "Deterministic mode" : "Checking model"}</small></div><button className={traceOpen ? "active" : ""} onClick={() => setTraceOpen((value) => !value)} aria-label="Toggle agent trace">⌘{trace.length ? <b>{trace.length}</b> : null}</button><button onClick={() => setChatOpen(false)}>×</button></header>
        <div className="pandit-context"><span>CONTEXT</span><strong>{selected?.title ?? `IPL ${season} archive`}</strong><small>{selected ? matchTab : `${total} matches`}</small></div>
        <div className="chat-messages">{messages.map((message, index) => <div className={`chat-message ${message.role}`} key={`${message.role}-${index}`}>
          {message.role === "assistant" && <span className="message-mark">P</span>}<div><p>{message.content}</p>{message.tools?.length ? <small>Used {message.tools.join(" · ")}</small> : null}</div>
        </div>)}{chatLoading && <div className="chat-message"><span className="message-mark">P</span><div className="thinking"><i /><i /><i /></div></div>}<div ref={chatEndRef} /></div>
        <div className="suggestion-list">{(selected ? ["Show me the ball-by-ball", "Who had the biggest impact?"] : [`Find RCB matches in ${season}`, "Show me the 2024 final"]).map((prompt) => <button key={prompt} onClick={() => void sendChat(undefined, prompt)}>{prompt}<span>↗</span></button>)}</div>
        <form className="chat-composer" onSubmit={sendChat}><textarea rows={2} value={chatInput} onChange={(event) => setChatInput(event.target.value)} placeholder="Ask Pandit anything about IPL…" onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void sendChat(); } }} /><div><span>Grounded tools + local Gemma</span><button disabled={!chatInput.trim() || chatLoading}>↑</button></div></form>
      </aside>
      {!chatOpen && <button className="pandit-fab" onClick={() => setChatOpen(true)}><span>✦</span> Ask Pandit</button>}
    </div>
  );
}

function Overview({ detail, onTab }: { detail: MatchDetail; onTab: (tab: MatchTab) => void }) {
  return <div className="overview-grid">
    <article className="detail-card"><div className="card-title"><div><p className="section-kicker">INNINGS</p><h3>Match at a glance</h3></div><button onClick={() => onTab("scorecard")}>Full scorecard →</button></div><div className="innings-summary">{detail.innings.map((inning) => <div key={inning.number}><span>{inning.number}</span><div><strong>{inning.team}</strong><small>{inning.overs} overs · {inning.extras} extras</small></div><b>{inning.runs}/{inning.wickets}</b></div>)}</div></article>
    <article className="detail-card"><div className="card-title"><div><p className="section-kicker">COVERAGE</p><h3>{detail.ball_coverage.label}</h3></div><span className={`coverage ${detail.ball_coverage.level}`}>{detail.ball_coverage.level}</span></div><p className="card-copy">{detail.ball_coverage.note}</p><button className="wide-action" onClick={() => onTab("balls")}>Explore available deliveries →</button></article>
    <article className="detail-card"><div className="card-title"><div><p className="section-kicker">MATCH NOTES</p><h3>Known facts</h3></div></div><dl className="facts"><div><dt>Toss</dt><dd>{typeof detail.toss === "string" ? detail.toss : JSON.stringify(detail.toss)}</dd></div><div><dt>Award</dt><dd>{detail.player_awards.join(", ") || "Unavailable"}</dd></div><div><dt>Feeds</dt><dd>{detail.sources.filter((source) => source.available).length}/{detail.sources.length} available</dd></div></dl></article>
    <article className="detail-card source-card"><div className="card-title"><div><p className="section-kicker">PROVENANCE</p><h3>Where this came from</h3></div></div>{detail.sources.map((source) => <div className="source-row" key={source.name}><i className={source.available ? "on" : ""} /><span><strong>{source.name}</strong><small>{source.provider}</small></span><b>{source.available ? "READY" : "FALLBACK"}</b></div>)}</article>
  </div>;
}

function Scorecard({ innings }: { innings: Innings[] }) {
  return <div className="scorecard-stack">{innings.map((inning) => <article className="detail-card scorecard-card" key={inning.number}><div className="innings-title"><span>{inning.number}</span><div><p className="section-kicker">{inning.number === 1 ? "FIRST INNINGS" : "SECOND INNINGS"}</p><h3>{inning.team}</h3></div><strong>{inning.runs}/{inning.wickets} <small>{inning.overs} ov</small></strong></div>
    <div className="table-wrap"><table><thead><tr><th>Batter</th><th>R</th><th>B</th><th>4s</th><th>6s</th><th>SR</th></tr></thead><tbody>{inning.batters.map((batter) => <tr key={batter.name}><td><strong>{batter.name}{batter.not_out ? "*" : ""}</strong><small>{dismissal(batter.dismissal) || (batter.not_out ? "not out" : "")}</small></td><td><b>{batter.runs}</b></td><td>{batter.balls}</td><td>{batter.fours}</td><td>{batter.sixes}</td><td>{batter.strike_rate}</td></tr>)}</tbody></table></div>
    <div className="table-wrap bowling-table"><table><thead><tr><th>Bowler</th><th>O</th><th>M</th><th>R</th><th>W</th><th>Econ</th></tr></thead><tbody>{inning.bowlers.map((bowler) => <tr key={bowler.name}><td><strong>{bowler.name}</strong></td><td>{bowler.overs}</td><td>{bowler.maidens}</td><td>{bowler.runs}</td><td><b>{bowler.wickets}</b></td><td>{bowler.economy}</td></tr>)}</tbody></table></div>
  </article>)}</div>;
}

function BallByBall({ detail, balls, filter, onFilter }: { detail: MatchDetail; balls: Ball[]; filter: number; onFilter: (value: number) => void }) {
  const innings = Array.from(new Set(detail.balls.map((ball) => ball.inning)));
  return <div className="balls-layout"><article className="detail-card balls-card"><div className="card-title"><div><p className="section-kicker">DELIVERY TIMELINE</p><h3>Ball by ball</h3></div><span className={`coverage ${detail.ball_coverage.level}`}>{detail.ball_coverage.label}</span></div><div className="innings-filter"><button className={filter === 0 ? "active" : ""} onClick={() => onFilter(0)}>All</button>{innings.map((number) => <button className={filter === number ? "active" : ""} key={number} onClick={() => onFilter(number)}>Innings {number}</button>)}</div>
    <div className="ball-list">{balls.length ? balls.map((ball) => <div className={`ball-row${ball.wicket ? " wicket" : ball.boundary ? " boundary" : ""}`} key={ball.id}><span>{ball.label}</span><b>{ball.event}</b><div><strong>{ball.title || `Delivery ${ball.label}`}</strong><p>{ball.text || "Commentary text unavailable."}</p></div>{ball.score && <em>{ball.score}</em>}</div>) : <div className="empty-state"><strong>No delivery records in this feed</strong><p>Try Overview or Scorecard; over-level data may still be available.</p></div>}</div></article>
    <aside className="coverage-note"><p className="section-kicker">READ THIS FEED</p><h3>Coverage, not guesswork.</h3><p>{detail.ball_coverage.note}</p><div>{detail.sources.map((source) => <span key={source.name}><i className={source.available ? "on" : ""} />{source.name}</span>)}</div></aside>
  </div>;
}

function Analytics({ detail }: { detail: MatchDetail }) {
  return <div className="analytics-layout"><article className="detail-card"><div className="card-title"><div><p className="section-kicker">PLAYER IMPACT</p><h3>Runs above match par</h3></div></div><div className="impact-list">{detail.analytics.impact.slice(0, 8).map((player, index) => <div key={`${player.player}-${player.role}`}><span>{String(index + 1).padStart(2, "0")}</span><div><strong>{player.player}</strong><small>{player.team} · {player.role}</small></div><b>{player.impact > 0 ? "+" : ""}{player.impact.toFixed(1)}</b></div>)}</div></article>
    <article className="detail-card"><div className="card-title"><div><p className="section-kicker">TURNING POINTS</p><h3>Where the match moved</h3></div></div><div className="turning-list">{detail.analytics.turning_points.map((point, index) => <div key={`${point.team}-${point.over}-${index}`}><span>{point.over}</span><div><strong>{point.team}</strong><small>{point.runs} runs · {point.wickets} wickets</small></div><b>{point.swing > 0 ? "+" : ""}{point.swing.toFixed(1)}</b></div>)}</div></article>
    <article className="detail-card momentum-card"><div className="card-title"><div><p className="section-kicker">MOMENTUM</p><h3>Innings pressure curve</h3></div></div>{detail.analytics.momentum.map((series) => <div className="momentum-row" key={series.team}><strong>{series.team}</strong><div>{series.values.map((value, index) => <i key={index} style={{ height: `${Math.max(4, Math.min(100, Math.abs(value)))}%` }} title={`${value}`} />)}</div></div>)}</article>
  </div>;
}

function DocsDrawer({ open, docs, onClose }: { open: boolean; docs: DocsCatalog | null; onClose: () => void }) {
  return <aside className={`docs-drawer${open ? " open" : ""}`} aria-label="Cricbot documentation"><header><div><p className="section-kicker">LIVING DOCUMENTATION</p><strong>{docs?.title ?? "Cricbot platform contract"}</strong><small>API patterns, ownership, state, and provenance</small></div><button onClick={onClose}>×</button></header><div className="docs-scroll">
    <section><h3>Architecture rules</h3>{(docs?.principles ?? ["Loading the local documentation catalog…"]).map((item) => <p className="doc-principle" key={item}>{item}</p>)}</section>
    <section><h3>Data sources</h3>{docs?.sources.map((source) => <article className="doc-source" key={source.name}><strong>{source.name}</strong><p>{source.use}</p><small>{source.mode}</small></article>)}</section>
    <section><h3>API contracts</h3>{docs?.apis.map((api) => <article className="api-contract" key={api.path}><div><b>{api.method}</b><code>{api.path}</code></div><p>{api.use}</p>{api.request && <small>Request · {api.request}</small>}<small>Response · {api.response}</small></article>)}</section>
    <section><h3>Tools and node ownership</h3>{docs?.tools.map((tool) => <article className="ownership" key={tool.name}><code>{tool.name}</code><span>owned by <b>{tool.owner}</b></span><p>{tool.use}</p></article>)}</section>
    <section><h3>LangGraph state</h3>{docs?.states.map((state) => <article className="state-contract" key={state.name}><code>{state.name}</code><p>{state.meaning}</p></article>)}</section>
    <section><h3>State transitions</h3><div className="transition-list">{docs?.transitions.map((edge, index) => <div key={`${edge.from}-${edge.to}-${index}`}><code>{edge.from}</code><span>→</span><code>{edge.to}</code>{edge.condition && <small>{edge.condition}</small>}</div>)}</div></section>
    <section><h3>Ball-data ladder</h3>{docs?.coverage.map((item) => <article className="coverage-contract" key={item.level}><span className={`coverage ${item.level}`}>{item.level}</span><p>{item.meaning}</p></article>)}</section>
  </div></aside>;
}

function TraceDrawer({ open, graph, trace, loading, onClose }: { open: boolean; graph: AgentGraph; trace: TraceEvent[]; loading: boolean; onClose: () => void }) {
  const current = trace.at(-1);
  const visited = new Set(trace.map((event) => event.node));
  return <aside className={`trace-panel${open ? " open" : ""}`} aria-label="Live LangGraph trace"><header className="trace-header"><div><p className="section-kicker">LIVE LANGGRAPH</p><strong>Agent graph</strong><small>{graph.nodes.length} nodes · {graph.version}</small></div><button onClick={onClose}>×</button></header><div className="trace-scroll">
    <section className="graph-card"><div className="graph-card-head"><span>Workflow</span><em>{loading ? "RUNNING" : "READY"}</em></div><div className="graph-flow">{graph.nodes.map((node, index) => <div key={node.id}><div className={`graph-node${current?.node === node.id && loading ? " active" : visited.has(node.id) ? " complete" : ""}`}><span>{visited.has(node.id) ? "✓" : index + 1}</span><div><strong>{node.label}</strong><small>{node.description}</small></div></div>{index < graph.nodes.length - 1 && <i className="flow-arrow">↓</i>}</div>)}</div></section>
    <section className="state-card"><div className="graph-card-head"><span>Current state</span><em>{current ? `#${current.sequence}` : "IDLE"}</em></div>{current ? <div className="state-grid"><div><span>Phase</span><strong>{current.label}</strong></div><div><span>Route</span><strong>{current.snapshot.route}</strong></div><div><span>Tools</span><strong>{current.snapshot.tools.join(", ") || "pending"}</strong></div><div><span>Results</span><strong>{current.snapshot.result_count}</strong></div><div><span>Actions</span><strong>{current.snapshot.action_count}</strong></div><div><span>Validation</span><strong>{current.snapshot.validation}</strong></div></div> : <p className="trace-empty">Send Pandit a message to watch every state patch.</p>}</section>
    <section className="timeline-card"><div className="graph-card-head"><span>State transitions</span><em>{trace.length} EVENTS</em></div><div className="trace-timeline">{trace.length ? trace.map((event) => <details className="trace-event" key={`${event.sequence}-${event.node}`} open={event.sequence === current?.sequence}><summary><span>{String(event.sequence).padStart(2, "0")}</span><div><strong>{event.label}</strong><small>{event.kind}</small></div><i>⌄</i></summary><p>{event.summary}</p><pre>{JSON.stringify(event.patch, null, 2)}</pre></details>) : <p className="trace-empty">No transitions yet. The graph is loaded and ready.</p>}</div></section>
  </div></aside>;
}
