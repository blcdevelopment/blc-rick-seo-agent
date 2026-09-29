import { SignInButton } from "@clerk/nextjs";
import Head from "next/head";

// Landing screen for signed-out visitors: explains what the tool does and offers
// a modal sign-in. After a successful sign-in, _app.tsx renders the app instead.
export default function Welcome() {
  return (
    <>
      <Head>
        <title>BLC Website Audit</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
      </Head>
      <div className="welcome">
        <div className="card welcome-card">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/blc-logo.svg" alt="Builder Lead Converter" className="welcome-logo" />
          <p className="eyebrow">Internal Operator Console</p>
          <h1>BLC Website Audit</h1>
          <p className="lede">
            Run a full automated audit of any website: we crawl the site, collect Google
            PageSpeed data, score SEO and UX/UI against BLC&apos;s rubrics, and generate a
            branded PDF report with AI commentary — ready to share with a client.
          </p>
          <ul className="welcome-features">
            <li>Submit a URL and watch the audit progress live</li>
            <li>Deterministic SEO, UX/UI, and lead-generation scores</li>
            <li>Grounded AI commentary and a prioritized roadmap</li>
            <li>Branded PDF report + full audit history</li>
          </ul>
          <SignInButton mode="modal">
            <button type="button" className="btn btn-primary welcome-signin">
              Sign in to get started
            </button>
          </SignInButton>
          <p className="welcome-note">Access is limited to the BLC team.</p>
        </div>
      </div>
    </>
  );
}
