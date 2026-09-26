import { useAuth } from "@clerk/nextjs";
import { useRouter } from "next/router";
import { FormEvent, useState } from "react";

import Layout from "../components/Layout";
import SearchConsoleIntegration from "../components/SearchConsoleIntegration";
import { ApiError, BrandOverrides, createAudit } from "../lib/api";

// Mirrors the backend HEX_COLOR_RE in report_branding.py so the operator gets inline feedback;
// the backend still silently ignores a malformed colour, so this only improves UX.
const HEX_COLOR = /^#[0-9a-fA-F]{6}$/;

function normalizeUrl(raw: string): string {
  const trimmed = raw.trim();
  if (!trimmed) return trimmed;
  if (/^https?:\/\//i.test(trimmed)) return trimmed;
  return `https://${trimmed}`;
}

function isValidUrl(value: string): boolean {
  if (/\s/.test(value)) return false;
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
  // Require a dotted public host (example.com) or localhost so obvious typos are caught inline.
  return parsed.hostname === "localhost" || parsed.hostname.includes(".");
}

// Accepts a pasted profile LINK or a bare @handle and returns the clean handle/ID, so adding
// social links turns the website audit into a COMBINED audit (one report with a social section +
// overall readiness appended). Mirrors social.tsx.
function extractHandle(raw: string): string {
  let value = raw.trim();
  if (!value) return "";
  if (/youtube\.com/i.test(value)) {
    const yt = value.match(/youtube\.com\/(?:channel\/|c\/|user\/|@)?([^/?#]+)/i);
    if (yt) value = yt[1];
  } else {
    const match = value.match(/(?:instagram\.com|facebook\.com)\/([^/?#]+)/i);
    if (match) value = match[1];
  }
  return value.replace(/^@/, "").replace(/\/+$/, "").trim();
}

export default function SubmitAuditPage() {
  const router = useRouter();
  const { getToken } = useAuth();
  const [url, setUrl] = useState("");
  const [niche, setNiche] = useState("");
  const [targetAudience, setTargetAudience] = useState("");
  // Optional social links — any value here makes this a COMBINED audit.
  const [instagram, setInstagram] = useState("");
  const [facebook, setFacebook] = useState("");
  const [youtube, setYoutube] = useState("");
  const [urlError, setUrlError] = useState<string | null>(null);
  const [apiError, setApiError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // White-label branding (P2-11) — all optional; blanks fall back to the default BLC brand.
  const [brandName, setBrandName] = useState("");
  const [brandShortName, setBrandShortName] = useState("");
  const [brandProductName, setBrandProductName] = useState("");
  const [brandPrimaryColor, setBrandPrimaryColor] = useState("");
  const [brandAccentColor, setBrandAccentColor] = useState("");
  const [brandLogoUrl, setBrandLogoUrl] = useState("");
  const [brandError, setBrandError] = useState<string | null>(null);

  function buildBrandOverrides(): BrandOverrides | undefined {
    const overrides: BrandOverrides = {};
    if (brandName.trim()) overrides.name = brandName.trim();
    if (brandShortName.trim()) overrides.short_name = brandShortName.trim();
    if (brandProductName.trim()) overrides.product_name = brandProductName.trim();
    if (brandPrimaryColor.trim()) overrides.primary_color = brandPrimaryColor.trim();
    if (brandAccentColor.trim()) overrides.accent_color = brandAccentColor.trim();
    if (brandLogoUrl.trim()) overrides.logo_url = brandLogoUrl.trim();
    return Object.keys(overrides).length > 0 ? overrides : undefined;
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setApiError(null);

    const candidate = normalizeUrl(url);
    if (!candidate) {
      setUrlError("Enter the website URL you want to audit.");
      return;
    }
    if (!isValidUrl(candidate)) {
      setUrlError("Enter a valid website URL, e.g. https://example.com");
      return;
    }
    setUrlError(null);

    const badColor = ([
      ["Primary colour", brandPrimaryColor],
      ["Accent colour", brandAccentColor],
    ] as const).find(([, value]) => value.trim() && !HEX_COLOR.test(value.trim()));
    if (badColor) {
      setBrandError(`${badColor[0]} must be a 6-digit hex value, e.g. #1a3a5c.`);
      return;
    }
    setBrandError(null);
    setSubmitting(true);

    const handles: Record<string, string> = {};
    const ig = extractHandle(instagram);
    const fb = extractHandle(facebook);
    const yt = extractHandle(youtube);
    if (ig) handles.instagram = ig;
    if (fb) handles.facebook = fb;
    if (yt) handles.youtube = yt;
    const hasSocial = Object.keys(handles).length > 0;

    try {
      const token = await getToken();
      const created = await createAudit(
        {
          url: candidate,
          // Adding any social link runs the social audit after the website audit and returns
          // one combined report; otherwise it stays a plain website audit.
          audit_type: hasSocial ? "combined" : "website",
          social_handles: hasSocial ? handles : undefined,
          niche: niche.trim() || null,
          target_audience: targetAudience.trim() || null,
          brand_overrides: buildBrandOverrides(),
        },
        token,
      );
      router.push(`/audit/${created.job_id}`);
    } catch (error) {
      const message =
        error instanceof ApiError
          ? error.message
          : "Something went wrong submitting the audit. Please try again.";
      setApiError(message);
      setSubmitting(false);
    }
  }

  return (
    <Layout title="New Audit | BLC Website Audit">
      <div className="page-narrow">
        <p className="eyebrow">New Website Audit</p>
        <h1>Submit a website for auditing</h1>
        <p className="lede">
          Enter a website URL to run the full SEO, UX/UI, and lead generation readiness audit. Add
          social links below to also audit social media and get one combined report with an overall
          readiness score. Niche and target audience are optional.
        </p>

        <SearchConsoleIntegration />

        <form className="card form" onSubmit={handleSubmit} noValidate>
          {apiError && (
            <div className="alert alert-danger" role="alert">
              {apiError}
            </div>
          )}

          <div className="field">
            <label htmlFor="url">
              Website URL <span className="required">*</span>
            </label>
            <input
              id="url"
              name="url"
              type="text"
              inputMode="url"
              placeholder="https://example.com"
              value={url}
              onChange={(event) => {
                setUrl(event.target.value);
                if (urlError) setUrlError(null);
              }}
              aria-invalid={urlError ? "true" : "false"}
              aria-describedby={urlError ? "url-error" : undefined}
              disabled={submitting}
              autoFocus
            />
            {urlError && (
              <p className="field-error" id="url-error">
                {urlError}
              </p>
            )}
          </div>

          <div className="field">
            <label htmlFor="niche">Niche (optional)</label>
            <input
              id="niche"
              name="niche"
              type="text"
              placeholder="e.g. custom home builder, kitchen remodeler"
              value={niche}
              maxLength={255}
              onChange={(event) => setNiche(event.target.value)}
              disabled={submitting}
            />
            <p className="field-hint">
              The audited business&apos;s trade. Printed on the report cover exactly as typed.
            </p>
          </div>

          <div className="field">
            <label htmlFor="target_audience">Target audience (optional)</label>
            <input
              id="target_audience"
              name="target_audience"
              type="text"
              placeholder="e.g. homeowners planning a custom build or remodel"
              value={targetAudience}
              maxLength={255}
              onChange={(event) => setTargetAudience(event.target.value)}
              disabled={submitting}
            />
            <p className="field-hint">
              Who the audited business sells to (its customers — not BLC&apos;s). Printed on the
              report cover exactly as typed.
            </p>
          </div>

          <details className="brand-panel">
            <summary>Social media (optional — auto-detected from the site if left blank)</summary>
            <p className="muted">
              Paste profile links or @handles to audit specific accounts. Leave any blank and we
              auto-detect that platform&apos;s link from the website itself (the footer/header
              icons). When any profile is provided or found, the social audit runs after the website
              audit and produces one combined report with a social section and an overall lead-gen
              readiness score.
            </p>

            <div className="field">
              <label htmlFor="instagram">Instagram</label>
              <input
                id="instagram"
                name="instagram"
                type="text"
                placeholder="@acmebuilders or instagram.com/acmebuilders"
                value={instagram}
                onChange={(event) => setInstagram(event.target.value)}
                disabled={submitting}
              />
            </div>

            <div className="field">
              <label htmlFor="facebook">Facebook</label>
              <input
                id="facebook"
                name="facebook"
                type="text"
                placeholder="facebook.com/acmebuilders"
                value={facebook}
                onChange={(event) => setFacebook(event.target.value)}
                disabled={submitting}
              />
            </div>

            <div className="field">
              <label htmlFor="youtube">YouTube</label>
              <input
                id="youtube"
                name="youtube"
                type="text"
                placeholder="youtube.com/@acmebuilders"
                value={youtube}
                onChange={(event) => setYoutube(event.target.value)}
                disabled={submitting}
              />
            </div>
          </details>

          <details className="brand-panel">
            <summary>White-label branding (optional, applies to the PDF report)</summary>
            <p className="muted">
              Override the report logo, name, and colours for a prospect-facing PDF. Leave any
              field blank to keep the default BLC brand.
            </p>
            {brandError && (
              <p className="field-error" role="alert">
                {brandError}
              </p>
            )}

            <div className="field">
              <label htmlFor="brand_name">Brand name</label>
              <input
                id="brand_name"
                name="brand_name"
                type="text"
                placeholder="e.g. Acme Marketing"
                value={brandName}
                maxLength={120}
                onChange={(event) => setBrandName(event.target.value)}
                disabled={submitting}
              />
            </div>

            <div className="field">
              <label htmlFor="brand_short_name">Short name</label>
              <input
                id="brand_short_name"
                name="brand_short_name"
                type="text"
                placeholder="e.g. Acme"
                value={brandShortName}
                maxLength={40}
                onChange={(event) => setBrandShortName(event.target.value)}
                disabled={submitting}
              />
            </div>

            <div className="field">
              <label htmlFor="brand_product_name">Report product name</label>
              <input
                id="brand_product_name"
                name="brand_product_name"
                type="text"
                placeholder='Cover/title branding (default "Gooch")'
                value={brandProductName}
                maxLength={60}
                onChange={(event) => setBrandProductName(event.target.value)}
                disabled={submitting}
              />
            </div>

            <div className="field">
              <label htmlFor="brand_primary_color">Primary colour (hex)</label>
              <input
                id="brand_primary_color"
                name="brand_primary_color"
                type="text"
                placeholder="#1a3a5c"
                value={brandPrimaryColor}
                maxLength={7}
                onChange={(event) => {
                  setBrandPrimaryColor(event.target.value);
                  if (brandError) setBrandError(null);
                }}
                disabled={submitting}
              />
            </div>

            <div className="field">
              <label htmlFor="brand_accent_color">Accent colour (hex)</label>
              <input
                id="brand_accent_color"
                name="brand_accent_color"
                type="text"
                placeholder="#f5a623"
                value={brandAccentColor}
                maxLength={7}
                onChange={(event) => {
                  setBrandAccentColor(event.target.value);
                  if (brandError) setBrandError(null);
                }}
                disabled={submitting}
              />
            </div>

            <div className="field">
              <label htmlFor="brand_logo_url">Logo URL</label>
              <input
                id="brand_logo_url"
                name="brand_logo_url"
                type="url"
                inputMode="url"
                placeholder="https://cdn.example.com/logo.png"
                value={brandLogoUrl}
                maxLength={1000}
                onChange={(event) => setBrandLogoUrl(event.target.value)}
                disabled={submitting}
              />
            </div>
          </details>

          <div className="form-actions">
            <button type="submit" className="btn btn-primary" disabled={submitting}>
              {submitting ? (
                <>
                  <span className="spinner" aria-hidden="true" /> Submitting…
                </>
              ) : (
                "Start audit"
              )}
            </button>
          </div>
        </form>
      </div>
    </Layout>
  );
}
