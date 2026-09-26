import { useAuth } from "@clerk/nextjs";
import { useRouter } from "next/router";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  ApiError,
  SearchConsolePropertiesResponse,
  createSearchConsoleConnectUrl,
  getSearchConsoleProperties,
} from "../lib/api";

function readableReason(reason: string | null | undefined): string {
  switch (reason) {
    case "oauth_not_configured":
      return "Google OAuth is not configured for this environment.";
    case "no_google_connection":
      return "No shared BLC Google account is connected yet.";
    default:
      return reason ? reason.replaceAll("_", " ") : "Not connected";
  }
}

function queryValue(value: string | string[] | undefined): string | null {
  if (Array.isArray(value)) return value[0] || null;
  return value || null;
}

export default function SearchConsoleIntegration() {
  const { getToken } = useAuth();
  const router = useRouter();
  const [status, setStatus] = useState<SearchConsolePropertiesResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const callbackStatus = queryValue(router.query.gsc);
  const callbackDetail = queryValue(router.query.detail);
  const connected = status?.status === "complete";
  const oauthConfigured = status?.reason !== "oauth_not_configured";

  const statusText = useMemo(() => {
    if (loading && !status) return "Checking...";
    if (connected) return "Connected";
    return readableReason(status?.reason);
  }, [connected, loading, status]);

  const loadStatus = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const token = await getToken();
      setStatus(await getSearchConsoleProperties(token));
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not check the Search Console connection.",
      );
    } finally {
      setLoading(false);
    }
  }, [getToken]);

  async function handleConnect() {
    setConnecting(true);
    setError(null);
    try {
      const token = await getToken();
      const response = await createSearchConsoleConnectUrl(token);
      if (response.status !== "ready" || !response.connect_url) {
        throw new ApiError(readableReason(response.reason), 503);
      }
      window.location.assign(response.connect_url);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not start the Google connection flow.",
      );
      setConnecting(false);
    }
  }

  useEffect(() => {
    loadStatus();
  }, [loadStatus]);

  return (
    <section className="card integration-card">
      <div className="section-head">
        <div>
          <p className="eyebrow">Shared Data Source</p>
          <h3>BLC Search Console connection</h3>
          <p className="section-headline">
            Reports use the connected BLC Google account when it has access to the audited site.
            Website submitters do not need their own Search Console account.
          </p>
        </div>
        <span className={`badge ${connected ? "badge-success" : "badge-neutral"}`}>
          {statusText}
        </span>
      </div>

      {callbackStatus && (
        <div
          className={callbackStatus === "connected" ? "alert alert-success" : "alert alert-warning"}
          role="status"
        >
          {callbackStatus === "connected"
            ? `Search Console connected: ${readableReason(callbackDetail)}.`
            : `Search Console connection returned: ${readableReason(callbackDetail)}.`}
        </div>
      )}

      {error && (
        <div className="alert alert-danger" role="alert">
          {error}
        </div>
      )}

      <div className="integration-stats">
        <div>
          <span>Google account</span>
          <strong>{status?.account_email || "Shared BLC account not connected"}</strong>
        </div>
        <div>
          <span>Properties available</span>
          <strong>{status?.properties.length ?? 0}</strong>
        </div>
        <div>
          <span>Enrichment behavior</span>
          <strong>{connected ? "Automatic when property matches" : "Audit still runs"}</strong>
        </div>
      </div>

      <div className="integration-actions">
        <button
          type="button"
          className="btn btn-primary"
          onClick={handleConnect}
          disabled={connecting || loading || !oauthConfigured}
        >
          {connecting ? "Opening Google..." : connected ? "Reconnect BLC Google" : "Connect BLC Google"}
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          onClick={loadStatus}
          disabled={loading || connecting}
        >
          {loading ? "Checking..." : "Refresh status"}
        </button>
      </div>
    </section>
  );
}
