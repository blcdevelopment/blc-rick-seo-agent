import { clerkMiddleware } from "@clerk/nextjs/server";

// Provides Clerk auth context to every route. Access is gated in _app.tsx with
// <SignedIn>/<SignedOut> (signed-out visitors see the welcome screen with a modal
// sign-in), so there is no server-side redirect to Clerk's hosted sign-in page.
export default clerkMiddleware();

export const config = {
  matcher: [
    // Run on everything except Next internals and static files...
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    // ...and always on API/trpc routes.
    "/(api|trpc)(.*)",
  ],
};
