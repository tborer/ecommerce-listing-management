import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Listing Manager",
  description: "Find eBay items with a matching CJdropshipping supplier, review them, and list.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
