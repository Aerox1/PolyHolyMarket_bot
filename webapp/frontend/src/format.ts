// Formatting + deterministic-gradient helpers shared across cards.

// 0.62 -> "62¢", null -> "—"
export function priceCents(p: number | null | undefined): string {
  if (p == null || Number.isNaN(p)) return "—";
  return `${Math.round(p * 100)}¢`;
}

// 0.62 -> "62%"
export function pricePercent(p: number | null | undefined): string {
  if (p == null || Number.isNaN(p)) return "—";
  return `${Math.round(p * 100)}%`;
}

// 125100000 -> "$125.1M Vol"
export function volumeLabel(v: number | null | undefined): string {
  const n = Number(v) || 0;
  let s: string;
  if (n >= 1e9) s = `$${(n / 1e9).toFixed(1)}B`;
  else if (n >= 1e6) s = `$${(n / 1e6).toFixed(1)}M`;
  else if (n >= 1e3) s = `$${(n / 1e3).toFixed(1)}K`;
  else s = `$${n.toFixed(0)}`;
  return `${s} Vol`;
}

// 5 -> "$5", 1000 -> "$1,000"
export function usd(n: number): string {
  return `$${n.toLocaleString("en-US")}`;
}

// 12.5 -> "$12.50", null -> "—" (cents-precise money display)
export function usdCents(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `$${n.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

// Signed money for P&L: 3.5 -> "+$3.50", -2 -> "-$2.00".
export function usdSigned(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  const sign = n < 0 ? "-" : "+";
  return `${sign}${usdCents(Math.abs(n))}`;
}

// Win-rate is delivered as a 0..100 number (or null). 63.2 -> "63%", null -> "—".
export function winRatePercent(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `${Math.round(n)}%`;
}

// True only when price is a usable probability strictly inside (0, 1).
export function isValidPrice(p: number | null | undefined): p is number {
  return typeof p === "number" && !Number.isNaN(p) && p > 0 && p < 1;
}

// shares ≈ amount / price (price must be valid). Returns null otherwise.
export function sharesFor(
  amount: number | null | undefined,
  price: number | null | undefined,
): number | null {
  if (amount == null || amount <= 0 || !isValidPrice(price)) return null;
  return amount / price;
}

// potential_payout ≈ shares * $1 == amount / price.
export function payoutFor(
  amount: number | null | undefined,
  price: number | null | undefined,
): number | null {
  return sharesFor(amount, price); // shares * $1 == shares
}

// Stable string hash -> hue 0..359.
function hashHue(str: string): number {
  let h = 0;
  for (let i = 0; i < str.length; i++) {
    h = (h << 5) - h + str.charCodeAt(i);
    h |= 0;
  }
  return Math.abs(h) % 360;
}

// Deterministic vivid gradient derived from a title.
export function gradientFor(title: string): string {
  const h1 = hashHue(title);
  const h2 = (h1 + 40) % 360;
  return `linear-gradient(150deg, hsl(${h1} 70% 32%) 0%, hsl(${h2} 75% 22%) 60%, hsl(${(h1 + 200) % 360} 60% 14%) 100%)`;
}

// Resolve a possibly-relative image path against the current origin.
export function imageUrl(path: string | null | undefined): string | null {
  if (!path) return null;
  return path; // backend returns same-origin paths like "/cards/x.png"
}
