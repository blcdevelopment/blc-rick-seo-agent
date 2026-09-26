import { useAuth } from "@clerk/nextjs";

/**
 * Public edition (the Rick edition): visitors run an audit and read its report without signing
 * in, so the app renders without Clerk and API calls carry no token. A build-time flag
 * (NEXT_PUBLIC_* values are inlined), the counterpart of the API's PUBLIC_AUDITS_ENABLED.
 */
export const PUBLIC_AUDITS = process.env.NEXT_PUBLIC_PUBLIC_AUDITS_ENABLED === "true";

type TokenGetter = () => Promise<string | null>;

const noToken: TokenGetter = async () => null;

function usePublicToken(): { getToken: TokenGetter } {
  return { getToken: noToken };
}

/**
 * Where API calls get their bearer token: Clerk's session, or none in the public edition.
 * PUBLIC_AUDITS is fixed per build, so every render calls the same hook.
 */
export const useApiToken: () => { getToken: TokenGetter } = PUBLIC_AUDITS
  ? usePublicToken
  : useAuth;
