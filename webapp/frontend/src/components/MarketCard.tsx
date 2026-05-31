import type { Category, Market } from "../api";
import { gradientFor, imageUrl, priceCents, volumeLabel } from "../format";

// Market card uses the category's image dimmed, else a gradient from the question.
export function marketBackground(market: Market, cat?: Category | null): string {
  const img = cat ? imageUrl(cat.image_url) : null;
  if (img) {
    return `linear-gradient(rgba(0,0,0,0.45), rgba(0,0,0,0.45)), url("${img}")`;
  }
  return gradientFor(market.question || market.id);
}

export function MarketCardContent({
  market,
  eyebrow,
}: {
  market: Market;
  eyebrow?: string | null;
}) {
  return (
    <>
      <div className="scrim" />
      <div className="card-content">
        {eyebrow ? <div className="eyebrow">{eyebrow}</div> : null}
        <h1 className="market-question">{market.question}</h1>
        <div className="pills">
          <div className="pill yes">
            <div className="label">YES</div>
            <div className="price">{priceCents(market.yes_price)}</div>
          </div>
          <div className="pill no">
            <div className="label">NO</div>
            <div className="price">{priceCents(market.no_price)}</div>
          </div>
        </div>
        <div className="meta-row">
          <span className="volume">{volumeLabel(market.volume)}</span>
        </div>
        <div className="hints">
          <span>↑↓ browse</span>
          <span>→ bet</span>
        </div>
      </div>
    </>
  );
}
