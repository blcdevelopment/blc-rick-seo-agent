// Typed client for the BLC Website Audit API.
// The base URL is configurable so the operator UI can point at a non-local API later.
export const API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000"
).replace(/\/$/, "");

export type ReportFormat = "pdf" | "docx";

interface ApiRequestInit extends RequestInit {
  authToken?: string | null;
}

export interface BrandOverrides {
  name?: string | null;
  short_name?: string | null;
  // Replaces the audit product's name on the report cover/title (default "Gooch").
  // Like every brand override, applies to the PDF only (the DOCX uses default branding).
  product_name?: string | null;
  primary_color?: string | null;
  accent_color?: string | null;
  logo_url?: string | null;
}

export interface AuditCreateRequest {
  url?: string | null;
  // "combined" runs the website audit then the social audit and returns ONE report with the
  // social section + Overall Lead-Gen Readiness appended (needs url AND >=1 handle).
  audit_type?: "website" | "social" | "combined";
  niche?: string | null;
  target_audience?: string | null;
  brand_overrides?: BrandOverrides | null;
  social_handles?: Record<string, string> | null;
}

export interface SocialReportFinding {
  id: string;
  label: string;
  metric?: string | null;
  remediation: string | null;
  impact: string;
  tier: string;
  result: string;
  narrative?: string;
}

export interface SocialStrength {
  id: string;
  label: string;
}

export interface SocialTopPost {
  platform?: string;
  type?: string;
  title?: string | null;
  views?: number | null;
  likes?: number;
  comments?: number;
  engagement?: number;
  posted?: string | null;
}

export interface SocialPlatformScore {
  platform?: string;
  handle?: string;
  followers?: number | null;
  posts_per_month?: number | null;
  days_since_last_post?: number | null;
  avg_engagement_rate_pct?: number | null;
  video_share_pct?: number | null;
  avg_views_per_post?: number | null;
  total_views?: number | null;
  verified?: boolean;
  is_business?: boolean;
  profile_complete?: boolean;
  link_in_bio?: boolean;
  has_cta?: boolean;
}

export interface SocialContentInsights {
  content_mix: { video: number | null; image: number | null; carousel: number | null };
  total_views: number | null;
  avg_views_per_post: number | null;
  avg_engagement_rate_pct: number | null;
  avg_like_to_comment_ratio: number | null;
  max_posting_gap_days: number | null;
  avg_hashtags_per_post: number | null;
  posts_with_cta_caption_pct: number | null;
  avg_follower_following_ratio: number | null;
}

// The public Google Business Profile listing the reviews/NAP checks were scored against
// (combined audits only; null/absent when the Places lookup was skipped or failed).
export interface SocialGoogleBusiness {
  name: string | null;
  address: string | null;
  phone: string | null;
  category: string | null;
  types?: string[];
  rating: number | null;
  review_count: number | null;
  // Precomposed by the shared report builder so PDF/DOCX prose never drifts.
  rating_line?: string | null;
  website: string | null;
  business_status?: string | null;
}

// Owner-consent connected YouTube Analytics (flag-gated; absent unless connected mode ran).
// The display lines are precomposed by the shared report builder so UI/PDF/DOCX can't drift.
export interface SocialConnectedYouTube {
  meta: string;
  lines: string[];
}

export interface SocialReport {
  version: string;
  score: number | null;
  status: string;
  handles: Record<string, string>;
  generated_date: string;
  platforms_audited: number;
  summary: Record<string, unknown>;
  platforms: Record<string, unknown>[];
  executive_summary?: string;
  commentary_provider?: string;
  findings: SocialReportFinding[];
  strengths?: SocialStrength[];
  content_insights?: SocialContentInsights | null;
  google_business?: SocialGoogleBusiness | null;
  connected_youtube?: SocialConnectedYouTube | null;
  top_posts?: SocialTopPost[];
  per_platform?: SocialPlatformScore[];
  roadmap: Record<string, SocialReportFinding[]>;
}

// Combined-audit Overall Lead-Gen Readiness (website composite blended with the Social Score).
export interface OverallReadiness {
  status: string;
  rubric_version: string;
  score: number | null;
  band: string;
  max_score: number;
  weights: { website: number; social: number };
  inputs: { website_lead_gen: number | null; social: number | null };
}

export interface AuditCreateResponse {
  job_id: string;
  status: string;
  status_url: string;
}

export interface AuditShareResponse {
  job_id: string;
  share_token: string;
  share_expires_at: string;
  report_path: string;
}

export interface AuditListItem {
  job_id: string;
  url: string;
  audit_type: string;
  status: string;
  current_stage: string | null;
  progress_pct: number;
  created_at: string;
  completed_at: string | null;
  seo_score: number | null;
  uxui_score: number | null;
  lead_gen_score: number | null;
  social_score: number | null;
  overall_score?: number | null;
  report_available: boolean;
}

export interface AuditListResponse {
  audits: AuditListItem[];
}

export interface ScoreCard {
  id: "lead_gen" | "seo" | "uxui";
  label: string;
  score: number;
  max_score: number;
  description: string;
}

export interface ReportFinding {
  section: string;
  severity: "info" | "low" | "medium" | "high";
  title: string;
  explanation: string;
  evidence_refs: string[];
  source: "commentary" | "rubric";
  // The fix carried on the finding card ("Do this"); absent on older stored results.
  action_items?: string[];
  tier?: string;
}

export interface ReportRecommendation {
  section: string;
  tier: "quick_win" | "mid_term" | "long_term";
  title: string;
  rationale: string;
  action_items: string[];
}

export interface ReportSection {
  id: string;
  label: string;
  headline: string;
  score: number | null;
  findings: ReportFinding[];
  recommendations: ReportRecommendation[];
  // Legacy-payload fallback flag, computed once server-side (findings without action_items
  // keep their fixes in `recommendations`) so PDF/DOCX/UI agree on when to render the list.
  show_recommendations?: boolean;
  opportunities: RuleSummary[];
}

export interface RoadmapTier {
  tier: string;
  label: string;
  recommendations: ReportRecommendation[];
}

export interface ValidationSummary {
  status: string;
  numeric_claims_checked: number;
  unsupported_claim_count: number;
  action: string;
}

export interface PageSpeedSummary {
  status: string;
  reason: string | null;
  scope: string | null;
  pages_requested: number;
  pages_analyzed: number;
  avg_mobile_performance: number | null;
  avg_desktop_performance: number | null;
}

export interface ExternalSeoSummary {
  status: string;
  technical_crawl_status: string;
  technical_crawl_tool: string | null;
  gsc_status: string;
  url_inspection_status: string;
  technical_issue_count: number;
  search_opportunity_count: number;
}

export interface TechnicalSeoIssue {
  id: string;
  severity: "info" | "low" | "medium" | "high";
  title: string;
  count: number;
  summary: string;
  why_it_matters: string;
  recommended_fix: string;
  location_label: string;
  examples: string[];
}

export interface TechnicalSeoSection {
  status: string;
  status_label: string;
  reason_label: string | null;
  source: string | null;
  tool_label: string | null;
  summary: Record<string, unknown>;
  issues: TechnicalSeoIssue[];
  notes: string[];
  warnings: string[];
}

export interface SearchPerformanceSection {
  status: string;
  status_label: string;
  reason_label: string | null;
  site_url: string | null;
  date_range: Record<string, unknown>;
  summary: Record<string, unknown>;
  top_queries: Record<string, unknown>[];
  top_pages: Record<string, unknown>[];
  high_impression_low_ctr_pages: Record<string, unknown>[];
  ranking_opportunities: Record<string, unknown>[];
  declining_pages: Record<string, unknown>[];
  url_inspection_summary: Record<string, unknown>;
  url_inspection_items: Record<string, unknown>[];
  // Business-opportunity framing (P1-P4); empty when GSC is not connected.
  opportunity?: Record<string, unknown>;
  branded?: Record<string, unknown>;
  topic_clusters?: Record<string, unknown>[];
}

export interface SearchConsoleProperty {
  siteUrl: string;
  permissionLevel?: string | null;
}

export interface SearchConsolePropertiesResponse {
  status: string;
  account_email: string | null;
  properties: SearchConsoleProperty[];
  reason: string | null;
}

export interface SearchConsoleConnectUrlResponse {
  status: string;
  connect_url: string | null;
  reason: string | null;
}

export interface RuleSummary {
  rule_id: string;
  description: string;
  result: "pass" | "partial" | "fail" | "skipped";
  points_awarded: number;
  points_possible: number;
  evidence_value: string | null;
  reason: string | null;
}

export interface ReportMetadata {
  site_domain: string;
  niche: string | null;
  target_audience: string | null;
  generated_date: string;
  pages_crawled: number;
  failed_pages: number;
  rubric_version: string;
  llm_model: string;
}

export interface AccessibilityIssue {
  rule_id: string;
  impact: string;
  wcag_criteria: string[];
  help: string;
  help_url: string;
  instances: number;
  example_selectors: string[];
  example_pages: string[];
  failure_summary: string;
}

// Advisory-only axe-core accessibility section (never affects the score). Optional in the
// payload, mirroring the Pydantic default-factory.
export interface AccessibilityAdvisorySection {
  status: string;
  status_label: string;
  disclaimer: string;
  axe_version: string;
  pages_scanned: number;
  impact_counts: Record<string, number>;
  needs_review_count: number;
  issues: AccessibilityIssue[];
  notes: string[];
}

// "What the whole website consists of" scope panel (null when nothing was discovered).
export interface WebsiteScope {
  pages_discovered: number | null;
  pages_analyzed: number | null;
  blog_posts: number | null;
  sitemap_entries: number | null;
  outbound_links: number | null;
  images: number | null;
}

export interface ReportPayload {
  version: string;
  metadata: ReportMetadata;
  scores: ScoreCard[];
  executive_summary: string;
  sections: ReportSection[];
  roadmap: RoadmapTier[];
  validation_summary: ValidationSummary;
  pagespeed_summary: PageSpeedSummary;
  external_seo_summary: ExternalSeoSummary;
  technical_seo_section: TechnicalSeoSection;
  search_performance_section: SearchPerformanceSection;
  accessibility_advisory_section?: AccessibilityAdvisorySection;
  website_scope?: WebsiteScope | null;
  // Combined-audit only: the social section + overall readiness appended to the website report.
  social_audit?: SocialReport | null;
  overall_readiness?: OverallReadiness | null;
  // The one shared combined-cover predicate (overall complete AND scored), computed
  // server-side so the PDF cover, DOCX title, and score-card intro can never disagree.
  combined_complete?: boolean;
  // On-demand AI Visibility (Semrush) enrichment; null unless the refresh action ran.
  ai_visibility?: AiVisibility | null;
}

export interface AiVisibilityMetric {
  key: string;
  label: string;
  value: string | number;
}

export interface AiVisibilityPlatform {
  platform: string;
  mentions?: number | null;
  share_pct?: number | null;
  share_display?: string | null;
}

export interface AiVisibilityTopic {
  topic: string;
  visibility?: number | null;
  your_mentions?: number | null;
  ai_volume?: string | null;
}

export interface AiVisibilityCompetitor {
  label: string;
  visibility_score?: number | null;
  mentions?: number | null;
}

export interface AiVisibilityCountry {
  country: string;
  mentions?: number | null;
  share_pct?: number | null;
  share_display?: string | null;
}

export interface AiVisibility {
  status: string;
  // Set when the bot reached Semrush but was blocked (CAPTCHA / login wall); `message` is the note.
  unavailable?: boolean;
  message?: string | null;
  provider?: string | null;
  domain?: string | null;
  retrieved_at?: string | null;
  visibility_score?: number | null;
  visibility_band?: string | null;
  metrics: AiVisibilityMetric[];
  per_platform: AiVisibilityPlatform[];
  topics: AiVisibilityTopic[];
  competitors: AiVisibilityCompetitor[];
  by_country: AiVisibilityCountry[];
}

export interface AuditDetail {
  job_id: string;
  url: string;
  audit_type: string;
  niche: string | null;
  target_audience: string | null;
  social_handles?: Record<string, string> | null;
  status: string;
  current_stage: string | null;
  progress_pct: number;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  report_available: boolean;
  seo_score?: number | null;
  uxui_score?: number | null;
  lead_gen_score?: number | null;
  social_score?: number | null;
  overall_score?: number | null;
  report: ReportPayload | null;
  social_report?: SocialReport | null;
}

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function readError(response: Response): Promise<string> {
  try {
    const body = await response.json();
    const detail = body?.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object" && typeof detail.message === "string") {
      return detail.message;
    }
    if (Array.isArray(detail) && detail.length > 0) {
      // FastAPI request-validation errors arrive as a list of issues.
      const first = detail[0];
      if (first?.msg) return String(first.msg);
    }
  } catch {
    // fall through to the generic message below
  }
  return `Request failed with status ${response.status}.`;
}

function requestHeaders(authToken?: string | null, includeJson = true): HeadersInit {
  const headers: Record<string, string> = {};
  if (includeJson) headers["Content-Type"] = "application/json";
  if (authToken) headers.Authorization = `Bearer ${authToken}`;
  return headers;
}

async function request<T>(path: string, init?: ApiRequestInit): Promise<T> {
  const { authToken, headers, ...requestInit } = init || {};
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      credentials: "include",
      headers: {
        ...requestHeaders(authToken),
        ...(headers as Record<string, string> | undefined),
      },
      ...requestInit,
    });
  } catch {
    throw new ApiError(
      `Could not reach the audit API. Make sure the backend is running on ${API_BASE_URL}.`,
      0,
    );
  }
  if (!response.ok) {
    throw new ApiError(await readError(response), response.status);
  }
  return (await response.json()) as T;
}

export function createAudit(
  payload: AuditCreateRequest,
  authToken?: string | null,
): Promise<AuditCreateResponse> {
  return request<AuditCreateResponse>("/audits", {
    method: "POST",
    authToken,
    body: JSON.stringify(payload),
  });
}

export function listAudits(
  limit = 25,
  authToken?: string | null,
): Promise<AuditListResponse> {
  return request<AuditListResponse>(`/audits?limit=${limit}`, { authToken });
}

export function getAuditDetail(
  jobId: string,
  authToken?: string | null,
): Promise<AuditDetail> {
  return request<AuditDetail>(`/audits/${jobId}`, { authToken });
}

export function reportUrl(jobId: string, format: ReportFormat = "pdf"): string {
  return `${API_BASE_URL}/audits/${jobId}/${format === "pdf" ? "report" : "docx"}`;
}

export function getSearchConsoleProperties(
  authToken?: string | null,
): Promise<SearchConsolePropertiesResponse> {
  return request<SearchConsolePropertiesResponse>("/google/search-console/properties", {
    authToken,
  });
}

export function createSearchConsoleConnectUrl(
  authToken?: string | null,
): Promise<SearchConsoleConnectUrlResponse> {
  return request<SearchConsoleConnectUrlResponse>("/google/search-console/connect-url", {
    authToken,
  });
}

export function rerunAuditEnrichment(
  jobId: string,
  authToken?: string | null,
): Promise<{ job_id: string; status: string; current_stage: string | null; message: string }> {
  return request(`/audits/${jobId}/rerun-enrichment`, {
    method: "POST",
    authToken,
  });
}

export function rerunAiVisibility(
  jobId: string,
  authToken?: string | null,
): Promise<{ job_id: string; status: string; current_stage: string | null; message: string }> {
  return request(`/audits/${jobId}/rerun-ai-visibility`, {
    method: "POST",
    authToken,
  });
}

export function shareAudit(
  jobId: string,
  authToken?: string | null,
): Promise<AuditShareResponse> {
  return request<AuditShareResponse>(`/audits/${jobId}/share`, {
    method: "POST",
    authToken,
  });
}

export function revokeShare(
  jobId: string,
  authToken?: string | null,
): Promise<{ job_id: string; shared: boolean }> {
  return request(`/audits/${jobId}/share`, {
    method: "DELETE",
    authToken,
  });
}

// Builds the absolute, login-free report URL from the API's relative report_path.
export function shareUrlFromPath(reportPath: string): string {
  return `${API_BASE_URL}${reportPath}`;
}

function filenameFromDisposition(disposition: string | null, fallback: string): string {
  const match = disposition?.match(/filename="?([^";]+)"?/i);
  return match?.[1] || fallback;
}

export async function downloadReport(
  jobId: string,
  format: ReportFormat,
  authToken?: string | null,
): Promise<{ blob: Blob; filename: string }> {
  let response: Response;
  try {
    response = await fetch(reportUrl(jobId, format), {
      credentials: "include",
      headers: requestHeaders(authToken, false),
    });
  } catch {
    throw new ApiError(
      `Could not reach the audit API. Make sure the backend is running on ${API_BASE_URL}.`,
      0,
    );
  }
  if (!response.ok) {
    throw new ApiError(await readError(response), response.status);
  }
  return {
    blob: await response.blob(),
    filename: filenameFromDisposition(
      response.headers.get("content-disposition"),
      `blc-website-audit-${jobId}.${format}`,
    ),
  };
}
