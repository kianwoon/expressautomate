"use client";

import type { OccupationMatch } from "./job-intelligence";

/**
 * The salary benchmark chart for the Work tab.
 *
 * Pure, library-free rendering of the matched MOM occupation's P25/Median/P75
 * against the job order's own salary offer — the first chart in the app, and
 * deliberately so. The project's stance (see `stat-cards.tsx`) is anti-fake-
 * data: no sparkline is drawn unless a real series backs it. Here every value
 * on the bar is a survey percentile or the recruiter's own offer, so a CSS bar
 * is honest without a 100 KB dependency.
 *
 * The comparison is honest about its limits: the MOM survey reports monthly
 * *gross* wages in SGD, so an offer in a different currency or period is shown
 * alongside the percentiles without a position marker, rather than converted
 * at a guessed rate and presented as comparable (landing-honesty-ethos).
 *
 * allow-hardcode: the band labels and conversion table below are user-facing
 * domain constants, not configuration.
 */

/** The salary offer as the row carries it. */
export type Offer = {
  min: number | null;
  max: number | null;
  currency: string | null;
  period: string | null;
  /** The sender's original salary text, shown when min/max weren't parsed. */
  raw?: string | null;
};

/**
 * Convert an offer to a monthly gross SGD figure, or null when it is not
 * comparable to the survey (different currency, or an unnormalised period).
 *
 * The survey is monthly gross SGD; a fair comparison requires the same unit.
 * Annual offers divide by 12; weekly by ~4.345; daily by ~21.75 (avg working
 * days/month); hourly by ~173.3 (avg monthly hours). These are the standard
 * Singapore labour-market conversions, not configurable — they are the
 * arithmetic that makes a yearly and a monthly figure sit on the same bar.
 */
// allow-hardcode: standard SG labour-market conversion factors.
const PERIOD_TO_MONTHLY: Record<string, number> = {
  month: 1,
  year: 1 / 12,
  week: 12 / 52,
  day: 12 / (52 * 5),
  hour: 12 / (52 * 44),
};

export function monthlyGrossSGD(offer: Offer): number | null {
  if (!offer.currency || offer.currency.toUpperCase() !== "SGD") return null;
  if (!offer.period) return null;
  const factor = PERIOD_TO_MONTHLY[offer.period.toLowerCase()];
  if (!factor) return null;
  // Use the midpoint of the range, or whichever end exists. A range is one
  // offer band, so its comparable point is the midpoint.
  const min = offer.min;
  const max = offer.max;
  const point =
    min != null && max != null
      ? (min + max) / 2
      : (min ?? max);
  if (point == null) return null;
  return point * factor;
}

/** The competitiveness bands the design doc defines. */
export type Band = "below" | "competitive" | "strong" | "premium";

export const BAND_LABELS: Record<Band, string> = {
  below: "Below market",
  competitive: "Competitive",
  strong: "Strong",
  premium: "Premium",
};

/**
 * Classify an offer against the percentiles. The thresholds mirror the design
 * doc exactly: below P25 is below market; P25–median is competitive; median–
 * P75 is strong; above P75 is premium. Returns null when the offer is not
 * comparable (monthlyGrossSGD returned null).
 */
export function benchmarkBand(
  offerMonthlySGD: number | null,
  p25: number,
  median: number,
  p75: number,
): Band | null {
  if (offerMonthlySGD == null) return null;
  if (offerMonthlySGD < p25) return "below";
  if (offerMonthlySGD < median) return "competitive";
  if (offerMonthlySGD <= p75) return "strong";
  return "premium";
}

/**
 * Approximate market percentile of the offer, piecewise-linear between the
 * survey points and clamped to [0, 100].
 *
 * Below P25 maps to the 0–25 ramp; P25–median to 25–50; median–P75 to 50–75;
 * above P75 to the 75–100 ramp. The ramp above P75 stretches to twice P75 at
 * the 100th mark (a soft ceiling), so an offer far above market does not pin
 * at exactly 100 and erase all gradation. Returns null when the offer is not
 * comparable.
 */
export function marketPercentile(
  offerMonthlySGD: number | null,
  p25: number,
  median: number,
  p75: number,
): number | null {
  if (offerMonthlySGD == null) return null;
  const clamp = (n: number) => Math.max(0, Math.min(100, n));
  if (offerMonthlySGD < p25) {
    if (p25 <= 0) return 0;
    return clamp((offerMonthlySGD / p25) * 25);
  }
  if (offerMonthlySGD < median) {
    if (median === p25) return 25;
    return clamp(25 + ((offerMonthlySGD - p25) / (median - p25)) * 25);
  }
  if (offerMonthlySGD <= p75) {
    if (p75 === median) return 50;
    return clamp(50 + ((offerMonthlySGD - median) / (p75 - median)) * 25);
  }
  // Above P75: ramp toward 2× P75 as the soft ceiling.
  const ceiling = p75 * 2;
  if (ceiling === p75) return 100;
  return clamp(75 + ((offerMonthlySGD - p75) / (ceiling - p75)) * 25);
}

/** Format a wage as compact SGD, e.g. "$6,658". */
function sgd(value: number): string {
  return "$" + value.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

/** Format the offer for display, with its original currency/period.
 *  Falls back to the raw salary text when the structured min/max were never
 *  parsed (a common extraction gap), so the chart never shows a bare "—". */
function offerLabel(offer: Offer): string {
  const min = offer.min;
  const max = offer.max;
  if (min == null && max == null) {
    // The raw text is the sender's own words — always more useful than a dash.
    return offer.raw || "—";
  }
  const amount =
    min != null && max != null && min !== max
      ? `${min.toLocaleString()}–${max.toLocaleString()}`
      : (min ?? max)!.toLocaleString();
  const currency = offer.currency ? `${offer.currency} ` : "";
  const period = offer.period ? ` / ${offer.period}` : "";
  return `${currency}${amount}${period}`;
}

export function SalaryBenchmark({
  occupation,
  offer,
}: {
  occupation: OccupationMatch;
  offer: Offer;
}) {
  const { gross_p25: p25, gross_p50: median, gross_p75: p75 } = occupation;
  const offerMonthly = monthlyGrossSGD(offer);
  const band = benchmarkBand(offerMonthly, p25, median, p75);
  const pct = marketPercentile(offerMonthly, p25, median, p75);
  const comparable = offerMonthly != null;

  // The offer as a range (min→max), or a single point if only one end exists.
  // When the structured min/max are absent, we can't draw a range bar — the
  // raw text label still shows in the legend below.
  const offerMin = comparable ? monthlyGrossSGD({ ...offer, max: null }) : null;
  const offerMax = comparable ? monthlyGrossSGD({ ...offer, min: null }) : null;
  const hasOfferRange = comparable && offerMin != null && offerMax != null && offerMin > 0;

  // Scale from 0 to 110% of the largest value, so both bars fit with headroom.
  const scaleMax = Math.max(p75, offerMax ?? offerMonthly ?? 0) * 1.1;
  const pctOf = (v: number) => `${(v / scaleMax) * 100}%`;

  return (
    <div className="jo-intel-stage jo-benchmark">
      <h4 className="jo-intel-stage-title">Salary benchmark</h4>
      <div className="jo-benchmark-match">
        <span className="body">
          <span className="jo-benchmark-title">{occupation.title}</span>
        </span>
        <span className="jo-sub">
          {" "}· MOM {occupation.year} resident wages (monthly gross SGD)
        </span>
      </div>

      <div className="jo-benchmark-track" role="img" aria-label="Salary benchmark">
        {/* Market range: a single filled bar from P25 to P75. */}
        <div
          className="jo-benchmark-market"
          style={{ left: pctOf(p25), width: pctOf(p75 - p25) }}
        />

        {/* Median tick line inside the market bar. */}
        <div
          className="jo-benchmark-median"
          style={{ left: pctOf(median) }}
        />

        {/* Offer range: an overlaid bar from offer min to offer max. */}
        {hasOfferRange && (
          <div
            className="jo-benchmark-offer"
            style={{ left: pctOf(offerMin!), width: pctOf(offerMax! - offerMin!) }}
            aria-label={`Your offer ${sgd(offerMin!)} to ${sgd(offerMax!)}`}
          />
        )}
      </div>

      <dl className="jo-benchmark-legend">
        <div>
          <dt className="jo-sub">P25</dt>
          <dd className="body">{sgd(p25)}</dd>
        </div>
        <div>
          <dt className="jo-sub">Median</dt>
          <dd className="body">{sgd(median)}</dd>
        </div>
        <div>
          <dt className="jo-sub">P75</dt>
          <dd className="body">{sgd(p75)}</dd>
        </div>
        <div>
          <dt className="jo-sub">Your offer</dt>
          <dd className="body">{offerLabel(offer)}</dd>
        </div>
      </dl>

      {comparable && band ? (
        <p className="body jo-benchmark-verdict">
          <span className={`jo-benchmark-band jo-benchmark-band-${band}`}>
            {BAND_LABELS[band]}
          </span>
          {pct != null && (
            <span className="jo-sub">
              {" "}· ~{Math.round(pct)}th percentile of market
            </span>
          )}
        </p>
      ) : (
        <p className="body jo-sub jo-benchmark-note">
          Offer not directly comparable to the survey (currency or period
          differs). Percentiles shown for reference.
        </p>
      )}

      {occupation.rationale && (
        <p className="body jo-sub jo-benchmark-rationale">
          {occupation.rationale}
          {occupation.confidence > 0 && (
            <span className="jo-benchmark-conf">
              {" "}· match confidence {Math.round(occupation.confidence * 100)}%
            </span>
          )}
        </p>
      )}
    </div>
  );
}
