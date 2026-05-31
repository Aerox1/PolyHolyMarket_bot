// Thin, dependency-free wrapper around the global window.Telegram.WebApp.
// All access is guarded so the app also works in a plain browser (dev).

type HapticStyle = "light" | "medium" | "heavy" | "rigid" | "soft";

interface TgButton {
  show: () => void;
  hide: () => void;
  onClick: (cb: () => void) => void;
  offClick: (cb: () => void) => void;
  setText?: (t: string) => void;
  enable?: () => void;
  disable?: () => void;
  showProgress?: (leaveActive?: boolean) => void;
  hideProgress?: () => void;
}

interface TgWebApp {
  initData: string;
  themeParams: Record<string, string>;
  colorScheme?: "light" | "dark";
  ready: () => void;
  expand: () => void;
  close: () => void;
  BackButton: TgButton;
  MainButton: TgButton & {
    setParams?: (p: Record<string, unknown>) => void;
  };
  HapticFeedback?: {
    impactOccurred: (style: HapticStyle) => void;
    notificationOccurred?: (type: "error" | "success" | "warning") => void;
  };
}

declare global {
  interface Window {
    Telegram?: { WebApp?: TgWebApp };
  }
}

export function tg(): TgWebApp | undefined {
  return window.Telegram?.WebApp;
}

// initData header value. Falls back to "DEV" so the UI renders in a plain
// browser (the API will 401, which the app handles gracefully).
export function initDataHeader(): string {
  const data = tg()?.initData;
  return data && data.length > 0 ? data : "DEV";
}

export function isInsideTelegram(): boolean {
  const data = tg()?.initData;
  return !!data && data.length > 0;
}

export function haptic(style: HapticStyle = "light"): void {
  try {
    tg()?.HapticFeedback?.impactOccurred(style);
  } catch {
    /* no-op outside Telegram */
  }
}

export function notify(type: "error" | "success" | "warning"): void {
  try {
    tg()?.HapticFeedback?.notificationOccurred?.(type);
  } catch {
    /* no-op */
  }
}

export function bootTelegram(): void {
  const w = tg();
  if (!w) return;
  try {
    w.ready();
    w.expand();
  } catch {
    /* no-op */
  }
}

// Theme colors with sensible dark defaults.
export interface Theme {
  bg: string;
  text: string;
  hint: string;
  link: string;
  button: string;
  buttonText: string;
  secondaryBg: string;
}

export function theme(): Theme {
  const p = tg()?.themeParams ?? {};
  return {
    bg: p.bg_color || "#0e0f13",
    text: p.text_color || "#ffffff",
    hint: p.hint_color || "#9aa0aa",
    link: p.link_color || "#5e8bff",
    button: p.button_color || "#3390ec",
    buttonText: p.button_text_color || "#ffffff",
    secondaryBg: p.secondary_bg_color || "#1a1c22",
  };
}

// Show/hide & wire the native BackButton. Returns a cleanup fn.
export function useBackButton(visible: boolean, onBack: () => void): () => void {
  const w = tg();
  if (!w) return () => {};
  const handler = () => onBack();
  if (visible) {
    w.BackButton.onClick(handler);
    w.BackButton.show();
  } else {
    w.BackButton.hide();
  }
  return () => {
    try {
      w.BackButton.offClick(handler);
    } catch {
      /* no-op */
    }
  };
}

export function closeApp(): void {
  try {
    tg()?.close();
  } catch {
    /* no-op */
  }
}
