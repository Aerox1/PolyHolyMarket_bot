import type { Category } from "../api";
import { gradientFor, imageUrl, volumeLabel } from "../format";

export function categoryBackground(cat: Category): string {
  const img = imageUrl(cat.image_url);
  if (img) {
    return `url("${img}")`;
  }
  return gradientFor(cat.title);
}

export function CategoryCardContent({ cat }: { cat: Category }) {
  return (
    <>
      <div className="scrim" />
      <div className="card-content">
        <div className="eyebrow">Category</div>
        <h1 className="cat-title">{cat.title}</h1>
        <div className="meta-row">
          <span className="volume">{volumeLabel(cat.volume)}</span>
        </div>
        <div className="hints">
          <span>↑↓ browse</span>
          <span>← explore</span>
        </div>
      </div>
    </>
  );
}
