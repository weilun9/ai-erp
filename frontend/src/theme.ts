// Look of the app on this device: light frosted glass (default), dark, or follow the system,
// plus an accent colour. Kept in localStorage and set on <html> as data-theme / data-accent
// before the first render, so the page never flashes the wrong theme.

export type ThemeMode = "light" | "dark" | "system";
export const ACCENTS = ["blue", "purple", "pink", "red", "orange", "green", "graphite"] as const;
export type Accent = (typeof ACCENTS)[number];
export const ACCENT_COLOURS: Record<Accent, string> = {
  blue: "#0A84FF",
  purple: "#8E5CF7",
  pink: "#F0468E",
  red: "#FF453A",
  orange: "#F7821B",
  green: "#2FB350",
  graphite: "#8C8C91",
};

const KEY = "aibutler.theme";
const dark = () => window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;

export interface Look {
  mode: ThemeMode;
  accent: Accent;
}

export function readLook(): Look {
  try {
    const x = JSON.parse(localStorage.getItem(KEY) ?? "{}");
    return {
      mode: ["light", "dark", "system"].includes(x.mode) ? x.mode : "light",
      accent: ACCENTS.includes(x.accent) ? x.accent : "blue",
    };
  } catch {
    return { mode: "light", accent: "blue" };
  }
}

export function applyLook(look: Look = readLook()): void {
  const root = document.documentElement;
  const theme = look.mode === "system" ? (dark() ? "dark" : "light") : look.mode;
  root.dataset.theme = theme;
  root.dataset.accent = look.accent;
  document.querySelector('meta[name="theme-color"]')?.setAttribute(
    "content",
    theme === "dark" ? "#0A0A0B" : "#EEF0F5",
  );
}

export function saveLook(look: Look): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(look));
  } catch {
    /* private window: the choice lasts for this visit only */
  }
  applyLook(look);
}

/** Once at start-up: apply the saved look and follow the system when asked to. */
export function startLook(): void {
  applyLook();
  window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (readLook().mode === "system") applyLook();
  });
}
