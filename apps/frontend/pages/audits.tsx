import { useAuth } from "@clerk/nextjs";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import Layout from "../components/Layout";
import SearchConsoleIntegration from "../components/SearchConsoleIntegration";
import {
  ApiError,
  AuditListItem,
  ReportFormat,
  downloadReport,
  listAudits,
} from "../lib/api";
import { formatDate, isTerminal, scoreTone, statusLabel, statusTone } from "../lib/format";

type StatusFilter = "all" | "complete" | "in_progress" | "failed";
type SortKey = "newest" | "oldest" | "lead_desc" | "seo_desc" | "uxui_desc";

type ScoreField = "lead_gen_score" | "seo_score" | "uxui_score";

function ScoreCell({ score }: { score: number | null }) {
  if (score === null || score === undefined) return <span className="muted">—</span>;
  return <span className={`score-chip tone-${scoreTone(score)}`}>{score}</span>;
}

function RowScores({ audit }: { audit: AuditListItem }) {
  if (audit.status === "failed") return <span className="tag tag-danger">Failed</span>;
  if (!isTerminal(audit.status)) return <span className="tag tag-progress">In progress</span>;
  if (audit.audit_type === "social") {
    if (audit.social_score === null || audit.social_score === undefined)
      return <span className="tag tag-neutral">Incomplete</span>;
    return (
      <div className="score-cells">
        <ScoreCell score={audit.social_score} />
      </div>
    );
  }
  if (audit.seo_score === null) return <span className="tag tag-neutral">Incomplete</span>;
  return (
    <div className="score-cells">
      {audit.audit_type === "combined" &&
        audit.overall_score !== null &&
        audit.overall_score !== undefined && <ScoreCell score={audit.overall_score} />}
      <ScoreCell score={audit.lead_gen_score} />
      <ScoreCell score={audit.seo_score} />
      <ScoreCell score={audit.uxui_score} />
    </div>
  );
}

function scoreOf(audit: AuditListItem, field: ScoreField): number {
  // Missing scores sort last in descending order.
  return audit[field] ?? -1;
}

export default function AuditsHistoryPage() {
  const { getToken } = useAuth();
  const [audits, setAudits] = useState<AuditListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [sortKey, setSortKey] = useState<SortKey>("newest");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const token = await getToken();
      const response = await listAudits(100, token);
      setAudits(response.audits);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not load audit history. Try again.",
      );
    } finally {
      setLoading(false);
    }
  }, [getToken]);

  // Filter + sort happen client-side over the loaded page of audits.
  const visibleAudits = useMemo(() => {
    if (!audits) return [];
    const q = query.trim().toLowerCase();
    const filtered = audits.filter((audit) => {
      if (q && !audit.url.toLowerCase().includes(q)) return false;
      if (statusFilter === "complete") return audit.status === "complete";
      if (statusFilter === "failed") return audit.status === "failed";
      if (statusFilter === "in_progress") return !isTerminal(audit.status);
      return true;
    });
    return [...filtered].sort((a, b) => {
      switch (sortKey) {
        case "oldest":
          return a.created_at.localeCompare(b.created_at);
        case "lead_desc":
          return scoreOf(b, "lead_gen_score") - scoreOf(a, "lead_gen_score");
        case "seo_desc":
          return scoreOf(b, "seo_score") - scoreOf(a, "seo_score");
        case "uxui_desc":
          return scoreOf(b, "uxui_score") - scoreOf(a, "uxui_score");
        case "newest":
        default:
          return b.created_at.localeCompare(a.created_at);
      }
    });
  }, [audits, query, statusFilter, sortKey]);

  async function handleDownload(jobId: string, format: ReportFormat) {
    setError(null);
    setDownloading(`${jobId}:${format}`);
    try {
      const token = await getToken();
      const { blob, filename } = await downloadReport(jobId, format, token);
      const objectUrl = window.URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.URL.revokeObjectURL(objectUrl);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not download the report.");
    } finally {
      setDownloading(null);
    }
  }

  useEffect(() => {
    load();
  }, [load]);

  const total = audits?.length ?? 0;

  return (
    <Layout title="Audit History | BLC Website Audit">
      <div className="page-wide">
        <div className="detail-header">
          <div>
            <p className="eyebrow">Audit History</p>
            <h1>Recent audits</h1>
          </div>
          <div className="header-actions">
            <button type="button" className="btn btn-secondary" onClick={load} disabled={loading}>
              {loading ? "Refreshing…" : "Refresh"}
            </button>
            <Link href="/" className="btn btn-primary">
              New audit
            </Link>
          </div>
        </div>

        {error && (
          <div className="alert alert-danger" role="alert">
            {error}
          </div>
        )}

        <SearchConsoleIntegration />

        {audits === null && !error && (
          <div className="card muted-card">
            <span className="spinner" aria-hidden="true" /> Loading audits…
          </div>
        )}

        {audits !== null && total === 0 && (
          <div className="card empty-state">
            <p>No audits yet.</p>
            <Link href="/" className="btn btn-primary">
              Submit your first audit
            </Link>
          </div>
        )}

        {audits !== null && total > 0 && (
          <>
            <div className="history-toolbar">
              <input
                type="search"
                className="history-search"
                placeholder="Filter by website URL…"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                aria-label="Filter audits by website URL"
              />
              <select
                className="history-select"
                value={statusFilter}
                onChange={(event) => setStatusFilter(event.target.value as StatusFilter)}
                aria-label="Filter by status"
              >
                <option value="all">All statuses</option>
                <option value="complete">Complete</option>
                <option value="in_progress">In progress</option>
                <option value="failed">Failed</option>
              </select>
              <select
                className="history-select"
                value={sortKey}
                onChange={(event) => setSortKey(event.target.value as SortKey)}
                aria-label="Sort audits"
              >
                <option value="newest">Newest first</option>
                <option value="oldest">Oldest first</option>
                <option value="lead_desc">Lead score (high → low)</option>
                <option value="seo_desc">SEO score (high → low)</option>
                <option value="uxui_desc">UX score (high → low)</option>
              </select>
              <span className="history-count">
                {visibleAudits.length} of {total}
              </span>
            </div>

            {visibleAudits.length === 0 ? (
              <div className="card empty-state">
                <p>No audits match the current filters.</p>
              </div>
            ) : (
              <div className="card table-card">
                <table className="audit-table">
                  <thead>
                    <tr>
                      <th>Website</th>
                      <th>Status</th>
                      <th>Submitted</th>
                      <th>
                        Scores <span className="th-hint">Overall · Lead · SEO · UX · Social</span>
                      </th>
                      <th className="col-actions">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleAudits.map((audit) => (
                      <tr key={audit.job_id}>
                        <td className="col-url">
                          <span
                            className={`badge badge-${
                              audit.audit_type === "social"
                                ? "progress"
                                : audit.audit_type === "combined"
                                  ? "success"
                                  : "neutral"
                            }`}
                          >
                            {audit.audit_type === "social"
                              ? "Social"
                              : audit.audit_type === "combined"
                                ? "Full"
                                : "Web"}
                          </span>{" "}
                          <Link href={`/audit/${audit.job_id}`} className="url-link">
                            {audit.url}
                          </Link>
                        </td>
                        <td>
                          <span className={`badge badge-${statusTone(audit.status)}`}>
                            {statusLabel(audit.status)}
                          </span>
                        </td>
                        <td className="muted">{formatDate(audit.created_at)}</td>
                        <td>
                          <RowScores audit={audit} />
                        </td>
                        <td className="col-actions">
                          <Link href={`/audit/${audit.job_id}`} className="link-action">
                            Details
                          </Link>
                          {audit.report_available && (
                            <>
                              <button
                                type="button"
                                className="link-action button-link"
                                onClick={() => handleDownload(audit.job_id, "pdf")}
                                disabled={downloading !== null}
                              >
                                {downloading === `${audit.job_id}:pdf` ? "PDF..." : "PDF"}
                              </button>
                              <button
                                type="button"
                                className="link-action button-link"
                                onClick={() => handleDownload(audit.job_id, "docx")}
                                disabled={downloading !== null}
                              >
                                {downloading === `${audit.job_id}:docx` ? "DOCX..." : "DOCX"}
                              </button>
                            </>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </div>
    </Layout>
  );
}
