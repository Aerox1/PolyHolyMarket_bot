import { useEffect, useState } from "react";
import {
  api,
  ApiError,
  type Leaderboard,
  type LeaderboardMetric,
} from "../api";
import { usdCents, usdSigned, winRatePercent } from "../format";
import { haptic } from "../telegram";
import { Sheet } from "./Sheet";

type State =
  | { status: "loading" }
  | { status: "ready"; data: Leaderboard }
  | { status: "error" };

export function LeaderboardSheet({ onClose }: { onClose: () => void }) {
  const [metric, setMetric] = useState<LeaderboardMetric>("bets");
  const [state, setState] = useState<State>({ status: "loading" });

  useEffect(() => {
    let alive = true;
    setState({ status: "loading" });
    (async () => {
      try {
        const data = await api.leaderboard(metric);
        if (alive) setState({ status: "ready", data });
      } catch (e) {
        if (!alive) return;
        if (e instanceof ApiError) setState({ status: "error" });
        else setState({ status: "error" });
      }
    })();
    return () => {
      alive = false;
    };
  }, [metric]);

  function pick(next: LeaderboardMetric) {
    if (next === metric) return;
    haptic("light");
    setMetric(next);
  }

  const data = state.status === "ready" ? state.data : null;
  // Highlight the user's own row when their name appears in the list.
  const meName = "You";

  return (
    <Sheet onClose={onClose}>
      <h2>Leaderboard</h2>

      <div className="lb-toggle">
        <button
          className={`lb-toggle-btn${metric === "bets" ? " active" : ""}`}
          onClick={() => pick("bets")}
        >
          Most active
        </button>
        <button
          className={`lb-toggle-btn${metric === "volume" ? " active" : ""}`}
          onClick={() => pick("volume")}
        >
          Volume
        </button>
        <button
          className={`lb-toggle-btn${metric === "pnl" ? " active" : ""}`}
          onClick={() => pick("pnl")}
        >
          P&amp;L
        </button>
      </div>

      {state.status === "loading" ? (
        <div className="sheet-center">
          <div className="spinner" />
        </div>
      ) : state.status === "error" ? (
        <div className="sheet-center">
          <div className="emoji">⚠️</div>
          <p className="sheet-muted">Couldn't load the leaderboard. Try again.</p>
        </div>
      ) : data && data.rows.length === 0 ? (
        <div className="sheet-center">
          <div className="emoji">🏁</div>
          <p className="sheet-muted">
            No one's on the board yet. Place a bet to claim a spot.
          </p>
        </div>
      ) : data ? (
        <>
          <div className="lb-list">
            {data.rows.map((r) => {
              const mine = r.name === meName;
              const isPnl = metric === "pnl";
              const value = isPnl
                ? usdSigned(r.pnl_usd)
                : metric === "bets"
                  ? `${r.bets} bets`
                  : metric === "wins"
                    ? `${r.wins} wins`
                    : usdCents(r.volume_usd);
              const valueClass = isPnl
                ? r.pnl_usd < 0
                  ? " neg"
                  : " pos"
                : "";
              const sub =
                r.win_rate != null
                  ? `${winRatePercent(r.win_rate)} · 🔥${r.streak}`
                  : null;
              return (
                <div className={`lb-row${mine ? " mine" : ""}`} key={r.rank}>
                  <div className="lb-rank">#{r.rank}</div>
                  <div className="lb-name-col">
                    <div className="lb-name">{r.name}</div>
                    {sub ? <div className="lb-sub">{sub}</div> : null}
                  </div>
                  <div className={`lb-value${valueClass}`}>{value}</div>
                  {sub ? null : <div className="lb-streak">🔥 {r.streak}</div>}
                </div>
              );
            })}
          </div>

          {data.rows.some((r) => r.name === meName) ? null : (
            <div className="lb-footer">
              You: #{data.me.rank_bets} · {data.me.total_bets} bets · 🔥
              {data.me.current_streak}
              {data.me.win_rate != null
                ? ` · ${winRatePercent(data.me.win_rate)} win`
                : ""}
              {data.me.settled_bets > 0
                ? ` · ${usdSigned(data.me.realized_pnl_usd)}`
                : ""}
            </div>
          )}
        </>
      ) : null}
    </Sheet>
  );
}
