import type { ButtonHTMLAttributes, ReactNode } from "react";

export function PageHeader({
  actions,
  eyebrow,
  title,
  description,
}: {
  actions?: ReactNode;
  description: string;
  eyebrow?: string;
  title: string;
}) {
  return (
    <header className="flex flex-col gap-5 border-b border-white/5 pb-7 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0">
        {eyebrow ? (
          <p className="mb-2 text-xs font-semibold uppercase tracking-[0.2em] text-sky-400">
            {eyebrow}
          </p>
        ) : null}
        <h1
          className="rounded-sm text-3xl font-semibold tracking-tight text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400 focus-visible:ring-offset-4 focus-visible:ring-offset-zinc-950 sm:text-4xl"
          tabIndex={-1}
        >
          {title}
        </h1>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-zinc-400">
          {description}
        </p>
      </div>
      {actions ? <div className="shrink-0">{actions}</div> : null}
    </header>
  );
}

export function Panel({
  actions,
  children,
  description,
  title,
}: {
  actions?: ReactNode;
  children: ReactNode;
  description?: string;
  title: string;
}) {
  return (
    <section className="rounded-2xl border border-white/[0.07] bg-zinc-900/75 shadow-panel backdrop-blur-sm">
      <div className="flex flex-col gap-3 border-b border-white/[0.06] px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="font-semibold text-zinc-100">{title}</h2>
          {description ? (
            <p className="mt-1 text-xs leading-5 text-zinc-500">
              {description}
            </p>
          ) : null}
        </div>
        {actions}
      </div>
      {children}
    </section>
  );
}

export function Button({
  children,
  className = "",
  variant = "secondary",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "danger" | "ghost";
}) {
  const variants = {
    danger:
      "border-red-400/20 bg-red-400/10 text-red-200 hover:bg-red-400/15 focus-visible:ring-red-400",
    ghost:
      "border-transparent bg-transparent text-zinc-400 hover:bg-white/5 hover:text-zinc-100 focus-visible:ring-zinc-400",
    primary:
      "border-sky-400/20 bg-sky-500 text-sky-950 hover:bg-sky-400 focus-visible:ring-sky-400",
    secondary:
      "border-white/10 bg-white/[0.05] text-zinc-200 hover:bg-white/[0.09] focus-visible:ring-zinc-400",
  };

  return (
    <button
      className={`inline-flex min-h-10 items-center justify-center gap-2 rounded-lg border px-3.5 py-2 text-sm font-semibold transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-offset-zinc-950 disabled:cursor-not-allowed disabled:opacity-50 ${variants[variant]} ${className}`}
      {...props}
    >
      {children}
    </button>
  );
}

export function LoadingBlock({ label }: { label: string }) {
  return (
    <div className="flex min-h-44 items-center justify-center px-6 py-12 text-sm text-zinc-500">
      <span className="mr-3 size-2 animate-pulse rounded-full bg-sky-400" />
      {label}
    </div>
  );
}

export function ErrorNotice({
  children,
  onRetry,
}: {
  children: ReactNode;
  onRetry?: () => void;
}) {
  return (
    <div
      className="flex flex-col gap-3 rounded-xl border border-red-400/20 bg-red-400/[0.08] p-4 text-sm text-red-100 sm:flex-row sm:items-center sm:justify-between"
      role="alert"
    >
      <p>{children}</p>
      {onRetry ? (
        <Button
          className="shrink-0"
          onClick={onRetry}
          type="button"
          variant="danger"
        >
          Retry
        </Button>
      ) : null}
    </div>
  );
}

export const inputClassName =
  "min-h-10 w-full rounded-lg border border-white/10 bg-zinc-950/70 px-3 py-2 text-sm text-zinc-100 shadow-inner outline-none transition placeholder:text-zinc-600 focus:border-sky-400/60 focus:ring-2 focus:ring-sky-400/15 disabled:cursor-not-allowed disabled:opacity-60";

export const labelClassName = "mb-1.5 block text-xs font-medium text-zinc-400";
