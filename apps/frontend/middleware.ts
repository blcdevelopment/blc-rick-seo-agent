import { clerkMiddleware } from "@clerk/nextjs/server";
import { NextResponse } from "next/server";

// Provides Clerk auth context to every route. Access is gated in _app.tsx with
// <SignedIn>/<SignedOut> (signed-out visitors see the welcome screen with a modal
// sign-in), so there is no server-side redirect to Clerk's hosted sign-in page.
// The public edition (NEXT_PUBLIC_PUBLIC_AUDITS_ENABLED) has no sign-in, so it passes
// every request straight through and needs no Clerk keys.
const publicAudits = process.env.NEXT_PUBLIC_PUBLIC_AUDITS_ENABLED === "true";

export default publicAudits ? () => NextResponse.next() : clerkMiddleware();

export const config = {
  matcher: [
    // Run on everything except Next internals and static files...
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    // ...and always on API/trpc routes.
    "/(api|trpc)(.*)",
  ],
};
