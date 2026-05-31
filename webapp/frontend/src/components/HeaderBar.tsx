import type { Me } from "../api";

// Persistent, semi-transparent bar pinned to the top above the full-bleed
// cards. It is non-blocking (only the chips/buttons capture taps) so the
// underlying swipe gestures keep working.
export function HeaderBar({
  me,
  onPortfolio,
  onLeaderboard,
}: {
  me: Me | null;
  onPortfolio: () => void;
  onLeaderboard: () => void;
}) {
  const streak = me?.stats?.current_streak ?? 0;

  return (
    <div className="header-bar">
      <div className="streak-chip" title="Current streak">
        🔥 {streak}
      </div>
      <div className="header-spacer" />
      <button
        className="header-btn"
        aria-label="Portfolio"
        onClick={onPortfolio}
      >
        💰
      </button>
      <button
        className="header-btn"
        aria-label="Leaderboard"
        onClick={onLeaderboard}
      >
        🏆
      </button>
    </div>
  );
}
