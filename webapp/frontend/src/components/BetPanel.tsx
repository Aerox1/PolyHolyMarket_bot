import { useState } from "react";
import { motion } from "framer-motion";
import { api, ApiError, type Market } from "../api";
import {
  isValidPrice,
  payoutFor,
  pricePercent,
  sharesFor,
  usd,
  usdCents,
} from "../format";
import { closeApp, notify, haptic } from "../telegram";

const PRESETS = [1, 5, 20, 100];

type Outcome = "yes" | "no";

function errorMessage(detail: string, status: number): string {
  switch (detail) {
    case "no_account":
      return "Connect your wallet in the bot first to place real bets.";
    case "trading_unavailable":
      return "Trading is temporarily unavailable. Try again shortly.";
    case "order_failed":
      return "The order could not be placed. Please try again.";
    case "order_rejected":
      return "The order was rejected. Your balance or the market may have changed.";
    case "network_error":
      return "Network error. Check your connection and try again.";
    default:
      if (status === 401 || status === 403)
        return "Session expired — please reopen from the bot.";
      if (status === 400) return "Invalid bet. Please adjust and retry.";
      return "Something went wrong. Please try again.";
  }
}

export function BetPanel({
  market,
  connected,
  onClose,
}: {
  market: Market;
  connected: boolean;
  onClose: () => void;
}) {
  const [side, setSide] = useState<Outcome | null>(null);
  const [amount, setAmount] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<{ amount: number; outcome: Outcome } | null>(
    null,
  );

  async function confirm() {
    if (!side || amount == null || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await api.bet({
        market_id: market.id,
        outcome: side,
        amount_usd: amount,
      });
      notify("success");
      setSuccess({ amount: res.amount, outcome: res.outcome });
    } catch (e) {
      notify("error");
      if (e instanceof ApiError) setError(errorMessage(e.detail, e.status));
      else setError("Something went wrong. Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  const sheet = (children: React.ReactNode) => (
    <>
      <motion.div
        className="sheet-backdrop"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onClick={onClose}
      />
      <motion.div
        className="sheet"
        initial={{ y: "100%" }}
        animate={{ y: 0 }}
        exit={{ y: "100%" }}
        transition={{ type: "spring", stiffness: 360, damping: 36 }}
      >
        <div className="sheet-handle" />
        {children}
      </motion.div>
    </>
  );

  // Not connected -> prompt to connect in the bot.
  if (!connected) {
    return sheet(
      <div className="connect-box">
        <h2>{market.question}</h2>
        <p>Connect your wallet in the bot first to place real bets.</p>
        <button className="confirm-btn" onClick={closeApp}>
          Open the bot
        </button>
      </div>,
    );
  }

  // Success state.
  if (success) {
    const label = success.outcome === "yes" ? "YES" : "NO";
    return sheet(
      <div className="result-box">
        <div className="emoji">✅</div>
        <div className="msg">
          Bought {usd(success.amount)} on {label}
        </div>
        <div className="sub">{market.question}</div>
        <button
          className="confirm-btn"
          style={{ marginTop: 18 }}
          onClick={onClose}
        >
          Done
        </button>
      </div>,
    );
  }

  const canConfirm = side != null && amount != null && !submitting;

  // Bet preview: derive shares + payout from the selected side's price.
  const entryPrice =
    side === "yes" ? market.yes_price : side === "no" ? market.no_price : null;
  const shares = sharesFor(amount, entryPrice);
  const payout = payoutFor(amount, entryPrice);
  const showPreview =
    side != null && amount != null && isValidPrice(entryPrice) && shares != null && payout != null;

  return sheet(
    <>
      <h2>{market.question}</h2>

      <div className="side-row">
        <button
          className={`side-btn yes${side === "yes" ? " selected" : ""}`}
          onClick={() => {
            haptic("light");
            setSide("yes");
          }}
        >
          YES
          <span className="big">{pricePercent(market.yes_price)}</span>
        </button>
        <button
          className={`side-btn no${side === "no" ? " selected" : ""}`}
          onClick={() => {
            haptic("light");
            setSide("no");
          }}
        >
          NO
          <span className="big">{pricePercent(market.no_price)}</span>
        </button>
      </div>

      <div className="section-label">Amount</div>
      <div className="chips">
        {PRESETS.map((p) => (
          <button
            key={p}
            className={`chip${amount === p ? " selected" : ""}`}
            onClick={() => {
              haptic("light");
              setAmount(p);
            }}
          >
            {usd(p)}
          </button>
        ))}
      </div>

      {showPreview ? (
        <div className="bet-preview">
          <div className="preview-line">
            ≈ {shares!.toFixed(1)} shares · pays {usdCents(payout)} if{" "}
            {side === "yes" ? "YES" : "NO"} wins
          </div>
          <div className="preview-sub">
            Entry price {pricePercent(entryPrice)} · Market order — final price
            may move slightly.
          </div>
        </div>
      ) : null}

      {error ? (
        <div className="warn-money" style={{ color: "var(--no)", marginBottom: 12 }}>
          {error}
        </div>
      ) : null}

      <button className="confirm-btn" disabled={!canConfirm} onClick={confirm}>
        {submitting
          ? "Placing bet…"
          : side && amount != null
            ? `Confirm: ${usd(amount)} on ${side === "yes" ? "YES" : "NO"}`
            : "Select side & amount"}
      </button>
      <div className="warn-money">Real money — bets are placed instantly.</div>
    </>,
  );
}
