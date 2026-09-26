import { useAuth } from "@clerk/nextjs";
import Link from "next/link";
import { useRouter } from "next/router";
import { useEffect, useState } from "react";

import Layout from "../../components/Layout";
import {
  AiVisibility,
  ApiError,
  AuditDetail,
  AuditShareResponse,
  OverallReadiness,
  ReportFormat,
  ReportPayload,
  ReportSection,
  RoadmapTier,
  ScoreCard,
  SocialReport,
  downloadReport,
  getAuditDetail,
  rerunAiVisibility,
  rerunAuditEnrichment,
  revokeShare,
  shareAudit,
  shareUrlFromPath,
} from "../../lib/api";
import { formatDate, isTerminal, scoreTone, statusLabel, statusTone } from "../../lib/format";

const POLL_INTERVAL_MS = 2500;
const RETRY_INTERVAL_MS = 4000;

const PIPELINE: { status: string; label: string }[] = [
  { status: "queued", label: "Queued" },
  { status: "crawling", label: "Crawl" },
  { status: "collecting_performance", label: "Performance" },
  { status: "extracting", label: "Extract" },
  { status: "scoring", label: "Score" },
  { status: "commenting", label: "Commentary" },
  { status: "validating", label: "Validate" },
  { status: "rendering", label: "Render" },
  { status: "complete", label: "Complete" },
];

function ScoreCards({ scores }: { scores: ScoreCard[] }) {
  return (
    <div className="score-grid">
      {scores.map((card) => (
        <div key={card.id} className={`score-card tone-${scoreTone(card.score)}`}>
          <p className="score-label">{card.label}</p>
          <p className="score-value">
            {card.score}
            <span className="score-max">/ {card.max_score}</span>
          </p>
          <p className="score-desc">{card.description}</p>
        </div>
      ))}
    </div>
  );
}

function WebsiteScopeBlock({ report }: { report: ReportPayload }) {
  const scope = report.website_scope;
  if (!scope) return null;
  const rows: [string, number | null][] = [
    ["Pages discovered", scope.pages_discovered],
    ["Pages analyzed in depth", scope.pages_analyzed],
    ["Blog / article posts", scope.blog_posts],
    ["Sitemap entries", scope.sitemap_entries],
    ["Outbound links", scope.outbound_links],
    ["Images", scope.images],
  ];
  const shown = rows.filter(([, value]) => value != null);
  if (shown.length === 0) return null;
  return (
    <section className="card section-block">
      <h3>What your website consists of</h3>
      <p className="muted">
        A snapshot of the whole site the audit discovered — pages, posts, sitemap, outbound links,
        and images. Discovered counts come from the site&apos;s internal links plus its sitemap; the
        audit analyzes the most important pages in depth.
      </p>
      <div className="meta-grid">
        {shown.map(([label, value]) => (
          <div key={label}>
            <h4>{label}</h4>
            <p className="muted">{value}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

function SectionBlock({ section }: { section: ReportSection }) {
  return (
    <section className="card section-block">
      <div className="section-head">
        <h3>{section.label}</h3>
        {section.score !== null && (
          <span className={`pill tone-${scoreTone(section.score)}`}>{section.score}/100</span>
        )}
      </div>
      <p className="section-headline">{section.headline}</p>

      {section.findings.length > 0 && (
        <div className="section-sub">
          <h4>Findings</h4>
          {/* One card per issue: the fix ("Do this") travels with its finding; the
              roadmap groups the same fixes by timeline, so a separate Recommendations
              list would repeat every sentence. Older stored results without
              action_items fall back to the recommendations list below —
              show_recommendations is computed once server-side so PDF/DOCX/UI agree. */}
          <ul className="finding-list">
            {section.findings.map((finding, index) => (
              <li key={index}>
                <span className={`sev sev-${finding.severity}`}>{finding.severity}</span>
                <div>
                  <strong>{finding.title}</strong>
                  <p>{finding.explanation}</p>
                  {(finding.action_items ?? []).length > 0 && (
                    <ul className="action-list">
                      {(finding.action_items ?? []).map((item, itemIndex) => (
                        <li key={itemIndex}>Do this: {item}</li>
                      ))}
                    </ul>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {(section.show_recommendations ?? false) && (
          <div className="section-sub">
            <h4>Recommendations</h4>
            <ul className="rec-list">
              {section.recommendations.map((rec, index) => (
                <li key={index}>
                  <strong>{rec.title}</strong>
                  <p>{rec.rationale}</p>
                  {rec.action_items.length > 0 && (
                    <ul className="action-list">
                      {rec.action_items.map((item, itemIndex) => (
                        <li key={itemIndex}>{item}</li>
                      ))}
                    </ul>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
    </section>
  );
}

function RoadmapBlock({ roadmap }: { roadmap: RoadmapTier[] }) {
  return (
    <section className="card roadmap-block">
      <h3>Lead generation roadmap</h3>
      <div className="roadmap-grid">
        {roadmap.map((tier) => (
          <div key={tier.tier} className="roadmap-tier-ui">
            <h4>{tier.label}</h4>
            {tier.recommendations.length > 0 ? (
              <ol>
                {tier.recommendations.map((recommendation, index) => (
                  <li key={`${recommendation.title}-${index}`}>
                    <strong>{recommendation.title}</strong>
                    {/* New-format results explain each issue once, on its finding card;
                        legacy stored results keep the rationale (their only context). */}
                    {recommendation.action_items.length > 0 ? (
                      <>
                        <ul className="action-list">
                          {recommendation.action_items.map((item, itemIndex) => (
                            <li key={itemIndex}>{item}</li>
                          ))}
                        </ul>
                        <p className="muted">
                          Details: see this item&apos;s finding card in the SEO / UX-UI sections.
                        </p>
                      </>
                    ) : (
                      <p>{recommendation.rationale}</p>
                    )}
                  </li>
                ))}
              </ol>
            ) : (
              <p className="muted">No recommendations generated for this tier.</p>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

function recordNumberText(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  return typeof value === "number" ? String(value) : "N/A";
}

function recordText(record: Record<string, unknown>, key: string, fallback = "N/A"): string {
  const value = record[key];
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number") return String(value);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return fallback;
}

function ctrText(record: Record<string, unknown>): string {
  const value = record.ctr;
  return typeof value === "number" ? `${(value * 100).toFixed(1)}%` : "N/A";
}

function AccessibilityBlock({ report }: { report: NonNullable<AuditDetail["report"]> }) {
  const a11y = report.accessibility_advisory_section;
  if (!a11y || a11y.status !== "complete") {
    return null;
  }
  const impactLevels = ["critical", "serious", "moderate", "minor"];
  return (
    <section className="card">
      <div className="section-head">
        <h3>Accessibility (advisory)</h3>
        <span className="pill">axe-core {a11y.axe_version}</span>
      </div>
      <p className="muted">{a11y.disclaimer}</p>
      <div className="meta-grid">
        {impactLevels.map((level) => (
          <div key={level}>
            <h4>{level}</h4>
            <p>{a11y.impact_counts[level] ?? 0}</p>
          </div>
        ))}
      </div>
      {a11y.notes.map((note) => (
        <p className="muted" key={note}>
          {note}
        </p>
      ))}
      {a11y.issues.length > 0 ? (
        <ul>
          {a11y.issues.map((issue) => (
            <li key={issue.rule_id}>
              <strong>{issue.help || issue.rule_id}</strong>{" "}
              <span className="muted">
                ({issue.impact}, {issue.instances} affected
                {issue.wcag_criteria.length > 0 ? ` · ${issue.wcag_criteria.join(", ")}` : ""})
              </span>
              {issue.failure_summary && <p className="muted">{issue.failure_summary}</p>}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">
          The scan flagged no issues beyond the accessibility checks already scored above.
        </p>
      )}
      {a11y.needs_review_count > 0 && (
        <p className="muted">{a11y.needs_review_count} item(s) need manual review.</p>
      )}
    </section>
  );
}

function ExternalSeoBlock({
  report,
}: {
  report: NonNullable<AuditDetail["report"]>;
}) {
  const technical = report.technical_seo_section;
  const search = report.search_performance_section;
  const summary = report.external_seo_summary;
  const technicalAvailable = technical.status === "complete";
  const searchAvailable = search.status === "complete";

  return (
    <section className="card external-seo-block">
      <div className="section-head">
        <div>
          <h3>External SEO intelligence</h3>
          <p className="section-headline">
            Site-wide technical crawl facts and Google Search Console opportunities.
          </p>
        </div>
        <span className="pill">{summary.status}</span>
      </div>

      <div className="external-status-grid">
        <div>
          <span>{summary.technical_crawl_tool || "Technical crawl"}</span>
          <strong>{summary.technical_crawl_status}</strong>
        </div>
        <div>
          <span>Search Console</span>
          <strong>{summary.gsc_status}</strong>
        </div>
        <div>
          <span>URL inspection</span>
          <strong>{summary.url_inspection_status}</strong>
        </div>
        <div>
          <span>Opportunities</span>
          <strong>{searchAvailable ? summary.search_opportunity_count : "N/A"}</strong>
        </div>
      </div>

      <div className="enrichment-grid">
        <div>
          <h4>Technical SEO</h4>
          <p className="muted">
            {technicalAvailable
              ? `${recordNumberText(technical.summary, "urls_crawled")} URLs crawled · ${
                  technical.issues.length
                } issue groups · ${recordNumberText(
                  technical.summary,
                  "non_indexable_internal_urls",
                )} non-indexable`
              : `Status: ${technical.status_label}${
                  technical.reason_label ? ` — ${technical.reason_label}` : ""
                }`}
          </p>
          {technical.notes.map((note) => (
            <p className="muted" key={note}>
              Coverage note: {note}
            </p>
          ))}
          {technicalAvailable && technical.issues.length > 0 ? (
            <table className="insight-table">
              <thead>
                <tr>
                  <th>Issue</th>
                  <th>Count</th>
                </tr>
              </thead>
              <tbody>
                {technical.issues.slice(0, 6).map((issue) => (
                  <tr key={issue.id}>
                    <td>
                      <span className={`sev sev-${issue.severity}`}>{issue.severity}</span>
                      <strong>{issue.title}</strong>
                      <div className="issue-guidance">
                        <p>
                          <b>What it means:</b> {issue.summary}
                        </p>
                        <p>
                          <b>Why it matters:</b> {issue.why_it_matters}
                        </p>
                        <p>
                          <b>Recommended fix:</b> {issue.recommended_fix}
                        </p>
                      </div>
                      {issue.examples.length > 0 && (
                        <div className="issue-locations">
                          <span>{issue.location_label}</span>
                          <ul>
                            {issue.examples.slice(0, 3).map((example) => (
                              <li key={example}>{example}</li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </td>
                    <td>{issue.count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : technicalAvailable ? (
            <p className="muted">
              The technical crawl completed and did not find issue groups that matched the
              report thresholds.
            </p>
          ) : (
            <p className="muted">
              Technical crawl data is not available for this audit, so no clean-or-broken
              technical SEO claims are shown.
            </p>
          )}
        </div>

        <div>
          <h4>Search performance</h4>
          <p className="muted">
            {searchAvailable
              ? `${search.site_url || "Matched Search Console property"} · ${recordNumberText(
                  search.summary,
                  "top_query_count",
                )} queries · ${recordNumberText(search.summary, "top_page_count")} pages`
              : `Status: ${search.status_label}${
                  search.reason_label ? ` — ${search.reason_label}` : ""
                }`}
          </p>
          {searchAvailable && search.ranking_opportunities.length > 0 ? (
            <table className="insight-table">
              <thead>
                <tr>
                  <th>Query</th>
                  <th>Position</th>
                  <th>CTR</th>
                </tr>
              </thead>
              <tbody>
                {search.ranking_opportunities.slice(0, 6).map((row, index) => (
                  <tr key={`${recordText(row, "query")}-${index}`}>
                    <td>{recordText(row, "query")}</td>
                    <td>{recordText(row, "position")}</td>
                    <td>{ctrText(row)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : searchAvailable ? (
            <p className="muted">
              Search Console completed and did not return ranking opportunities that matched
              the report thresholds.
            </p>
          ) : (
            <p className="muted">Search Console data is not available for this audit.</p>
          )}
        </div>
      </div>
    </section>
  );
}

function ProgressView({ detail }: { detail: AuditDetail }) {
  const currentIndex = PIPELINE.findIndex((step) => step.status === detail.status);
  return (
    <div className="card progress-card">
      <div className="progress-meta">
        <span className="spinner spinner-lg" aria-hidden="true" />
        <div>
          <p className="progress-stage">{detail.current_stage || statusLabel(detail.status)}</p>
          <p className="progress-hint">This page refreshes automatically while the audit runs.</p>
        </div>
        <span className="progress-pct">{detail.progress_pct}%</span>
      </div>
      <div className="progress-track" role="progressbar" aria-valuenow={detail.progress_pct}>
        <div className="progress-fill" style={{ width: `${detail.progress_pct}%` }} />
      </div>
      <ol className="stepper">
        {PIPELINE.map((step, index) => {
          const state =
            currentIndex < 0
              ? "todo"
              : index < currentIndex
                ? "done"
                : index === currentIndex
                  ? "active"
                  : "todo";
          return (
            <li key={step.status} className={`step step-${state}`}>
              <span className="step-dot" aria-hidden="true" />
              {step.label}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function OverallReadinessBlock({ overall }: { overall: OverallReadiness }) {
  return (
    <section className="card section-block">
      <div className="section-head">
        <h3>Overall Lead-Gen Readiness</h3>
      </div>
      <div className="score-grid">
        <div className={`score-card tone-${scoreTone(overall.score)}`}>
          <p className="score-label">Overall Lead-Gen Readiness</p>
          <p className="score-value">
            {overall.score ?? "—"}
            <span className="score-max">/ 100</span>
          </p>
          <p className="score-desc">
            Website {overall.inputs.website_lead_gen ?? "—"} (
            {Math.round(overall.weights.website * 100)}%) · Social {overall.inputs.social ?? "—"} (
            {Math.round(overall.weights.social * 100)}%)
          </p>
        </div>
      </div>
    </section>
  );
}

function AiVisibilityBlock({ aiv }: { aiv: AiVisibility }) {
  return (
    <section className="card section-block">
      <div className="section-head">
        <h3>AI Visibility</h3>
      </div>
      {aiv.unavailable ? (
        <p className="muted">
          {aiv.message ?? "AI Visibility data could not be retrieved for this report."}
        </p>
      ) : (
        <AiVisibilityData aiv={aiv} />
      )}
    </section>
  );
}

function AiVisibilityData({ aiv }: { aiv: AiVisibility }) {
  return (
    <>
      <p className="muted">
        How this brand appears in AI-generated answers (ChatGPT, Google AI Overviews / AI Mode,
        Gemini, Perplexity), from the Semrush AI Visibility Toolkit. Presentation only — it does not
        change any audit score. This measures brand mentions in AI answers to a tracked sample of
        prompts — it is not website traffic, so analytics can still record visits from AI tools
        (organic referrals or AI ads) even when tracked visibility is near zero.
      </p>
      {aiv.visibility_score !== null && aiv.visibility_score !== undefined && (
        <div className="score-grid">
          <div className={`score-card tone-${scoreTone(aiv.visibility_score)}`}>
            <p className="score-label">AI Visibility Score</p>
            <p className="score-value">
              {aiv.visibility_score}
              <span className="score-max">/ 100</span>
            </p>
            {aiv.visibility_band && <p className="score-desc">{aiv.visibility_band}</p>}
          </div>
        </div>
      )}
      {aiv.metrics.length > 0 && (
        <ul className="fact-list">
          {aiv.metrics.map((m) => (
            <li key={m.key}>
              <strong>{m.label}:</strong> {m.value}
            </li>
          ))}
        </ul>
      )}
      {aiv.per_platform.length > 0 && (
        <>
          <h4>Distribution by AI platform</h4>
          <ul className="fact-list">
            {aiv.per_platform.map((p) => (
              <li key={p.platform}>
                <strong>{p.platform}:</strong> mentions {p.mentions ?? "—"} · share{" "}
                {p.share_display ?? "—"}
              </li>
            ))}
          </ul>
        </>
      )}
      {aiv.topics.length > 0 && (
        <>
          <h4>Top performing topics</h4>
          <ul className="fact-list">
            {aiv.topics.map((t) => (
              <li key={t.topic}>
                <strong>{t.topic}:</strong> visibility {t.visibility ?? "—"} · your mentions{" "}
                {t.your_mentions ?? "—"} · AI volume {t.ai_volume || "—"}
              </li>
            ))}
          </ul>
        </>
      )}
      {aiv.competitors.length > 0 && (
        <>
          <h4>Competitors in AI answers</h4>
          <ul className="fact-list">
            {aiv.competitors.map((c) => (
              <li key={c.label}>
                <strong>{c.label}:</strong> visibility {c.visibility_score ?? "—"} · mentions{" "}
                {c.mentions ?? "—"}
              </li>
            ))}
          </ul>
        </>
      )}
      {aiv.by_country.length > 0 && (
        <>
          <h4>Mentions by country</h4>
          <ul className="fact-list">
            {aiv.by_country.map((c) => (
              <li key={c.country}>
                <strong>{c.country}:</strong> mentions {c.mentions ?? "—"} · share{" "}
                {c.share_display ?? "—"}
              </li>
            ))}
          </ul>
        </>
      )}
      <p className="muted">
        Source: Semrush AI Visibility Toolkit{aiv.retrieved_at ? `, retrieved ${aiv.retrieved_at}` : ""}.
      </p>
    </>
  );
}

function SocialReportView({ report }: { report: SocialReport }) {
  // The server nulls content_insights when every field is missing, so plain truthiness is the
  // whole "anything to show" check. Same for google_business (combined audits only) — the
  // listing the Google-reviews and phone (NAP) checks were scored against.
  const ci = report.content_insights;
  const gb = report.google_business;
  // Same complete/partial gate the PDF and DOCX enforce: on a failed/skipped collection the
  // report must say the profiles could not be collected — never "no major issues" — and must
  // not render body sections (GBP card) the other surfaces suppress.
  const collected = report.status === "complete" || report.status === "partial";
  return (
    <>
      <div className="score-grid">
        <div className={`score-card tone-${scoreTone(report.score)}`}>
          <p className="score-label">Social Score</p>
          <p className="score-value">
            {report.score ?? "—"}
            <span className="score-max">/ 100</span>
          </p>
          <p className="score-desc">
            {Object.entries(report.handles)
              .map(([platform, handle]) => `${platform}: @${handle}`)
              .join(" · ")}
          </p>
        </div>
      </div>

      {report.executive_summary && (
        <section className="card section-block">
          <div className="section-head">
            <h3>Summary</h3>
          </div>
          <p className="summary-text">{report.executive_summary}</p>
        </section>
      )}

      {!collected ? (
        <section className="card">
          <p className="muted">
            The social profiles could not be collected for this audit, so no Social Score or
            findings are available. Check that the handles are public and spelled correctly,
            then re-run the audit.
          </p>
        </section>
      ) : report.findings.length > 0 ? (
        <section className="card section-block">
          <div className="section-head">
            <h3>What to improve</h3>
          </div>
          <ul className="finding-list">
            {report.findings.map((finding) => (
              <li key={finding.id}>
                <span className={`sev sev-${finding.impact}`}>{finding.impact}</span>
                <div>
                  <strong>{finding.label}</strong>
                  {finding.metric && <p className="muted">{finding.metric}</p>}
                  {finding.narrative ? (
                    <p>{finding.narrative}</p>
                  ) : (
                    finding.remediation && <p>{finding.remediation}</p>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </section>
      ) : (
        <section className="card">
          <p className="muted">No major issues detected on the audited profiles.</p>
        </section>
      )}

      {report.strengths && report.strengths.length > 0 && (
        <section className="card section-block">
          <div className="section-head">
            <h3>What&apos;s working</h3>
          </div>
          <ul className="finding-list">
            {report.strengths.map((strength) => (
              <li key={strength.id}>
                <span className="sev sev-low">ok</span>
                <div>
                  <strong>{strength.label}</strong>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      {ci && (
        <section className="card">
          <h3>Content insights</h3>
          <table className="audit-table">
            <tbody>
              {ci.content_mix.video != null && (
                <tr>
                  <td>Video share</td>
                  <td>{ci.content_mix.video}%</td>
                </tr>
              )}
              {ci.content_mix.image != null && (
                <tr>
                  <td>Image share</td>
                  <td>{ci.content_mix.image}%</td>
                </tr>
              )}
              {ci.content_mix.carousel != null && (
                <tr>
                  <td>Carousel share</td>
                  <td>{ci.content_mix.carousel}%</td>
                </tr>
              )}
              {ci.total_views != null && (
                <tr>
                  <td>Lifetime YouTube views</td>
                  <td>{ci.total_views.toLocaleString()}</td>
                </tr>
              )}
              {ci.avg_views_per_post != null && (
                <tr>
                  <td>Avg views / video</td>
                  <td>{ci.avg_views_per_post.toLocaleString()}</td>
                </tr>
              )}
              {ci.avg_engagement_rate_pct != null && (
                <tr>
                  <td>Avg engagement</td>
                  <td>{ci.avg_engagement_rate_pct}%</td>
                </tr>
              )}
              {ci.avg_like_to_comment_ratio != null && (
                <tr>
                  <td>Likes / comment</td>
                  <td>{ci.avg_like_to_comment_ratio}</td>
                </tr>
              )}
              {ci.avg_hashtags_per_post != null && (
                <tr>
                  <td>Hashtags / post</td>
                  <td>{ci.avg_hashtags_per_post}</td>
                </tr>
              )}
              {ci.posts_with_cta_caption_pct != null && (
                <tr>
                  <td>Captions with CTA</td>
                  <td>{ci.posts_with_cta_caption_pct}%</td>
                </tr>
              )}
              {ci.max_posting_gap_days != null && (
                <tr>
                  <td>Longest posting gap</td>
                  <td>{ci.max_posting_gap_days} days</td>
                </tr>
              )}
              {ci.avg_follower_following_ratio != null && (
                <tr>
                  <td>Follower / following ratio</td>
                  <td>{ci.avg_follower_following_ratio}×</td>
                </tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      {collected && report.connected_youtube && (
        <section className="card">
          <h3>Connected YouTube analytics</h3>
          {/* Lines are precomposed by the shared builder so UI/PDF/DOCX prose can't drift. */}
          <p className="muted">{report.connected_youtube.meta}</p>
          <ul className="finding-list">
            {report.connected_youtube.lines.map((line, index) => (
              <li key={index}>{line}</li>
            ))}
          </ul>
        </section>
      )}

      {collected && gb && (
        <section className="card">
          <h3>Google Business Profile</h3>
          <table className="audit-table">
            <tbody>
              {gb.name && (
                <tr>
                  <td>Listing</td>
                  <td>
                    {gb.name}
                    {gb.category ? ` (${gb.category})` : ""}
                  </td>
                </tr>
              )}
              {gb.rating != null && (
                <tr>
                  <td>Rating</td>
                  <td>{gb.rating} / 5</td>
                </tr>
              )}
              {gb.review_count != null && (
                <tr>
                  <td>Google reviews</td>
                  <td>{gb.review_count.toLocaleString()}</td>
                </tr>
              )}
              {gb.address && (
                <tr>
                  <td>Address</td>
                  <td>{gb.address}</td>
                </tr>
              )}
              {gb.phone && (
                <tr>
                  <td>Phone</td>
                  <td>{gb.phone}</td>
                </tr>
              )}
              {gb.website && (
                <tr>
                  <td>Website</td>
                  <td>{gb.website}</td>
                </tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      {report.top_posts && report.top_posts.length > 0 && (
        <section className="card">
          <h3>Top performing posts</h3>
          <table className="audit-table">
            <thead>
              <tr>
                <th>Platform</th>
                <th>Type</th>
                <th>Title</th>
                <th>Views</th>
                <th>Likes</th>
                <th>Comments</th>
              </tr>
            </thead>
            <tbody>
              {report.top_posts.map((post, index) => (
                <tr key={index}>
                  <td>{post.platform ?? "—"}</td>
                  <td>{post.type ?? "—"}</td>
                  <td>{post.title ?? "—"}</td>
                  <td>{post.views != null ? post.views.toLocaleString() : "—"}</td>
                  <td>{post.likes ?? 0}</td>
                  <td>{post.comments ?? 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {/* Bound to the same curated per_platform projection the PDF/DOCX scorecards consume
          (build_social_report_data), so the three surfaces can't silently drift apart. */}
      {(report.per_platform ?? []).length > 0 && (
        <section className="card">
          <h3>Per-platform scorecard</h3>
          <table className="audit-table">
            {/* Column set, units, and empty placeholder all match the PDF and DOCX scorecards —
                the three surfaces render this table from the one per_platform projection and
                must not disagree about what a cell means. */}
            <thead>
              <tr>
                <th>Platform</th>
                <th>Handle</th>
                <th>Followers</th>
                <th>Posts/mo</th>
                <th>Engagement</th>
                <th>Video %</th>
                <th>Last post</th>
                <th>Business</th>
              </tr>
            </thead>
            <tbody>
              {(report.per_platform ?? []).map((platform, index) => (
                <tr key={index}>
                  <td>{platform.platform || "—"}</td>
                  <td>{platform.handle ? `@${platform.handle}` : "—"}</td>
                  <td>{platform.followers != null ? String(platform.followers) : "—"}</td>
                  <td>{platform.posts_per_month ?? "—"}</td>
                  <td>
                    {platform.avg_engagement_rate_pct != null
                      ? `${platform.avg_engagement_rate_pct}%`
                      : "—"}
                  </td>
                  <td>{platform.video_share_pct != null ? `${platform.video_share_pct}%` : "—"}</td>
                  <td>
                    {platform.days_since_last_post != null
                      ? `${platform.days_since_last_post}d ago`
                      : "—"}
                  </td>
                  <td>{platform.is_business == null ? "—" : platform.is_business ? "✓" : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </>
  );
}

export default function AuditDetailPage() {
  const router = useRouter();
  const { getToken } = useAuth();
  const { id } = router.query;
  const [detail, setDetail] = useState<AuditDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [enrichmentError, setEnrichmentError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState<ReportFormat | null>(null);
  const [enriching, setEnriching] = useState(false);
  const [aiVisibilityError, setAiVisibilityError] = useState<string | null>(null);
  const [refreshingAiv, setRefreshingAiv] = useState(false);
  const [pollNonce, setPollNonce] = useState(0);
  const [share, setShare] = useState<AuditShareResponse | null>(null);
  const [shareError, setShareError] = useState<string | null>(null);
  const [sharing, setSharing] = useState(false);
  const [shareCopied, setShareCopied] = useState(false);

  async function handleDownload(format: ReportFormat) {
    if (!detail) return;
    setDownloadError(null);
    setDownloading(format);
    try {
      const token = await getToken();
      const { blob, filename } = await downloadReport(detail.job_id, format, token);
      const objectUrl = window.URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.URL.revokeObjectURL(objectUrl);
    } catch (error) {
      setDownloadError(
        error instanceof ApiError ? error.message : "Could not download the report.",
      );
    } finally {
      setDownloading(null);
    }
  }

  async function handleRerunEnrichment() {
    if (!detail) return;
    setEnrichmentError(null);
    setEnriching(true);
    try {
      const token = await getToken();
      const response = await rerunAuditEnrichment(detail.job_id, token);
      setDetail({
        ...detail,
        status: response.status,
        current_stage: response.current_stage,
        progress_pct: 70,
      });
      setPollNonce((value) => value + 1);
    } catch (error) {
      setEnrichmentError(
        error instanceof ApiError ? error.message : "Could not rerun external SEO enrichment.",
      );
    } finally {
      setEnriching(false);
    }
  }

  async function handleRefreshAiVisibility() {
    if (!detail) return;
    setAiVisibilityError(null);
    setRefreshingAiv(true);
    try {
      const token = await getToken();
      const response = await rerunAiVisibility(detail.job_id, token);
      setDetail({
        ...detail,
        status: response.status,
        current_stage: response.current_stage,
        progress_pct: 80,
      });
      setPollNonce((value) => value + 1);
    } catch (error) {
      setAiVisibilityError(
        error instanceof ApiError ? error.message : "Could not refresh AI visibility.",
      );
    } finally {
      setRefreshingAiv(false);
    }
  }

  async function handleShare() {
    if (!detail) return;
    setShareError(null);
    setShareCopied(false);
    setSharing(true);
    try {
      const token = await getToken();
      const response = await shareAudit(detail.job_id, token);
      setShare(response);
    } catch (error) {
      setShareError(error instanceof ApiError ? error.message : "Could not create a share link.");
    } finally {
      setSharing(false);
    }
  }

  async function handleRevokeShare() {
    if (!detail) return;
    setShareError(null);
    setSharing(true);
    try {
      const token = await getToken();
      await revokeShare(detail.job_id, token);
      setShare(null);
      setShareCopied(false);
    } catch (error) {
      setShareError(error instanceof ApiError ? error.message : "Could not revoke the share link.");
    } finally {
      setSharing(false);
    }
  }

  async function handleCopyShare() {
    if (!share) return;
    try {
      await navigator.clipboard.writeText(shareUrlFromPath(share.report_path));
      setShareCopied(true);
    } catch {
      setShareError("Copy failed — select and copy the link manually.");
    }
  }

  useEffect(() => {
    if (!id || typeof id !== "string") return;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function poll() {
      try {
        const token = await getToken();
        const data = await getAuditDetail(id as string, token);
        if (!active) return;
        setDetail(data);
        setLoadError(null);
        if (!isTerminal(data.status)) {
          timer = setTimeout(poll, POLL_INTERVAL_MS);
        }
      } catch (error) {
        if (!active) return;
        const apiError = error instanceof ApiError ? error : null;
        if (apiError && apiError.status === 404) {
          setLoadError("This audit could not be found. It may have been removed.");
          return;
        }
        setLoadError(
          apiError?.message || "Could not load the audit status. Retrying automatically…",
        );
        timer = setTimeout(poll, RETRY_INTERVAL_MS);
      }
    }

    poll();
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
    };
  }, [getToken, id, pollNonce]);

  const notFound = loadError && !detail;

  return (
    <Layout title="Audit Detail | BLC Website Audit">
      <div className="page-wide">
        <Link href="/audits" className="back-link">
          ← Back to audit history
        </Link>

        {!detail && !loadError && (
          <div className="card muted-card">
            <span className="spinner" aria-hidden="true" /> Loading audit…
          </div>
        )}

        {notFound && (
          <div className="card">
            <div className="alert alert-danger" role="alert">
              {loadError}
            </div>
            <Link href="/" className="btn btn-secondary">
              Submit a new audit
            </Link>
          </div>
        )}

        {detail && (
          <>
            <div className="detail-header">
              <div>
                <p className="eyebrow">Audit Result</p>
                <h1 className="detail-url">{detail.url}</h1>
                <p className="detail-meta">
                  {detail.niche && <span>Niche: {detail.niche}</span>}
                  {detail.target_audience && <span>Audience: {detail.target_audience}</span>}
                  <span>Submitted: {formatDate(detail.created_at)}</span>
                </p>
              </div>
              <span className={`badge badge-${statusTone(detail.status)}`}>
                {statusLabel(detail.status)}
              </span>
            </div>

            {loadError && detail && (
              <div className="alert alert-warning" role="status">
                {loadError}
              </div>
            )}

            {detail.status === "failed" && (
              <div className="card">
                <div className="alert alert-danger" role="alert">
                  <strong>Audit failed.</strong>
                  <p>{detail.error_message || "The audit pipeline reported an error."}</p>
                </div>
                <Link href="/" className="btn btn-secondary">
                  Try another audit
                </Link>
              </div>
            )}

            {detail.status !== "failed" && !isTerminal(detail.status) && (
              <ProgressView detail={detail} />
            )}

            {detail.status === "complete" && detail.audit_type !== "social" && detail.report && (
              <>
                <div className="result-actions card">
                  <div>
                    <h2>Audit complete</h2>
                    <p className="muted">
                      Generated {detail.report.metadata.generated_date} ·{" "}
                      {detail.report.metadata.pages_crawled} pages crawled
                      {detail.report.metadata.failed_pages > 0 &&
                        ` · ${detail.report.metadata.failed_pages} failed`}
                    </p>
                  </div>
                  {detail.report_available ? (
                    <div className="download-buttons">
                      <button
                        type="button"
                        className="btn btn-secondary"
                        onClick={handleRerunEnrichment}
                        disabled={enriching || detail.status !== "complete"}
                      >
                        {enriching ? "Starting enrichment..." : "Rerun enrichment"}
                      </button>
                      <button
                        type="button"
                        className="btn btn-secondary"
                        onClick={handleRefreshAiVisibility}
                        disabled={refreshingAiv || detail.status !== "complete"}
                      >
                        {refreshingAiv ? "Refreshing AI visibility..." : "Refresh AI Visibility"}
                      </button>
                      <button
                        type="button"
                        className="btn btn-primary"
                        onClick={() => handleDownload("pdf")}
                        disabled={downloading !== null}
                      >
                        {downloading === "pdf" ? "Downloading PDF..." : "Download PDF"}
                      </button>
                      <button
                        type="button"
                        className="btn btn-secondary"
                        onClick={() => handleDownload("docx")}
                        disabled={downloading !== null}
                      >
                        {downloading === "docx" ? "Downloading DOCX..." : "Download DOCX"}
                      </button>
                      <button
                        type="button"
                        className="btn btn-secondary"
                        onClick={handleShare}
                        disabled={sharing}
                      >
                        {sharing ? "Working..." : share ? "Refresh link" : "Share"}
                      </button>
                    </div>
                  ) : (
                    <span className="muted">Report exports unavailable.</span>
                  )}
                </div>

                {share && (
                  <div className="card share-panel">
                    <p className="muted">
                      Anyone with this link can view the report without signing in, until{" "}
                      {formatDate(share.share_expires_at)}.
                    </p>
                    <div className="share-row">
                      <input
                        type="text"
                        className="share-link-input"
                        readOnly
                        value={shareUrlFromPath(share.report_path)}
                        onFocus={(event) => event.target.select()}
                      />
                      <button type="button" className="btn btn-secondary" onClick={handleCopyShare}>
                        {shareCopied ? "Copied" : "Copy"}
                      </button>
                      <button
                        type="button"
                        className="btn btn-secondary"
                        onClick={handleRevokeShare}
                        disabled={sharing}
                      >
                        Revoke
                      </button>
                    </div>
                  </div>
                )}

                {shareError && (
                  <div className="alert alert-danger" role="alert">
                    {shareError}
                  </div>
                )}

                {downloadError && (
                  <div className="alert alert-danger" role="alert">
                    {downloadError}
                  </div>
                )}

                {enrichmentError && (
                  <div className="alert alert-danger" role="alert">
                    {enrichmentError}
                  </div>
                )}

                {aiVisibilityError && (
                  <div className="alert alert-danger" role="alert">
                    {aiVisibilityError}
                  </div>
                )}

                <ScoreCards scores={detail.report.scores} />

                {detail.report.executive_summary && (
                  <section className="card">
                    <h3>Executive summary</h3>
                    <p className="summary-text">{detail.report.executive_summary}</p>
                  </section>
                )}

                <WebsiteScopeBlock report={detail.report} />

                <ExternalSeoBlock report={detail.report} />

                {detail.report.sections.map((section) => (
                  <SectionBlock key={section.id} section={section} />
                ))}

                <AccessibilityBlock report={detail.report} />

                <RoadmapBlock roadmap={detail.report.roadmap} />

                <section className="card meta-grid">
                  <div>
                    <h4>PageSpeed</h4>
                    <p className="muted">
                      {detail.report.pagespeed_summary.status === "complete" ||
                      detail.report.pagespeed_summary.status === "partial"
                        ? `Mobile ${detail.report.pagespeed_summary.avg_mobile_performance ?? "—"} · Desktop ${
                            detail.report.pagespeed_summary.avg_desktop_performance ?? "—"
                          }`
                        : `Status: ${detail.report.pagespeed_summary.status}`}
                    </p>
                  </div>
                  <div>
                    <h4>Commentary validation</h4>
                    <p className="muted">
                      {detail.report.validation_summary.status} ·{" "}
                      {detail.report.validation_summary.numeric_claims_checked} claims checked
                    </p>
                  </div>
                  <div>
                    <h4>Rubric</h4>
                    <p className="muted">{detail.report.metadata.rubric_version}</p>
                  </div>
                  <div>
                    <h4>Commentary model</h4>
                    <p className="muted">{detail.report.metadata.llm_model}</p>
                  </div>
                </section>

                {/* Combined audit: the social media report + overall readiness score are appended
                    at the VERY END here, mirroring the PDF. The SEO/UX-UI sections above are
                    untouched; these render only when social data is present (combined audit). */}
                {detail.report.social_audit && (
                  <>
                    <section className="card section-block">
                      <div className="section-head">
                        <h3>Social Media Audit</h3>
                      </div>
                    </section>
                    <SocialReportView report={detail.report.social_audit} />
                  </>
                )}

                {detail.report.overall_readiness &&
                  detail.report.overall_readiness.score !== null && (
                    <OverallReadinessBlock overall={detail.report.overall_readiness} />
                  )}

                {detail.report.ai_visibility && (
                  <AiVisibilityBlock aiv={detail.report.ai_visibility} />
                )}
              </>
            )}

            {detail.status === "complete" &&
              detail.audit_type === "social" &&
              detail.social_report && (
                <>
                  <div className="result-actions card">
                    <div>
                      <h2>Social audit complete</h2>
                      <p className="muted">
                        Generated {detail.social_report.generated_date} ·{" "}
                        {detail.social_report.platforms_audited} profile(s)
                      </p>
                    </div>
                    {detail.report_available ? (
                      <div className="download-buttons">
                        <button
                          type="button"
                          className="btn btn-primary"
                          onClick={() => handleDownload("pdf")}
                          disabled={downloading !== null}
                        >
                          {downloading === "pdf" ? "Downloading PDF..." : "Download PDF"}
                        </button>
                        <button
                          type="button"
                          className="btn btn-secondary"
                          onClick={handleShare}
                          disabled={sharing}
                        >
                          {sharing ? "Working..." : share ? "Refresh link" : "Share"}
                        </button>
                      </div>
                    ) : (
                      <span className="muted">Report export unavailable.</span>
                    )}
                  </div>

                  {share && (
                    <div className="card share-panel">
                      <p className="muted">
                        Anyone with this link can view the report without signing in, until{" "}
                        {formatDate(share.share_expires_at)}.
                      </p>
                      <div className="share-row">
                        <input
                          type="text"
                          className="share-link-input"
                          readOnly
                          value={shareUrlFromPath(share.report_path)}
                          onFocus={(event) => event.target.select()}
                        />
                        <button
                          type="button"
                          className="btn btn-secondary"
                          onClick={handleCopyShare}
                        >
                          {shareCopied ? "Copied" : "Copy"}
                        </button>
                        <button
                          type="button"
                          className="btn btn-secondary"
                          onClick={handleRevokeShare}
                          disabled={sharing}
                        >
                          Revoke
                        </button>
                      </div>
                    </div>
                  )}

                  {(shareError || downloadError) && (
                    <div className="alert alert-danger" role="alert">
                      {shareError || downloadError}
                    </div>
                  )}

                  <SocialReportView report={detail.social_report} />
                </>
              )}
          </>
        )}
      </div>
    </Layout>
  );
}
