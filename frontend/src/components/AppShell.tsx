import { useEffect, useRef, useState, type ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";

import {
  CloseIcon,
  DashboardIcon,
  LogsIcon,
  MenuIcon,
  SettingsIcon,
} from "./icons";

const navigation = [
  { end: true, icon: DashboardIcon, label: "Dashboard", to: "/" },
  { end: false, icon: SettingsIcon, label: "Settings", to: "/settings" },
  { end: false, icon: LogsIcon, label: "Live logs", to: "/logs" },
];

function pageTitle(pathname: string): string {
  return navigation.find(({ to }) => to === pathname)?.label ?? "Dashboard";
}

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(
    () => window.matchMedia(query).matches,
  );

  useEffect(() => {
    const media = window.matchMedia(query);
    const update = (event: MediaQueryListEvent) => setMatches(event.matches);
    setMatches(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [query]);

  return matches;
}

function NavItems({ onNavigate }: { onNavigate?: () => void }) {
  return navigation.map(({ end, icon: Icon, label, to }) => (
    <NavLink
      className={({ isActive }) =>
        `group flex min-h-11 items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400 ${
          isActive
            ? "bg-sky-400/10 text-sky-300 ring-1 ring-inset ring-sky-400/15"
            : "text-zinc-400 hover:bg-white/[0.05] hover:text-zinc-100"
        }`
      }
      end={end}
      key={to}
      onClick={onNavigate}
      to={to}
    >
      <Icon className="shrink-0 transition group-hover:scale-105" />
      <span>{label}</span>
    </NavLink>
  ));
}

function Brand() {
  return (
    <div className="flex items-center gap-3">
      <div className="grid size-10 place-items-center rounded-xl bg-gradient-to-br from-sky-400 to-cyan-600 text-lg font-black text-sky-950 shadow-lg shadow-sky-950/40">
        C
      </div>
      <div>
        <p className="text-base font-bold tracking-tight text-white">
          Crowdarrr
        </p>
        <p className="flex items-center gap-1.5 text-[11px] text-zinc-500">
          <span className="size-1.5 rounded-full bg-emerald-400 shadow-[0_0_8px_rgb(52_211_153)]" />
          NFO companion
        </p>
      </div>
    </div>
  );
}

export default function AppShell({ children }: { children: ReactNode }) {
  const compact = useMediaQuery("(max-width: 767px)");
  const { pathname } = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const [routeAnnouncement, setRouteAnnouncement] = useState<string>();
  const initialPathnameRef = useRef(pathname);
  const mainRef = useRef<HTMLElement>(null);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const currentPage = pageTitle(pathname);

  useEffect(() => {
    document.title = `${currentPage} · Crowdarrr`;
    mainRef.current?.querySelector<HTMLHeadingElement>("h1")?.focus();

    if (initialPathnameRef.current !== pathname) {
      setRouteAnnouncement(currentPage);
      initialPathnameRef.current = pathname;
    }
  }, [currentPage, pathname]);

  useEffect(() => {
    if (!menuOpen) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setMenuOpen(false);
        menuButtonRef.current?.focus();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [menuOpen]);

  return (
    <div className="min-h-screen bg-[#09090b] text-zinc-100 selection:bg-sky-400/30">
      <a
        className="fixed left-3 top-3 z-50 -translate-y-20 rounded-lg bg-sky-400 px-4 py-2 font-semibold text-sky-950 transition focus:translate-y-0"
        href="#main-content"
      >
        Skip to content
      </a>

      {compact ? (
        <header className="sticky top-0 z-40 flex h-16 items-center justify-between border-b border-white/[0.07] bg-zinc-950/90 px-4 backdrop-blur-xl">
          <Brand />
          <button
            aria-expanded={menuOpen}
            aria-label={menuOpen ? "Close navigation" : "Open navigation"}
            className="grid size-11 place-items-center rounded-xl border border-white/10 bg-white/[0.04] text-zinc-300 transition hover:bg-white/[0.08] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400"
            onClick={() => setMenuOpen((current) => !current)}
            ref={menuButtonRef}
            type="button"
          >
            {menuOpen ? <CloseIcon /> : <MenuIcon />}
          </button>
        </header>
      ) : (
        <aside className="fixed inset-y-0 left-0 z-30 flex w-64 flex-col border-r border-white/[0.07] bg-zinc-950/95 px-4 py-5">
          <div className="px-2">
            <Brand />
          </div>
          <nav aria-label="Primary navigation" className="mt-9 space-y-1">
            <NavItems />
          </nav>
          <div className="mt-auto rounded-xl border border-white/[0.06] bg-white/[0.025] p-3 text-xs leading-5 text-zinc-500">
            <p className="font-medium text-zinc-300">Byte-exact repairs</p>
            <p className="mt-1">
              Raw NFO bytes stay untouched from lookup to recheck.
            </p>
          </div>
        </aside>
      )}

      {compact && menuOpen ? (
        <div className="fixed inset-x-0 top-16 z-30 border-b border-white/[0.07] bg-zinc-950/95 p-4 shadow-2xl backdrop-blur-xl">
          <nav aria-label="Mobile navigation" className="space-y-1">
            <NavItems onNavigate={() => setMenuOpen(false)} />
          </nav>
        </div>
      ) : null}

      {routeAnnouncement ? (
        <p
          aria-atomic="true"
          aria-label="Current page"
          aria-live="polite"
          className="sr-only"
          role="status"
        >
          {routeAnnouncement}
        </p>
      ) : null}

      <main
        className={compact ? "min-h-screen" : "min-h-screen pl-64"}
        id="main-content"
        ref={mainRef}
      >
        <div className="mx-auto max-w-[1500px] px-4 py-7 sm:px-6 sm:py-9 lg:px-10">
          {children}
        </div>
      </main>
    </div>
  );
}
