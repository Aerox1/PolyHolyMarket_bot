import { useEffect, useState } from "react";
import { api, ApiError, type Portfolio } from "../api";
import { usdCents, usdSigned } from "../format";
import { closeApp } from "../telegram";
import { Sheet } from "./Sheet";

type State =
  | { status: "loading" }
  | { status: "ready"; data: Portfolio }
  | { status: "no_account" }
  | { status: "error" };

export function PortfolioSheet({ onClose }: { onClose: () => void }) {
  const [state, setState] = useState<State>({ status: "loading" });

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const data = await api.portfolio();
        if (alive) setState({ status: "ready", data });
      } catch (e) {
        if (!alive) return;
        if (e instanceof ApiError && e.status === 409 && e.detail === "no_account") {
          setState({ status: "no_account" });
        } else {
          setState({ status: "error" });
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  if (state.status === "loading") {
    return (
      <Sheet onClose={onClose}>
        <h2>Portfolio</h2>
        <div className="sheet-center">
          <div className="spinner" />
        </div>
      </Sheet>
    );
  }

  if (state.status === "no_account") {
    return (
      <Sheet onClose={onClose}>
        <div className="connect-box">
          <h2>Portfolio</h2>
          <p>Connect your wallet in the bot first to see your positions.</p>
          <button className="confirm-btn" onClick={closeApp}>
            Open the bot
          </button>
        </div>
      </Sheet>
    );
  }

  if (state.status === "error") {
    return (
      <Sheet onClose={onClose}>
        <h2>Portfolio</h2>
        <div className="sheet-center">
          <div className="emoji">⚠️</div>
          <p className="sheet-muted">Couldn't load your portfolio. Try again.</p>
        </div>
      </Sheet>
    );
  }

  const { balance, positions } = state.data;

  return (
    <Sheet onClose={onClose}>
      <h2>Portfolio</h2>

      <div className="balance-card">
        <div className="balance-label">USDC Balance</div>
        <div className="balance-value">{usdCents(balance)}</div>
      </div>

      {positions.length === 0 ? (
        <div className="sheet-center">
          <div className="emoji">📭</div>
          <p className="sheet-muted">
            No open positions yet. Swipe right on a market to place your first
            bet.
          </p>
        </div>
      ) : (
        <div className="position-list">
          {positions.map((p, i) => (
            <div className="position-row" key={`${p.title}-${p.outcome}-${i}`}>
              <div className="position-main">
                <div className="position-title">{p.title}</div>
                <div className="position-meta">
                  <span
                    className={`outcome-tag ${
                      p.outcome.toLowerCase() === "yes" ? "yes" : "no"
                    }`}
                  >
                    {p.outcome.toUpperCase()}
                  </span>
                  <span className="position-size">{p.size.toFixed(1)} shares</span>
                </div>
              </div>
              <div className="position-numbers">
                <div className="position-value">{usdCents(p.value)}</div>
                <div className={`position-pnl ${p.pnl < 0 ? "neg" : "pos"}`}>
                  {usdSigned(p.pnl)}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </Sheet>
  );
}
