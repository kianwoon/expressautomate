/** The Singapore lion head for the "Built in Singapore" line.
 *
 *  Replaces the earlier hand-drawn approximation: this is the real mark the
 *  agency uses, shipped as the PNG the brand provided (public/lion-mark.png).
 *  It is a brand asset rather than code, so there is nothing to parameterise
 *  here — the component exists so the footer can size it with the same
 *  `size` prop the old inline SVG took, and so a future swap of the asset
 *  file is a drop-in change. */
export function LionMark({ size = 20 }: { size?: number }) {
  return (
    // A brand PNG in /public, not something `next/image` can optimise — same
    // tradeoff as `whatsapp.svg`, which is also shipped as a plain asset.
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src="/lion-mark.png"
      alt=""
      aria-hidden="true"
      width={size}
      height={size}
      style={{ flex: "none" }}
    />
  );
}
