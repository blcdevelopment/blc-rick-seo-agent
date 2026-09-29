import { ClerkProvider, SignedIn, SignedOut } from "@clerk/nextjs";
import type { AppProps } from "next/app";
import Head from "next/head";
import Welcome from "../components/Welcome";
import { PUBLIC_AUDITS } from "../lib/auth";
import "../styles/globals.css";

export default function App({ Component, pageProps }: AppProps) {
  const head = (
    <Head>
      <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
      <meta name="theme-color" content="#1d6da2" />
    </Head>
  );
  // Public edition: no sign-in and no Clerk at all (lib/auth.ts).
  if (PUBLIC_AUDITS) {
    return (
      <>
        {head}
        <Component {...pageProps} />
      </>
    );
  }
  return (
    <ClerkProvider {...pageProps}>
      {head}
      <SignedIn>
        <Component {...pageProps} />
      </SignedIn>
      <SignedOut>
        <Welcome />
      </SignedOut>
    </ClerkProvider>
  );
}
