/** Brand mark from branding.png: an "e" whose bowl opens into a circular
 *  arrow, with speed lines entering from the left, on a blue→teal gradient.
 *  Drawn as SVG so it stays sharp and recolourable rather than shipping the
 *  raster sheet. `mono` renders the monochrome variant from the same sheet.
 *
 *  allow-hardcode: the strings below are SVG path geometry — vector drawing
 *  coordinates, not matching rules or a detect-list.
 */
export function Logo({ size = 36, mono = false }: { size?: number; mono?: boolean }) {
  const id = mono ? "ea-mono" : "ea-grad";
  const stroke = mono ? "currentColor" : `url(#${id})`;
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      role="img"
      aria-label="expressautomate.app"
    >
      {!mono && (
        <defs>
          <linearGradient id={id} x1="4" y1="8" x2="58" y2="58" gradientUnits="userSpaceOnUse">
            <stop stopColor="#1D4ED8" />
            <stop offset="1" stopColor="#14A89A" />
          </linearGradient>
        </defs>
      )}

      {/* speed lines entering from the left */}
      <g fill={stroke}>
        <circle cx="6" cy="22" r="3" />
        <circle cx="7" cy="32" r="2.6" />
        <circle cx="9" cy="42" r="2.2" />
      </g>
      <g stroke={stroke} strokeWidth="3.4" strokeLinecap="round">
        <path d="M12 22h9" />
        <path d="M13 32h7" />
        <path d="M15 42h5" />
      </g>

      {/* the bowl and crossbar of the "e" */}
      <path
        d="M24 33c0-8 6.5-14 14.5-14S52 24.2 52 30.5c0 1.6-1 2.5-2.7 2.5H24z"
        stroke={stroke}
        strokeWidth="4"
        strokeLinejoin="round"
      />
      {/* circular arrow closing the loop */}
      <path d="M50 39a14 14 0 0 1-25.6 2.4" stroke={stroke} strokeWidth="4" strokeLinecap="round" />
      <path d="M45 36.5 51.5 39 49 45.5z" fill={stroke} />
    </svg>
  );
}
