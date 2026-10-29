"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV = [
  { href: "/", label: "Status" },
  { href: "/plans", label: "Plans" },
  { href: "/calendar", label: "Calendar" },
  { href: "/history", label: "History" },
  { href: "/settings", label: "Settings" },
];

/** Client component because the current path can only be read in the browser. Pills as in the pool app. */
export function NavLinks() {
  const pathname = usePathname();
  return (
    // Scrolls sideways rather than wrapping or pushing the page wider on a phone.
    <nav className="-mr-4 flex min-w-0 items-center gap-1 overflow-x-auto pr-4 text-sm whitespace-nowrap">
      {NAV.map((n) => {
        const active = n.href === "/" ? pathname === "/" : pathname.startsWith(n.href);
        return (
          <Link
            key={n.href}
            href={n.href}
            aria-current={active ? "page" : undefined}
            className={
              active
                ? "rounded-full bg-surface-2 px-3 py-1.5 font-medium text-ink"
                : "rounded-full px-3 py-1.5 text-ink-2 transition-colors hover:text-ink"
            }
          >
            {n.label}
          </Link>
        );
      })}
    </nav>
  );
}
