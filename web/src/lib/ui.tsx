/**
 * Shared bits in the pool app's style. Status always pairs colour with a
 * glyph and words: on the light surface the warning colour is below 3:1, and
 * colour-blind readers can't tell good from critical by hue alone.
 */

export type Level = "good" | "warning" | "serious" | "critical" | "info" | "neutral";

const MARK: Record<Level, { color: string; glyph: string; ink: string }> = {
  good: { color: "var(--status-good)", glyph: "✓", ink: "#fff" },
  warning: { color: "var(--status-warning)", glyph: "!", ink: "#0b0b0b" },
  serious: { color: "var(--status-serious)", glyph: "▲", ink: "#0b0b0b" },
  critical: { color: "var(--status-critical)", glyph: "✕", ink: "#fff" },
  info: { color: "var(--accent)", glyph: "i", ink: "#fff" },
  neutral: { color: "var(--ink-muted)", glyph: "–", ink: "#fff" },
};

export function StatusMark({ level, className = "" }: { level: Level; className?: string }) {
  const m = MARK[level];
  return (
    <span
      aria-hidden
      className={`inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-[9px] font-bold ${className}`}
      style={{ background: m.color, color: m.ink }}
    >
      {m.glyph}
    </span>
  );
}

/** A small pill: glyph and label, for run outcomes. */
export function StatusPill({ level, children }: { level: Level; children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-surface-2 py-0.5 pr-2 pl-0.5 text-xs font-medium text-ink">
      <StatusMark level={level} />
      {children}
    </span>
  );
}

const BANNER: Record<"error" | "warn" | "info" | "good", string> = {
  error: "border-[var(--status-critical)]/40 bg-[var(--status-critical)]/8",
  warn: "border-[var(--status-warning)]/50 bg-[var(--status-warning)]/10",
  info: "border-accent/40 bg-accent/8",
  good: "border-[var(--status-good)]/40 bg-[var(--status-good)]/8",
};
const BANNER_LEVEL = { error: "critical", warn: "warning", info: "info", good: "good" } as const;

export function Banner({ tone, children }: { tone: keyof typeof BANNER; children: React.ReactNode }) {
  return (
    <div className={`flex items-start gap-2 rounded-2xl border p-4 text-sm text-ink ${BANNER[tone]}`}>
      <StatusMark level={BANNER_LEVEL[tone]} className="mt-0.5" />
      <div className="min-w-0">{children}</div>
    </div>
  );
}

/** An inline warning line: dark text beside a warning mark (the yellow is too pale for text). */
export function Warning({ children }: { children: React.ReactNode }) {
  return (
    <p className="flex items-start gap-1.5 text-sm text-ink">
      <StatusMark level="warning" className="mt-0.5" />
      <span>{children}</span>
    </p>
  );
}

/** A form message: red with a mark when it's an error, quiet otherwise. */
export function Note({ children, error }: { children: React.ReactNode; error?: boolean }) {
  return error ? (
    <p className="flex items-start gap-1.5 text-sm text-[var(--status-critical)]">
      <StatusMark level="critical" className="mt-0.5" />
      <span>{children}</span>
    </p>
  ) : (
    <p className="text-sm text-ink-2">{children}</p>
  );
}
