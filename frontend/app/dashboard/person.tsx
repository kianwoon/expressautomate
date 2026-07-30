"use client";

/**
 * How a person is drawn when there is no photo of them.
 *
 * These two helpers were written twice before this file existed — once in
 * `clients/client-logo.tsx` and once in `candidate-avatar.tsx`. The client
 * logo now imports them from here; the candidate avatar still carries its own
 * copy, which is worth folding in but belongs to a screen this feature does
 * not touch.
 */

export const LOGO_COLORS = [
  "#5b6ee1",
  "#e15b8f",
  "#2fa88a",
  "#c77f2f",
  "#8a5be1",
  "#2f8fc7",
  "#c74f4f",
  "#4f9e4f",
];

/** Deterministic, not random: the same seed always lands on the same colour.
 *
 * The seed is separate from the name on purpose. A person's colour keys on
 * their user id, so fixing a typo in their name does not recolour them
 * everywhere. A client logo keys on the client's name, which is what it
 * already did — passing the name preserves every existing logo's colour.
 */
export function colorFor(seed: string): string {
  let hash = 0;
  for (let i = 0; i < seed.length; i += 1) {
    hash = (hash * 31 + seed.charCodeAt(i)) | 0;
  }
  return LOGO_COLORS[Math.abs(hash) % LOGO_COLORS.length];
}

export function initialsFor(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

/**
 * A person, as a coloured disc of initials.
 *
 * `role="img"` with the full name as the label: the two letters inside are a
 * picture of a person, not text worth reading out. A screen reader announces
 * "Priya Nair", not "PN".
 */
export function Initials({
  name,
  seed,
  size = 24,
}: {
  name: string;
  seed: string;
  size?: number;
}) {
  return (
    <span
      className="person-initials"
      role="img"
      aria-label={name}
      style={{ width: size, height: size, background: colorFor(seed), fontSize: size * 0.4 }}
    >
      {initialsFor(name)}
    </span>
  );
}
