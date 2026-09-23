import type { Metadata } from "next";

// Signed-in app pages (and login): keep them out of search results.
export const metadata: Metadata = {
  robots: { index: false, follow: false },
};

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return children;
}
