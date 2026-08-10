import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = { title: "NIFTY operations monitor", description: "Read-only operational decision support" };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
