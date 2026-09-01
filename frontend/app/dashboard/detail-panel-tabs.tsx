"use client";

/**
 * The tab bar for the job order modal.
 *
 * Reuses the visual style of the settings `.nt-tab` / `.nt-tab-on` rules but
 * under its own `jo-` classes, because the settings version's border is a
 * white-alpha line meant for a dark panel — the modal is a light surface and
 * needs `var(--line)` instead.
 *
 * Buttons, not links: this is client-side state inside a modal, not a route.
 * `role="tab"` / `aria-selected` carry the semantics to assistive tech.
 */

export type TabKey = "origin" | "work" | "person" | "search" | "external";

export const JOB_ORDER_TABS: { key: TabKey; label: string }[] = [
  { key: "origin", label: "Origin" },
  { key: "work", label: "Work" },
  { key: "person", label: "Person" },
  { key: "search", label: "Search" },
  { key: "external", label: "External Candidates" },
];

export function TabBar({
  active,
  onSelect,
}: {
  active: TabKey;
  onSelect: (tab: TabKey) => void;
}) {
  return (
    <div className="jo-tabs" role="tablist" aria-label="Job order sections">
      {JOB_ORDER_TABS.map((t) => (
        <button
          key={t.key}
          type="button"
          role="tab"
          aria-selected={t.key === active}
          className={t.key === active ? "jo-tab jo-tab-on" : "jo-tab"}
          onClick={() => onSelect(t.key)}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}
