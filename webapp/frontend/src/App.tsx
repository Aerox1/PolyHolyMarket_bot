import { useCallback, useEffect, useState } from "react";
import { AnimatePresence } from "framer-motion";
import { api, ApiError, type Category, type Market, type Me } from "./api";
import {
  bootTelegram,
  haptic,
  isInsideTelegram,
  theme,
  tg,
} from "./telegram";
import { SwipeCard, type Dir } from "./components/SwipeCard";
import { CategoryCardContent, categoryBackground } from "./components/CategoryCard";
import { MarketCardContent, marketBackground } from "./components/MarketCard";
import { BetPanel } from "./components/BetPanel";
import { HeaderBar } from "./components/HeaderBar";
import { PortfolioSheet } from "./components/PortfolioSheet";
import { LeaderboardSheet } from "./components/LeaderboardSheet";
import { Loading, StatusScreen } from "./components/Status";

type Level = "CATEGORY" | "MARKETS";

function applyTheme() {
  const t = theme();
  const root = document.documentElement.style;
  root.setProperty("--bg", t.bg);
  root.setProperty("--text", t.text);
  root.setProperty("--hint", t.hint);
  root.setProperty("--button", t.button);
  root.setProperty("--button-text", t.buttonText);
  root.setProperty("--secondary-bg", t.secondaryBg);
  document.body.style.background = t.bg;
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [authError, setAuthError] = useState<ApiError | null>(null);

  const [categories, setCategories] = useState<Category[] | null>(null);
  const [catError, setCatError] = useState<string | null>(null);

  const [level, setLevel] = useState<Level>("CATEGORY");
  const [catIndex, setCatIndex] = useState(0);
  const [marketIndex, setMarketIndex] = useState(0);

  const [markets, setMarkets] = useState<Market[] | null>(null);
  const [marketsLoading, setMarketsLoading] = useState(false);
  const [marketsError, setMarketsError] = useState<string | null>(null);

  const [betMarket, setBetMarket] = useState<Market | null>(null);
  const [showPortfolio, setShowPortfolio] = useState(false);
  const [showLeaderboard, setShowLeaderboard] = useState(false);
  const [exitDir, setExitDir] = useState<Dir | null>(null);

  // Open/close helpers fire a light haptic (guarded inside `haptic`).
  const openPortfolio = useCallback(() => {
    haptic("light");
    setShowLeaderboard(false);
    setShowPortfolio(true);
  }, []);
  const closePortfolio = useCallback(() => {
    haptic("light");
    setShowPortfolio(false);
  }, []);
  const openLeaderboard = useCallback(() => {
    haptic("light");
    setShowPortfolio(false);
    setShowLeaderboard(true);
  }, []);
  const closeLeaderboard = useCallback(() => {
    haptic("light");
    setShowLeaderboard(false);
  }, []);
  const closeBet = useCallback(() => {
    haptic("light");
    setBetMarket(null);
  }, []);

  // ── boot ──────────────────────────────────────────────────
  useEffect(() => {
    bootTelegram();
    applyTheme();
  }, []);

  const loadCore = useCallback(async () => {
    setCatError(null);
    setAuthError(null);
    try {
      const m = await api.me();
      setMe(m);
    } catch (e) {
      if (e instanceof ApiError) setAuthError(e);
      // still attempt categories below; both may fail with 401 in dev
    }
    try {
      const cats = await api.categories();
      setCategories(cats);
    } catch (e) {
      if (e instanceof ApiError) {
        if (e.status === 401 || e.status === 403) setAuthError(e);
        else setCatError("Couldn't load categories. Pull to retry.");
      }
      setCategories([]);
    }
  }, []);

  useEffect(() => {
    void loadCore();
  }, [loadCore]);

  // ── native BackButton wiring ──────────────────────────────
  // An overlay (bet panel / portfolio / leaderboard) takes priority over
  // level navigation: Back closes the topmost open overlay first, and only
  // falls back to CATEGORY navigation when nothing is open.
  useEffect(() => {
    const w = tg();
    if (!w) return;
    const anyOverlay =
      betMarket != null || showPortfolio || showLeaderboard;
    const showBack = level === "MARKETS" || anyOverlay;
    const onBack = () => {
      if (betMarket != null) {
        closeBet();
      } else if (showPortfolio) {
        closePortfolio();
      } else if (showLeaderboard) {
        closeLeaderboard();
      } else if (level === "MARKETS") {
        setLevel("CATEGORY");
        setMarkets(null);
        setMarketsError(null);
      }
    };
    w.BackButton.onClick(onBack);
    if (showBack) w.BackButton.show();
    else w.BackButton.hide();
    return () => {
      try {
        w.BackButton.offClick(onBack);
      } catch {
        /* no-op */
      }
    };
  }, [
    level,
    betMarket,
    showPortfolio,
    showLeaderboard,
    closeBet,
    closePortfolio,
    closeLeaderboard,
  ]);

  const enterCategory = useCallback(async (cat: Category) => {
    setMarketsLoading(true);
    setMarketsError(null);
    setMarkets(null);
    setMarketIndex(0);
    setLevel("MARKETS");
    try {
      const res = await api.categoryMarkets(cat.id);
      setMarkets(res.markets ?? []);
    } catch (e) {
      if (e instanceof ApiError && (e.status === 401 || e.status === 403)) {
        setAuthError(e);
      }
      setMarketsError("Couldn't load this category's markets.");
      setMarkets([]);
    } finally {
      setMarketsLoading(false);
    }
  }, []);

  // ── gesture handlers ──────────────────────────────────────
  const onCategorySwipe = useCallback(
    (dir: Dir) => {
      if (!categories || categories.length === 0) return;
      if (dir === "up" || dir === "down") {
        setExitDir(dir);
        haptic("light");
        setCatIndex((i) => {
          const n = categories.length;
          if (dir === "up") return (i + 1) % n; // next, wrap
          return (i - 1 + n) % n; // previous, wrap
        });
      } else if (dir === "left") {
        haptic("light");
        void enterCategory(categories[catIndex]);
      }
    },
    [categories, catIndex, enterCategory],
  );

  const onMarketSwipe = useCallback(
    (dir: Dir) => {
      if (!markets || markets.length === 0) return;
      if (dir === "up" || dir === "down") {
        setExitDir(dir);
        haptic("light");
        setMarketIndex((i) => {
          const n = markets.length;
          if (dir === "up") return (i + 1) % n;
          return (i - 1 + n) % n;
        });
      } else if (dir === "right") {
        haptic("light");
        setBetMarket(markets[marketIndex]);
      }
    },
    [markets, marketIndex],
  );

  // clamp indices if data shrinks
  const safeCatIndex =
    categories && categories.length ? catIndex % categories.length : 0;
  const safeMarketIndex =
    markets && markets.length ? marketIndex % markets.length : 0;

  // ── render: auth / loading / empty gates ──────────────────
  if (categories === null && !authError) {
    return <Loading label="Loading markets…" />;
  }

  // 401/403 outside Telegram (or expired session): clear instruction.
  if (authError && (authError.status === 401 || authError.status === 403)) {
    return (
      <StatusScreen
        emoji="📲"
        title="Open inside Telegram"
        message={
          isInsideTelegram()
            ? "Your session has expired. Please reopen this app from the bot."
            : "This Mini App must be opened from the Telegram bot to load your account and markets."
        }
      />
    );
  }

  if (catError && (!categories || categories.length === 0)) {
    return (
      <StatusScreen
        emoji="⚠️"
        title="Couldn't load"
        message={catError}
        actionLabel="Retry"
        onAction={() => void loadCore()}
      />
    );
  }

  if (categories && categories.length === 0) {
    return (
      <StatusScreen
        emoji="🗳️"
        title="No categories yet"
        message="Check back soon — markets are being prepared."
        actionLabel="Refresh"
        onAction={() => void loadCore()}
      />
    );
  }

  const currentCat = categories![safeCatIndex];
  const currentMarket =
    markets && markets.length ? markets[safeMarketIndex] : null;

  return (
    <div className="app">
      <HeaderBar
        me={me}
        onPortfolio={openPortfolio}
        onLeaderboard={openLeaderboard}
      />

      {level === "CATEGORY" ? (
        <>
          <SwipeCard
            cardKey={`cat-${currentCat.id}`}
            background={categoryBackground(currentCat)}
            enable={{ up: true, down: true, left: true }}
            exitDir={exitDir}
            onSwipe={onCategorySwipe}
          >
            <CategoryCardContent cat={currentCat} />
          </SwipeCard>
          <PageDots count={categories!.length} active={safeCatIndex} />
        </>
      ) : (
        <MarketsLevel
          loading={marketsLoading}
          error={marketsError}
          markets={markets}
          marketIndex={safeMarketIndex}
          currentMarket={currentMarket}
          category={currentCat}
          exitDir={exitDir}
          onSwipe={onMarketSwipe}
        />
      )}

      <AnimatePresence>
        {betMarket ? (
          <BetPanel
            key="bet"
            market={betMarket}
            connected={me?.connected ?? false}
            onClose={closeBet}
          />
        ) : showPortfolio ? (
          <PortfolioSheet key="portfolio" onClose={closePortfolio} />
        ) : showLeaderboard ? (
          <LeaderboardSheet key="leaderboard" onClose={closeLeaderboard} />
        ) : null}
      </AnimatePresence>
    </div>
  );
}

function MarketsLevel({
  loading,
  error,
  markets,
  marketIndex,
  currentMarket,
  category,
  exitDir,
  onSwipe,
}: {
  loading: boolean;
  error: string | null;
  markets: Market[] | null;
  marketIndex: number;
  currentMarket: Market | null;
  category: Category;
  exitDir: Dir | null;
  onSwipe: (dir: Dir) => void;
}) {
  if (loading) return <Loading label="Loading markets…" />;
  if (error)
    return (
      <StatusScreen emoji="⚠️" title="Couldn't load" message={error} />
    );
  if (!markets || markets.length === 0 || !currentMarket)
    return (
      <StatusScreen
        emoji="🔍"
        title="No markets here"
        message="This category has no open markets right now. Go back and pick another."
      />
    );

  return (
    <>
      <SwipeCard
        cardKey={`mkt-${currentMarket.id}`}
        background={marketBackground(currentMarket, category)}
        enable={{ up: true, down: true, right: true }}
        exitDir={exitDir}
        onSwipe={onSwipe}
      >
        <MarketCardContent market={currentMarket} eyebrow={category.title} />
      </SwipeCard>
      <PageDots count={markets.length} active={marketIndex} />
    </>
  );
}

function PageDots({ count, active }: { count: number; active: number }) {
  // Cap rendered dots so very long lists don't overflow.
  const max = 9;
  if (count <= 1) return null;
  if (count <= max) {
    return (
      <div className="dots">
        {Array.from({ length: count }).map((_, i) => (
          <div key={i} className={`dot${i === active ? " active" : ""}`} />
        ))}
      </div>
    );
  }
  // windowed view around active
  const half = Math.floor(max / 2);
  let start = Math.max(0, active - half);
  start = Math.min(start, count - max);
  return (
    <div className="dots">
      {Array.from({ length: max }).map((_, i) => {
        const idx = start + i;
        return (
          <div key={idx} className={`dot${idx === active ? " active" : ""}`} />
        );
      })}
    </div>
  );
}
