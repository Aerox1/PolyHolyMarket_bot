import type { ReactNode } from "react";

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="status">
      <div className="spinner" />
      <p>{label}</p>
    </div>
  );
}

export function StatusScreen({
  emoji,
  title,
  message,
  actionLabel,
  onAction,
}: {
  emoji: string;
  title: string;
  message?: ReactNode;
  actionLabel?: string;
  onAction?: () => void;
}) {
  return (
    <div className="status">
      <div className="big">{emoji}</div>
      <h1>{title}</h1>
      {message ? <p>{message}</p> : null}
      {actionLabel && onAction ? (
        <button onClick={onAction}>{actionLabel}</button>
      ) : null}
    </div>
  );
}
