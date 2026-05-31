// Same-origin API client. Every request sends the Telegram initData header.
import { initDataHeader } from "./telegram";

export interface Stats {
  current_streak: number;
  longest_streak: number;
  total_bets: number;
  total_volume_usd: number;
  rank_bets: number;
}

export interface Me {
  telegram_id: number;
  language: string | null;
  connected: boolean;
  wallet: string | null;
  stats: Stats;
}

export interface Position {
  title: string;
  outcome: string;
  size: number;
  value: number;
  pnl: number;
}

export interface Portfolio {
  balance: number | null;
  positions: Position[];
}

export type LeaderboardMetric = "bets" | "volume";

export interface LeaderboardRow {
  rank: number;
  name: string;
  bets: number;
  volume_usd: number;
  streak: number;
}

export interface Leaderboard {
  metric: LeaderboardMetric;
  rows: LeaderboardRow[];
  me: Stats;
}

export interface Category {
  id: number;
  title: string;
  slug: string;
  volume: number;
  image_url: string | null;
  image_status: string | null;
}

export interface Market {
  id: string;
  question: string;
  volume: number;
  yes_price: number | null;
  no_price: number | null;
  yes_token: string;
  no_token: string;
  neg_risk: boolean;
  event_title: string | null;
}

export interface MarketsResponse {
  category: { id: number; title: string };
  markets: Market[];
}

export interface BetResult {
  ok: boolean;
  order_id: string | null;
  outcome: "yes" | "no";
  amount: number;
  question: string;
}

// Carries the HTTP status + parsed `detail` so callers can map to friendly text.
export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail || `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        "X-Telegram-Init-Data": initDataHeader(),
        ...(init?.headers ?? {}),
      },
    });
  } catch (e) {
    throw new ApiError(0, "network_error");
  }

  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }

  // 204 / empty body safety.
  const text = await res.text();
  return (text ? JSON.parse(text) : null) as T;
}

export const api = {
  me: () => request<Me>("/api/me"),
  categories: () => request<Category[]>("/api/categories"),
  categoryMarkets: (id: number) =>
    request<MarketsResponse>(`/api/categories/${id}/markets`),
  market: (id: string) => request<Market>(`/api/markets/${id}`),
  bet: (body: { market_id: string; outcome: "yes" | "no"; amount_usd: number }) =>
    request<BetResult>("/api/bet", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  portfolio: () => request<Portfolio>("/api/portfolio"),
  leaderboard: (metric: LeaderboardMetric) =>
    request<Leaderboard>(`/api/leaderboard?metric=${metric}`),
};
