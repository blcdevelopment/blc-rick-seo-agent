import { ClerkProvider, SignedIn, SignedOut } from "@clerk/nextjs";
import type { AppProps } from "next/app";
import Head from "next/head";
import Welcome from "../components/Welcome";
import "../styles/globals.css";

export default function App({ Component, pageProps }: AppProps) {
  return (
    <ClerkProvider {...pageProps}>
      <Head>
        <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
        <meta name="theme-color" content="#1d6da2" />
      </Head>
      <SignedIn>
        <Component {...pageProps} />
      </SignedIn>
      <SignedOut>
        <Welcome />
      </SignedOut>
    </ClerkProvider>
  );
}
