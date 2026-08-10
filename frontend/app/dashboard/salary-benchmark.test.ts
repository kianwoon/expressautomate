import { describe, expect, it } from "vitest";

import {
  BAND_LABELS,
  benchmarkBand,
  marketPercentile,
  monthlyGrossSGD,
  parseRawSalary,
  type Offer,
} from "./salary-benchmark";

/**
 * The pure functions behind the salary benchmark chart. These are the
 * load-bearing arithmetic — currency/period conversion and the
 * percentile/band classification — so they are pinned independently of the
 * render. The chart itself is a thin projection of these values.
 *
 * allow-hardcode: the figures below are test inputs, not an oracle.
 */

const SGD_MONTHLY = (min: number, max?: number): Offer => ({
  min,
  max: max ?? min,
  currency: "SGD",
  period: "month",
});

// --------------------------------------------------------------------------- //
// parseRawSalary — the raw-text fallback
// --------------------------------------------------------------------------- //

describe("parseRawSalary", () => {
  it("parses a two-figure range", () => {
    expect(parseRawSalary("S$4800 to S$5200")).toEqual([4800, 5200]);
  });

  it("parses a dashed two-figure range", () => {
    expect(parseRawSalary("4800-5200")).toEqual([4800, 5200]);
  });

  it("parses a single figure as min == max", () => {
    expect(parseRawSalary("$3500")).toEqual([3500, 3500]);
  });

  it("orders an out-of-order two-figure range ascending", () => {
    expect(parseRawSalary("$4500 max, from $2700")).toEqual([2700, 4500]);
  });

  it("refuses a multi-figure sentence instead of guessing a range", () => {
    // Regression: "$4500 basic max + $800 Rotating shift allowance" — the
    // $800 allowance was read as the low end of a range, turning the offer
    // into a $2,650 midpoint and the benchmark into a bogus "below market".
    expect(
      parseRawSalary(
        "$4500 basic max + $800 Rotating shift allowance; $3500 for fresh Deg; $2700 for fresh dip and above depending on exp",
      ),
    ).toBeNull();
  });

  it("returns null when there is no text", () => {
    expect(parseRawSalary(null)).toBeNull();
    expect(parseRawSalary("")).toBeNull();
  });
});

// --------------------------------------------------------------------------- //
// monthlyGrossSGD — currency + period normalisation
// --------------------------------------------------------------------------- //

describe("monthlyGrossSGD", () => {
  it("passes through a monthly SGD offer at its midpoint", () => {
    expect(monthlyGrossSGD(SGD_MONTHLY(5000, 6000))).toBe(5500);
  });

  it("converts an annual SGD offer to monthly by /12", () => {
    expect(monthlyGrossSGD({ min: 66000, max: null, currency: "SGD", period: "year" })).toBe(5500);
  });

  it("converts a weekly SGD offer using the standard factor", () => {
    // 12/52 ≈ 0.2308; 1000/week → ~230.77/month
    const result = monthlyGrossSGD({ min: 1000, max: null, currency: "SGD", period: "week" })!;
    expect(result).toBeCloseTo(230.77, 1);
  });

  it("returns null for a non-SGD currency", () => {
    expect(monthlyGrossSGD({ min: 5000, max: null, currency: "USD", period: "month" })).toBeNull();
  });

  it("returns null when the period is unnormalised", () => {
    expect(monthlyGrossSGD({ min: 5000, max: null, currency: "SGD", period: "fortnight" })).toBeNull();
  });

  it("returns null when no salary figures exist", () => {
    expect(monthlyGrossSGD({ min: null, max: null, currency: "SGD", period: "month" })).toBeNull();
  });

  it("refuses a multi-figure raw salary rather than inventing a midpoint", () => {
    // Regression: the "Contract Biotechnologist" offer — "$4500 basic max +
    // $800 rotating shift allowance; $3500 fresh degree; $2700 fresh diploma".
    // Four figures is not a range this parser can read, so the offer must be
    // treated as not comparable instead of benchmarked at a $2,650 guess.
    expect(
      monthlyGrossSGD({
        min: null,
        max: null,
        currency: null,
        period: null,
        raw: "$4500 basic max + $800 Rotating shift allowance; $3500 for fresh Deg; $2700 for fresh dip and above depending on exp",
      }),
    ).toBeNull();
  });

  it("uses a single-ended offer (only max)", () => {
    expect(monthlyGrossSGD({ min: null, max: 6000, currency: "SGD", period: "month" })).toBe(6000);
  });

  it("treats currency case-insensitively", () => {
    expect(monthlyGrossSGD({ min: 5000, max: null, currency: "sgd", period: "month" })).toBe(5000);
  });
});

// --------------------------------------------------------------------------- //
// benchmarkBand — the four competitiveness bands
// --------------------------------------------------------------------------- //

const P25 = 4900;
const MED = 6500;
const P75 = 8165;

describe("benchmarkBand", () => {
  it("is 'below' when the offer is under P25", () => {
    expect(benchmarkBand(4000, P25, MED, P75)).toBe("below");
  });

  it("is 'competitive' between P25 and the median", () => {
    expect(benchmarkBand(5000, P25, MED, P75)).toBe("competitive");
  });

  it("is 'strong' between the median and P75", () => {
    expect(benchmarkBand(7000, P25, MED, P75)).toBe("strong");
  });

  it("is 'premium' above P75", () => {
    expect(benchmarkBand(10000, P25, MED, P75)).toBe("premium");
  });

  it("is null when the offer is not comparable", () => {
    expect(benchmarkBand(null, P25, MED, P75)).toBeNull();
  });

  it("classifies the boundary P25 as competitive (>= P25)", () => {
    expect(benchmarkBand(P25, P25, MED, P75)).toBe("competitive");
  });

  it("classifies the boundary P75 as strong (<= P75)", () => {
    expect(benchmarkBand(P75, P25, MED, P75)).toBe("strong");
  });
});

describe("BAND_LABELS", () => {
  it("labels every band", () => {
    expect(BAND_LABELS.below).toBe("Below market");
    expect(BAND_LABELS.competitive).toBe("Competitive");
    expect(BAND_LABELS.strong).toBe("Strong");
    expect(BAND_LABELS.premium).toBe("Premium");
  });
});

// --------------------------------------------------------------------------- //
// marketPercentile — piecewise-linear position
// --------------------------------------------------------------------------- //

describe("marketPercentile", () => {
  it("maps P25 to the 25th percentile", () => {
    expect(marketPercentile(P25, P25, MED, P75)).toBeCloseTo(25, 5);
  });

  it("maps the median to the 50th percentile", () => {
    expect(marketPercentile(MED, P25, MED, P75)).toBeCloseTo(50, 5);
  });

  it("maps P75 to the 75th percentile", () => {
    expect(marketPercentile(P75, P25, MED, P75)).toBeCloseTo(75, 5);
  });

  it("ramps below P25 toward 0", () => {
    // Halfway between 0 and P25 → ~12.5th percentile
    expect(marketPercentile(P25 / 2, P25, MED, P75)).toBeCloseTo(12.5, 1);
  });

  it("clamps to 0 at or below a zero offer", () => {
    expect(marketPercentile(0, P25, MED, P75)).toBe(0);
  });

  it("clamps above P75 toward 100 without pinning immediately", () => {
    // An offer above P75 but below 2×P75 should be between 75 and 100.
    const pct = marketPercentile(P75 * 1.5, P25, MED, P75)!;
    expect(pct).toBeGreaterThan(75);
    expect(pct).toBeLessThan(100);
  });

  it("returns null when the offer is not comparable", () => {
    expect(marketPercentile(null, P25, MED, P75)).toBeNull();
  });
});
