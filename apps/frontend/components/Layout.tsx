import { UserButton } from "@clerk/nextjs";
import Head from "next/head";
import Link from "next/link";
import { useRouter } from "next/router";
import type { ReactNode } from "react";
import { PUBLIC_AUDITS } from "../lib/auth";

interface LayoutProps {
  title: string;
  children: ReactNode;
}

const NAV_LINKS = [
  // Social audits are now run from the Website Audit page (add social links there to get a
  // combined report) — there is no separate Social Audit tab.
  { href: "/", label: "Website Audit" },
  // The history lists every audit, so it is an operator page: hidden from public visitors.
  ...(PUBLIC_AUDITS ? [] : [{ href: "/audits", label: "Audit History" }]),
];

export default function Layout({ title, children }: LayoutProps) {
  const router = useRouter();

  return (
    <>
      <Head>
        <title>{title}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
      </Head>
      <div className="app">
        <header className="topbar">
          <div className="topbar-inner">
            <Link href="/" className="brand" aria-label="Builder Lead Converter home">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src="/blc-logo.svg" alt="Builder Lead Converter" className="brand-logo" />
            </Link>
            <nav className="topnav" aria-label="Primary">
              {NAV_LINKS.map((link) => {
                const active =
                  link.href === "/"
                    ? router.pathname === "/"
                    : router.pathname.startsWith(link.href);
                return (
                  <Link
                    key={link.href}
                    href={link.href}
                    className={active ? "topnav-link active" : "topnav-link"}
                  >
                    {link.label}
                  </Link>
                );
              })}
            </nav>
            {!PUBLIC_AUDITS && (
              <div className="topbar-user">
                <UserButton />
              </div>
            )}
          </div>
        </header>
        <main className="content">{children}</main>
        <footer className="appfooter">
          <span>
            {PUBLIC_AUDITS
              ? "Builder Lead Converter · Website Audit"
              : "BLC Website Audit · Phase 1 Internal Operator Console"}
          </span>
        </footer>
      </div>
    </>
  );
}
