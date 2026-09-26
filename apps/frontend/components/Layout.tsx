import { UserButton } from "@clerk/nextjs";
import Head from "next/head";
import Link from "next/link";
import { useRouter } from "next/router";
import type { ReactNode } from "react";

interface LayoutProps {
  title: string;
  children: ReactNode;
}

const NAV_LINKS = [
  // Social audits are now run from the Website Audit page (add social links there to get a
  // combined report) — there is no separate Social Audit tab.
  { href: "/", label: "Website Audit" },
  { href: "/audits", label: "Audit History" },
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
            <div className="topbar-user">
              <UserButton />
            </div>
          </div>
        </header>
        <main className="content">{children}</main>
        <footer className="appfooter">
          <span>BLC Website Audit · Phase 1 Internal Operator Console</span>
        </footer>
      </div>
    </>
  );
}
