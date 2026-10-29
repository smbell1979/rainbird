import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";
import { NavLinks } from "./NavLinks";

// Header, font and tokens follow the pool and weather apps so the home-screen
// apps look like one set.
const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: { default: "Sprinklers", template: "%s · Sprinklers" },
  description: "Cycle-and-soak runner for the Rain Bird ESP-TM2",
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f9f9f7" },
    { media: "(prefers-color-scheme: dark)", color: "#0d0d0d" },
  ],
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}>
      <body className="flex min-h-full flex-col">
        <header className="sticky top-0 z-30 border-b border-hairline bg-page/85 backdrop-blur-md">
          <div className="mx-auto flex h-14 w-full max-w-6xl items-center gap-4 px-4 sm:gap-8 sm:px-6">
            <Link href="/" aria-label="Sprinklers" className="flex shrink-0 items-center gap-2 text-[15px] font-semibold tracking-tight text-ink">
              {/* eslint-disable-next-line @next/next/no-img-element -- a 20 px SVG gains nothing from next/image */}
              <img src="/icon.svg" alt="" width={20} height={20} />
              {/* Mark only on a phone: with five nav links the word doesn't fit. */}
              <span className="hidden sm:inline">Sprinklers</span>
            </Link>
            <NavLinks />
          </div>
        </header>
        <main className="mx-auto w-full max-w-6xl flex-1 px-4 pt-6 pb-20 sm:px-6">{children}</main>
      </body>
    </html>
  );
}
