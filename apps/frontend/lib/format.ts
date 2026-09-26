// Presentation helpers shared across the operator UI screens.

export const TERMINAL_STATUSES = ["complete", "failed"] as const;

export function isTerminal(status: string): boolean {
  return status === "complete" || status === "failed";
}

const STATUS_LABELS: Record<string, string> = {
  queued: "Queued",
  crawling: "Crawling",
  collecting_performance: "Collecting performance",
  extracting: "Extracting",
  scoring: "Scoring",
  commenting: "Commenting",
  validating: "Validating",
  rendering: "Rendering",
  complete: "Complete",
  failed: "Failed",
};

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] || status;
}

// Maps a status to a badge tone used for colouring in globals.css.
export function statusTone(status: string): "success" | "danger" | "progress" | "neutral" {
  if (status === "complete") return "success";
  if (status === "failed") return "danger";
  if (status === "queued") return "neutral";
  return "progress";
}

// Scores share the report's three colour bands so the UI matches the PDF.
export function scoreTone(score: number | null): "strong" | "fair" | "weak" | "empty" {
  if (score === null || score === undefined) return "empty";
  if (score >= 75) return "strong";
  if (score >= 50) return "fair";
  return "weak";
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
