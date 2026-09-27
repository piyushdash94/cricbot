"use client";

import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

type Match = {
  id: string;
  league: string;
  status: string;
  teams: [string, string];
  names: [string, string];
  scores: [string, string];
  overs: [string, string];
  result: string;
  venue: string;
  winProbability: number[];
};

type SearchResult = {
  id: string;
  type: string;
  title: string;
  subtitle: string;
  meta: string;
  action?: UiAction;
};

type UiAction = {
  type: "select_match" | "set_view" | "navigate" | "theme" | "copy_summary";
  match_id?: string;
  view?: "scorecard" | "momentum" | "stands";
  section?: string;
  value?: "light" | "dark";
};

type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  tools?: string[];
};

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

const matches: Match[] = [
  {
    id: "rcb-pbks",
    league: "IPL 2025 · Final",
    status: "Final",
    teams: ["RCB", "PBKS"],
    names: ["Royal Challengers Bengaluru", "Punjab Kings"],
    scores: ["190/9", "184/7"],
    overs: ["20.0 ov", "20.0 ov"],
    result: "RCB won by 6 runs",
    venue: "Narendra Modi Stadium · Ahmedabad",
    winProbability: [49, 51, 48, 54, 52, 55, 58, 63, 60, 66, 62, 69, 73, 70, 76, 72, 80, 84, 91, 100],
  },
  {
    id: "mi-gt",
    league: "IPL 2025 · Qualifier 2",
    status: "Live · 14.2 ov",
    teams: ["MI", "GT"],
    names: ["Mumbai Indians", "Gujarat Titans"],
    scores: ["178/6", "131/3"],
    overs: ["20.0 ov", "14.2 ov"],
    result: "GT need 48 from 34 balls",
    venue: "Wankhede Stadium · Mumbai",
    winProbability: [43, 46, 42, 49, 54, 58, 61, 56, 63, 60, 66, 69, 64, 59, 62],
  },
  {
    id: "kkr-srh",
    league: "IPL 2025 · Match 74",
    status: "Today · 7:30 PM",
    teams: ["KKR", "SRH"],
    names: ["Kolkata Knight Riders", "Sunrisers Hyderabad"],
    scores: ["—", "—"],
    overs: ["Toss at 7:00", "Starts in 2h 18m"],
    result: "Match yet to begin",
    venue: "Eden Gardens · Kolkata",
    winProbability: [50, 50, 50, 50, 50, 50, 50, 50, 50, 50],
  },
];

const batting = [
  { name: "Phil Salt", detail: "c Arya b Arshdeep", runs: 61, balls: 30, fours: 5, sixes: 4, sr: "203.3" },
  { name: "Virat Kohli", detail: "b Chahal", runs: 43, balls: 35, fours: 4, sixes: 1, sr: "122.8" },
  { name: "Rajat Patidar", detail: "not out", runs: 36, balls: 22, fours: 2, sixes: 2, sr: "163.6" },
  { name: "Liam Livingstone", detail: "c Iyer b Arshdeep", runs: 27, balls: 18, fours: 2, sixes: 1, sr: "150.0" },
];

const impactPlayers = [
  { rank: 1, name: "Krunal Pandya", team: "RCB", role: "BOWL", value: "+24.7", detail: "3/23 · 12 dots" },
  { rank: 2, name: "Phil Salt", team: "RCB", role: "BAT", value: "+19.4", detail: "61 off 30" },
  { rank: 3, name: "Priyansh Arya", team: "PBKS", role: "BAT", value: "+13.2", detail: "54 off 32" },
  { rank: 4, name: "Josh Hazlewood", team: "RCB", role: "BOWL", value: "+10.8", detail: "2/36 · 8 dots" },
  { rank: 5, name: "Shreyas Iyer", team: "PBKS", role: "BAT", value: "+7.6", detail: "44 off 30" },
];

const standings = [
  { rank: 1, team: "PBKS", played: 14, won: 9, nrr: "+0.372", points: 19 },
  { rank: 2, team: "RCB", played: 14, won: 9, nrr: "+0.301", points: 19 },
  { rank: 3, team: "GT", played: 14, won: 9, nrr: "+0.254", points: 18 },
  { rank: 4, team: "MI", played: 14, won: 8, nrr: "+1.142", points: 16 },
  { rank: 5, team: "DC", played: 14, won: 7, nrr: "+0.011", points: 15 },
];

const momentum = [5, 8, 4, 12, 10, 15, 8, 18, 22, 16, 13, 26, 32, 25, 38, 30, 44, 52, 66, 82];
const partnerships = [
  { pair: "Kohli / Salt", runs: 95, balls: 51, share: 45 },
  { pair: "Patidar / Kohli", runs: 48, balls: 31, share: 60 },
  { pair: "Livingstone / Patidar", runs: 36, balls: 21, share: 42 },
];

const compactSummary = `🏏 MATCH\nRCB vs PBKS\nRCB won by 6 runs\nNarendra Modi Stadium, Ahmedabad\n\nRCB: 190/9 in 20 overs\nPBKS: 184/7 in 20 overs\n\nPlayer of the Match: Krunal Pandya`;

export default function Home() {
  const [matchFeed, setMatchFeed] = useState(matches);
  const [tableRows, setTableRows] = useState(standings);
  const [sharedSummary, setSharedSummary] = useState(compactSummary);
  const [selectedMatch, setSelectedMatch] = useState(0);
  const [activeView, setActiveView] = useState<"scorecard" | "momentum" | "stands">("scorecard");
  const [section, setSection] = useState("Overview");
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [searchSources, setSearchSources] = useState<string[]>([]);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchLoading, setSearchLoading] = useState(false);
  const [dark, setDark] = useState(true);
  const [copied, setCopied] = useState(false);
  const [apiStatus, setApiStatus] = useState<"demo" | "connected" | "offline">("demo");
  const [chatOpen, setChatOpen] = useState(true);
  const [chatInput, setChatInput] = useState("");
  const [chatLoading, setChatLoading] = useState(false);
  const [agentStatus, setAgentStatus] = useState<"checking" | "ready" | "offline">("checking");
  const [messages, setMessages] = useState<ChatMessage[]>([
    { role: "assistant", content: "I’m Pandit, your cricket intelligence assistant. Ask about this match, search players or standings, or tell me what you want to see in the dashboard." },
  ]);
  const chatEndRef = useRef<HTMLDivElement>(null);
  const match = matchFeed[selectedMatch];

  useEffect(() => {
    fetch(`${API_BASE}/api/dashboard`)
      .then(async (response) => {
        if (!response.ok) throw new Error("API unavailable");
        const payload = await response.json();
        if (Array.isArray(payload.matches)) {
          setMatchFeed((current) => current.map((item, index) => {
            const remote = payload.matches[index];
            if (!remote) return item;
            return {
              ...item,
              teams: remote.teams ?? item.teams,
              scores: remote.score ?? item.scores,
              status: remote.status ?? item.status,
              result: remote.result ?? item.result,
              venue: remote.venue ?? item.venue,
            };
          }));
        }
        if (Array.isArray(payload.standings)) setTableRows(payload.standings);
        if (payload.summary) setSharedSummary(payload.summary);
        setApiStatus("connected");
      })
      .catch(() => setApiStatus("offline"));
    fetch(`${API_BASE}/api/agent/status`)
      .then(async (response) => {
        if (!response.ok) throw new Error("Agent unavailable");
        const payload = await response.json();
        setAgentStatus(payload.status === "ready" ? "ready" : "offline");
      })
      .catch(() => setAgentStatus("offline"));
  }, []);

  useEffect(() => {
    const query = searchQuery.trim();
    if (query.length < 2) return;
    const timer = window.setTimeout(() => {
      fetch(`${API_BASE}/api/search?q=${encodeURIComponent(query)}&limit=10`)
        .then(async (response) => {
          if (!response.ok) throw new Error("Search failed");
          const payload = await response.json();
          setSearchResults(payload.results ?? []);
          setSearchSources(payload.sources ?? []);
          setSearchOpen(true);
        })
        .catch(() => {
          setSearchResults([]);
          setSearchSources([]);
        })
        .finally(() => setSearchLoading(false));
    }, 240);
    return () => window.clearTimeout(timer);
  }, [searchQuery]);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, chatLoading]);

  async function copySummary() {
    try {
      await navigator.clipboard.writeText(sharedSummary);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setCopied(false);
    }
  }

  function selectSection(label: string) {
    setSection(label);
    const target = label === "Overview" ? "top" : label.toLowerCase();
    document.getElementById(target)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function applyUiAction(action?: UiAction) {
    if (!action) return;
    if (action.type === "select_match" && action.match_id) {
      const index = matchFeed.findIndex((item) => item.id === action.match_id);
      if (index >= 0) setSelectedMatch(index);
      document.getElementById("matches")?.scrollIntoView({ behavior: "smooth" });
    }
    if (action.type === "set_view" && action.view) {
      setActiveView(action.view);
      document.getElementById("scorecard")?.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    if (action.type === "navigate" && action.section) {
      const label = action.section.charAt(0).toUpperCase() + action.section.slice(1);
      setSection(label);
      document.getElementById(action.section)?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    if (action.type === "theme" && action.value) setDark(action.value === "dark");
    if (action.type === "copy_summary") void copySummary();
  }

  function chooseSearchResult(result: SearchResult) {
    applyUiAction(result.action);
    setSearchQuery(result.title);
    setSearchOpen(false);
  }

  async function sendChat(event?: FormEvent, suggestedMessage?: string) {
    event?.preventDefault();
    const text = (suggestedMessage ?? chatInput).trim();
    if (!text || chatLoading) return;
    const userMessage: ChatMessage = { role: "user", content: text };
    const history = messages.slice(-8);
    setMessages((current) => [...current, userMessage]);
    setChatInput("");
    setChatLoading(true);
    try {
      const response = await fetch(`${API_BASE}/api/agent/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: text,
          history,
          ui_context: {
            selected_match: match.id,
            teams: match.teams,
            score: match.scores,
            active_view: activeView,
            section,
            theme: dark ? "dark" : "light",
          },
        }),
      });
      if (!response.ok) throw new Error("Pandit failed to respond");
      const payload = await response.json();
      setMessages((current) => [...current, {
        role: "assistant",
        content: payload.reply,
        tools: (payload.tool_calls ?? []).map((item: { name: string }) => item.name.replace("search_", "")),
      }]);
      (payload.ui_actions ?? []).forEach((action: UiAction) => applyUiAction(action));
      setAgentStatus("ready");
    } catch {
      setMessages((current) => [...current, { role: "assistant", content: "I couldn’t reach the local Pandit service. The dashboard is still available, and I’ll reconnect when the backend is ready." }]);
      setAgentStatus("offline");
    } finally {
      setChatLoading(false);
    }
  }

  return (
    <div className={`${dark ? "app dark" : "app light"}${chatOpen ? " chat-open" : ""}`}>
      <aside className="sidebar" aria-label="Primary navigation">
        <button className="brand" onClick={() => selectSection("Overview")} aria-label="Cricbot home">
          <span className="brand-mark"><span /></span>
          <span>CRIC<span>BOT</span></span>
        </button>

        <nav>
          {["Overview", "Matches", "Analytics", "Players"].map((label, index) => (
            <button
              key={label}
              className={section === label ? "nav-item active" : "nav-item"}
              onClick={() => selectSection(label)}
            >
              <span className="nav-icon" aria-hidden="true">{["⌂", "◉", "⌁", "♙"][index]}</span>
              <span>{label}</span>
            </button>
          ))}
        </nav>

        <div className="sidebar-foot">
          <div className="api-indicator">
            <span className={`status-dot ${apiStatus}`} />
            <div><strong>{apiStatus === "connected" ? "API connected" : apiStatus === "offline" ? "API offline" : "Demo data"}</strong><small>ESPNcricinfo feed</small></div>
          </div>
          <button className="profile"><span>SM</span><div><strong>Saket</strong><small>Match analyst</small></div><b>•••</b></button>
        </div>
      </aside>

      <main id="top">
        <header className="topbar">
          <div>
            <p className="eyebrow">MATCH CENTRE</p>
            <h1>Good evening, Saket.</h1>
          </div>
          <div className="top-actions">
            <div className="search-wrap">
              <label className="search-control">
                <span>{searchLoading ? "◌" : "⌕"}</span>
                <input
                  aria-label="Search matches, players, standings, and analytics"
                  placeholder="Search cricket…"
                  value={searchQuery}
                  onChange={(event) => {
                    const value = event.target.value;
                    setSearchQuery(value);
                    setSearchLoading(value.trim().length >= 2);
                    if (value.trim().length < 2) {
                      setSearchResults([]);
                      setSearchSources([]);
                    }
                  }}
                  onFocus={() => setSearchOpen(true)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && searchResults[0]) chooseSearchResult(searchResults[0]);
                    if (event.key === "Escape") setSearchOpen(false);
                  }}
                />
              </label>
              {searchOpen && searchQuery.trim().length >= 2 && (
                <div className="search-popover" role="listbox">
                  <div className="search-popover-head"><span>{searchLoading ? "Searching…" : `${searchResults.length} results`}</span><button onClick={() => setSearchOpen(false)}>×</button></div>
                  {searchResults.length ? searchResults.map((result) => (
                    <button key={result.id} className="search-result" onClick={() => chooseSearchResult(result)}>
                      <span className={`result-type ${result.type}`}>{result.type.slice(0, 1).toUpperCase()}</span>
                      <span><strong>{result.title}</strong><small>{result.subtitle}</small><em>{result.meta}</em></span>
                    </button>
                  )) : !searchLoading && <p className="search-empty">No grounded result found. Try a team, player, score, pitch, or standings query.</p>}
                  {searchSources.length > 0 && <div className="search-sources">Searched {searchSources.map((source) => source.replace("search_", "")).join(" · ")}</div>}
                </div>
              )}
            </div>
            <button className="icon-button" onClick={() => setDark(!dark)} aria-label="Toggle color theme">{dark ? "☼" : "☾"}</button>
            <button className="pandit-toggle" onClick={() => setChatOpen(!chatOpen)} aria-label="Toggle Pandit assistant"><span>✦</span> Pandit</button>
            <button className="share-button" onClick={copySummary}>{copied ? "Copied!" : "Share summary"}<span>↗</span></button>
          </div>
        </header>

        <section className="match-switcher" id="matches" aria-label="Select a match">
          {matchFeed.map((item, index) => (
            <button key={item.id} className={selectedMatch === index ? "match-pill selected" : "match-pill"} onClick={() => setSelectedMatch(index)}>
              <span className={item.status.startsWith("Live") ? "live-label" : "pill-status"}>{item.status}</span>
              <strong>{item.teams[0]} <i>vs</i> {item.teams[1]}</strong>
              <small>{item.result}</small>
            </button>
          ))}
        </section>

        <section className="hero-grid">
          <article className="match-hero">
            <div className="hero-header">
              <div><p className="eyebrow">{match.league}</p><p className="venue">⌖ {match.venue}</p></div>
              <span className="final-chip">{match.status}</span>
            </div>
            <div className="scoreboard">
              <TeamScore initials={match.teams[0]} name={match.names[0]} score={match.scores[0]} overs={match.overs[0]} color="red" />
              <div className="versus"><span>VS</span><small>{selectedMatch === 0 ? "20 overs" : "T20"}</small></div>
              <TeamScore initials={match.teams[1]} name={match.names[1]} score={match.scores[1]} overs={match.overs[1]} color="pink" reverse />
            </div>
            <div className="result-banner"><span>✓</span><strong>{match.result}</strong><small>{selectedMatch === 0 ? "A six-run thriller in Ahmedabad" : "Live match intelligence"}</small></div>
            <div className="match-metrics">
              <div><span>Player of the match</span><strong>{selectedMatch === 0 ? "Krunal Pandya" : "Match in progress"}</strong></div>
              <div><span>Top score</span><strong>{selectedMatch === 0 ? "Phil Salt · 61 (30)" : "S. Gill · 58* (39)"}</strong></div>
              <div><span>Best bowling</span><strong>{selectedMatch === 0 ? "3/23 · Econ 5.75" : "2/31 · Econ 7.75"}</strong></div>
            </div>
          </article>

          <article className="probability-card">
            <div className="card-heading"><div><p className="eyebrow">MATCH PULSE</p><h2>Win probability</h2></div><span className="metric-tag">{match.teams[0]} {match.winProbability.at(-1)}%</span></div>
            <div className="probability-chart" aria-label={`Win probability chart for ${match.teams[0]}`}>
              <div className="chart-label top">100%</div><div className="chart-label middle">50%</div><div className="chart-label bottom">0%</div>
              <div className="chart-bars">
                {match.winProbability.map((value, index) => <span key={index} style={{ height: `${Math.max(8, value)}%` }} className={index === match.winProbability.length - 1 ? "last" : ""} />)}
              </div>
            </div>
            <div className="chart-axis"><span>Powerplay</span><span>Middle</span><span>Death</span></div>
            <div className="swing-note"><span className="swing-icon">↗</span><div><strong>Defining swing</strong><p>Two wickets across overs 17–18 moved the game by <b>+18.4%</b>.</p></div></div>
          </article>
        </section>

        <section className="content-grid" id="scorecard">
          <article className="innings-card">
            <div className="tab-row">
              <div role="tablist" aria-label="Match detail view">
                <button className={activeView === "scorecard" ? "active" : ""} onClick={() => setActiveView("scorecard")}>Scorecard</button>
                <button className={activeView === "momentum" ? "active" : ""} onClick={() => setActiveView("momentum")}>Momentum</button>
                <button className={activeView === "stands" ? "active" : ""} onClick={() => setActiveView("stands")}>Partnerships</button>
              </div>
              <span>RCB innings <b>190/9</b></span>
            </div>
            {activeView === "scorecard" && (
              <div className="score-table-wrap">
                <table className="score-table">
                  <thead><tr><th>Batter</th><th>R</th><th>B</th><th>4s</th><th>6s</th><th>SR</th></tr></thead>
                  <tbody>{batting.map((row, index) => <tr key={row.name}><td><span className="player-avatar">{row.name.split(" ").map((part) => part[0]).join("")}</span><div><strong>{row.name}</strong><small>{row.detail}</small></div>{index === 0 && <em>TOP</em>}</td><td><strong>{row.runs}</strong></td><td>{row.balls}</td><td>{row.fours}</td><td>{row.sixes}</td><td>{row.sr}</td></tr>)}</tbody>
                </table>
                <div className="extras-row"><span>Extras <b>8</b></span><span>Total <strong>190/9</strong> <small>(20 overs)</small></span></div>
              </div>
            )}
            {activeView === "momentum" && (
              <div className="momentum-panel">
                <div className="momentum-bars">{momentum.map((value, index) => <span key={index} style={{ height: `${value}%` }}><i>{index + 1}</i></span>)}</div>
                <div className="momentum-legend"><span><i className="green" />RCB momentum</span><span>Overs 1–20</span></div>
              </div>
            )}
            {activeView === "stands" && (
              <div className="stands-panel">{partnerships.map((stand) => <div className="stand" key={stand.pair}><div><strong>{stand.pair}</strong><small>{stand.runs} runs · {stand.balls} balls</small></div><div className="stand-bar"><span style={{ width: `${stand.share}%` }} /><i /></div><b>{stand.runs}</b></div>)}</div>
            )}
          </article>

          <aside className="insights-card">
            <div className="card-heading"><div><p className="eyebrow">CONDITIONS</p><h2>Pitch read</h2></div><span className="confidence">High confidence</span></div>
            <div className="pitch-visual"><div className="pitch"><span /><i /><b /></div><div><strong>Balanced, slowing late</strong><p>Grip increased through the second innings. Spin conceded 1.8 fewer runs per over than pace.</p></div></div>
            <div className="condition-grid">
              <div><span>Pace economy</span><strong>8.62</strong><small>14 wickets</small></div>
              <div><span>Spin economy</span><strong>6.81</strong><small>5 wickets</small></div>
              <div><span>1st innings RPO</span><strong>9.50</strong><small>Above venue avg</small></div>
              <div><span>2nd innings RPO</span><strong>9.20</strong><small>−0.30 drop</small></div>
            </div>
            <div className="analyst-note"><span>i</span><p><strong>Analyst note</strong>One match is directional, not a venue rating. Compare against 10+ matches before drawing conclusions.</p></div>
          </aside>
        </section>

        <section className="analytics-grid" id="analytics">
          <article className="impact-card">
            <div className="card-heading"><div><p className="eyebrow">RUNS ABOVE PAR</p><h2>Impact leaderboard</h2></div><button>Methodology ↗</button></div>
            <div className="impact-list">
              {impactPlayers.map((player) => <div className="impact-row" key={player.name}><span className="rank">{player.rank}</span><span className={`team-badge ${player.team.toLowerCase()}`}>{player.team}</span><div><strong>{player.name}</strong><small>{player.detail}</small></div><em>{player.role}</em><b>{player.value}</b></div>)}
            </div>
          </article>

          <article className="standings-card" id="players">
            <div className="card-heading"><div><p className="eyebrow">IPL 2025</p><h2>Standings</h2></div><button>Full table ↗</button></div>
            <table><thead><tr><th>#</th><th>Team</th><th>P</th><th>W</th><th>NRR</th><th>Pts</th></tr></thead><tbody>{tableRows.map((row) => <tr key={row.team} className={row.team === "RCB" ? "highlight" : ""}><td>{row.rank}</td><td><span className={`mini-badge ${row.team.toLowerCase()}`}>{row.team}</span><strong>{row.team}</strong></td><td>{row.played}</td><td>{row.won}</td><td>{row.nrr}</td><td><b>{row.points}</b></td></tr>)}</tbody></table>
          </article>
        </section>

        <footer><span>CRICBOT · Cricket intelligence, compressed.</span><span>Data via ESPNcricinfo · Updated moments ago</span></footer>
      </main>
      <aside className={chatOpen ? "pandit-panel open" : "pandit-panel"} aria-label="Pandit cricket assistant">
        <header className="pandit-header">
          <div className="pandit-avatar">पं<span>✦</span></div>
          <div><strong>Pandit</strong><small><i className={agentStatus} /> Gemma 4 · {agentStatus === "ready" ? "Local & ready" : agentStatus === "offline" ? "Offline fallback" : "Checking"}</small></div>
          <button onClick={() => setChatOpen(false)} aria-label="Close Pandit">×</button>
        </header>
        <div className="pandit-context">
          <span>Watching</span><strong>{match.teams[0]} vs {match.teams[1]}</strong><small>{match.scores[0]} · {match.scores[1]}</small>
        </div>
        <div className="chat-messages" aria-live="polite">
          {messages.map((message, index) => (
            <div className={`chat-message ${message.role}`} key={`${message.role}-${index}`}>
              {message.role === "assistant" && <span className="message-mark">✦</span>}
              <div><p>{message.content}</p>{message.tools && message.tools.length > 0 && <span className="tool-trace">Used {message.tools.join(" + ")}</span>}</div>
            </div>
          ))}
          {chatLoading && <div className="chat-message assistant"><span className="message-mark">✦</span><div className="thinking"><i /><i /><i /></div></div>}
          <div ref={chatEndRef} />
        </div>
        {messages.length < 3 && <div className="suggestion-list">
          {["What changed in this match?", "Show me the momentum", "Who had the most impact?"].map((suggestion) => <button key={suggestion} onClick={() => void sendChat(undefined, suggestion)}>{suggestion}<span>↗</span></button>)}
        </div>}
        <form className="chat-composer" onSubmit={(event) => void sendChat(event)}>
          <textarea value={chatInput} onChange={(event) => setChatInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void sendChat(); } }} placeholder="Ask Pandit anything about cricket…" rows={2} aria-label="Message Pandit" />
          <div><span>Natural language → tools → UI</span><button type="submit" disabled={!chatInput.trim() || chatLoading} aria-label="Send message">↑</button></div>
        </form>
      </aside>
      {!chatOpen && <button className="pandit-fab" onClick={() => setChatOpen(true)}><span>✦</span> Ask Pandit</button>}
    </div>
  );
}

function TeamScore({ initials, name, score, overs, color, reverse = false }: { initials: string; name: string; score: string; overs: string; color: string; reverse?: boolean }) {
  return <div className={reverse ? "team-score reverse" : "team-score"}><div className={`team-crest ${color}`}>{initials.slice(0, 2)}<span /></div><div><p>{name}</p><strong>{score}</strong><span>{overs}</span></div></div>;
}
