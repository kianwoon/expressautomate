/** A stylised lion head for the "Built in Singapore" line.
 *
 *  Deliberately NOT Singapore's official Lion Head Symbol: that mark is a
 *  protected national symbol and a private company may not put it on its own
 *  site. This is an original drawing that reads as a lion, nothing more.
 *
 *  The mane is generated rather than hand-plotted so the point count and the
 *  radii stay adjustable — the alternative is a path string nobody can edit.
 *  The face, ears and muzzle stay literal: there is no shape to parameterise.
 *
 *  allow-hardcode: as in logo.tsx, the numbers below are SVG drawing
 *  coordinates and stroke widths — vector geometry, not matching rules or a
 *  detect-list.
 */

const MANE_POINTS = 11;
/** Varying tip lengths — a mane of equal spikes reads as a sun, not an animal.
 *  Three lengths against 11 points is deliberate: the counts share no factor,
 *  so the cycle never lands two equal spikes side by side, not even where it
 *  wraps at the top. */
const TIP_RADII = [30, 26.5, 28];
const VALLEY_RADIUS = 20;
const CENTRE = 32;

function manePath() {
  const step = (Math.PI * 2) / MANE_POINTS;
  const at = (angle: number, radius: number) =>
    `${(CENTRE + Math.cos(angle) * radius).toFixed(2)} ${(CENTRE + Math.sin(angle) * radius).toFixed(2)}`;

  const segments: string[] = [];
  for (let i = 0; i < MANE_POINTS; i++) {
    const tip = i * step - Math.PI / 2;
    segments.push(at(tip, TIP_RADII[i % TIP_RADII.length]), at(tip + step / 2, VALLEY_RADIUS));
  }
  return `M${segments.join("L")}Z`;
}

export function LionMark({ size = 20 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      aria-hidden="true"
      focusable="false"
    >
      <path
        d={manePath()}
        stroke="currentColor"
        strokeWidth="3"
        strokeLinejoin="round"
      />
      {/* The face outline is what stops the mane reading as a sunburst: it
          gives the features something to sit inside. */}
      <circle cx="32" cy="32" r="16" stroke="currentColor" strokeWidth="2.4" />
      {/* Ears, tucked against the face where the mane parts. */}
      <path
        d="M21.5 21.5c-1.5-3 .3-5.2 3.4-4.2M42.5 21.5c1.5-3-.3-5.2-3.4-4.2"
        stroke="currentColor"
        strokeWidth="2.4"
        strokeLinecap="round"
      />
      {/* Brow-shaded eyes: a flat top edge is the difference between a lion and
          a cat at this size. */}
      <path
        d="M23.5 27.5h5.5M34.5 27.5h5.5"
        stroke="currentColor"
        strokeWidth="2.6"
        strokeLinecap="round"
      />
      <circle cx="26.5" cy="31" r="2" fill="currentColor" />
      <circle cx="37.5" cy="31" r="2" fill="currentColor" />
      {/* Muzzle: broad nose, philtrum, and the two jowls under it. */}
      <path d="M32 39.5 36.5 34h-9z" fill="currentColor" />
      <path
        d="M32 39.5v2.2M32 41.7c0 2.6-2.4 4-4.8 2.9M32 41.7c0 2.6 2.4 4 4.8 2.9"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
      />
    </svg>
  );
}
