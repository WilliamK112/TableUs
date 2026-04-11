"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Search, PenSquare, User, Users, ChevronDown } from "lucide-react";
import { useUser } from "../context/user-context";
import { useState } from "react";

const NAV = [
  { href: "/discover", label: "Discover", icon: Search },
  { href: "/friends", label: "Friends", icon: Users },
  { href: "/review", label: "Review", icon: PenSquare },
  { href: "/profile", label: "Profile", icon: User },
];

export function Sidebar() {
  const pathname = usePathname();
  const { currentUser, allUsers, switchUser } = useUser();
  const [open, setOpen] = useState(false);

  return (
    <aside className="w-72 h-full flex flex-col shrink-0 border-r border-[var(--border)] bg-white/70 shadow-[12px_0_40px_rgba(248,180,108,0.08)] backdrop-blur-xl">
      {/* Logo */}
      <div className="p-7 pb-5">
        <h1 className="text-3xl font-bold tracking-tight">
          <span className="bg-[linear-gradient(135deg,var(--accent),var(--accent-light))] bg-clip-text text-transparent">TableUs</span>
        </h1>
        <p className="mt-2 text-sm text-[var(--muted-foreground)]">Food plans, shared at the table.</p>
      </div>

      {/* Nav */}
      <nav className="flex-1 px-4 space-y-2">
        {NAV.map(({ href, label, icon: Icon }) => {
          const active = pathname === href || (href === "/discover" && pathname === "/");
          return (
            <Link
              key={href}
              href={href}
              className={`flex items-center gap-3 rounded-2xl px-4 py-3 text-sm font-medium transition-all ${
                active
                  ? "bg-[linear-gradient(135deg,rgba(255,138,61,0.14),rgba(17,181,164,0.12))] text-[var(--foreground)] shadow-[0_10px_30px_rgba(255,138,61,0.1)]"
                  : "text-[var(--muted-foreground)] hover:text-[var(--foreground)] hover:bg-white/65"
              }`}
            >
              <div className={`flex h-9 w-9 items-center justify-center rounded-full ${
                active ? "bg-white/80 text-[var(--accent)]" : "bg-[var(--muted)]/80 text-[var(--muted-foreground)]"
              }`}>
                <Icon className="w-4 h-4" />
              </div>
              {label}
            </Link>
          );
        })}
      </nav>

      {/* User Switcher */}
      <div className="border-t border-[var(--border)] p-4">
        <button
          onClick={() => setOpen(!open)}
          className="w-full flex items-center gap-3 rounded-3xl bg-white/80 px-4 py-3 shadow-[0_12px_30px_rgba(244,186,114,0.12)] transition-colors hover:bg-white"
        >
          {currentUser && (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={currentUser.avatar} alt="" className="h-10 w-10 rounded-full bg-[var(--muted)]" />
          )}
          <div className="flex-1 text-left">
            <p className="text-sm font-medium truncate">{currentUser?.name ?? "Loading..."}</p>
            <p className="text-xs text-[var(--muted-foreground)]">Manage my account</p>
          </div>
          <ChevronDown className={`w-4 h-4 text-[var(--muted-foreground)] transition-transform ${open ? "rotate-180" : ""}`} />
        </button>

        {open && (
          <div className="mt-2 space-y-1 rounded-3xl bg-white/72 p-2">
            {allUsers.map((u) => (
              <button
                key={u.id}
                onClick={() => { switchUser(u.id); setOpen(false); }}
                className={`w-full flex items-center gap-3 rounded-2xl px-3 py-2 text-sm transition-colors ${
                  u.id === currentUser?.id
                    ? "bg-[linear-gradient(135deg,rgba(255,138,61,0.12),rgba(17,181,164,0.1))] text-[var(--foreground)]"
                    : "text-[var(--muted-foreground)] hover:text-[var(--foreground)] hover:bg-white/80"
                }`}
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={u.avatar} alt="" className="w-6 h-6 rounded-full bg-[var(--muted)]" />
                {u.name}
              </button>
            ))}
          </div>
        )}
      </div>
    </aside>
  );
}
