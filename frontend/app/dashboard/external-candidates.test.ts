import { describe, expect, it } from "vitest";

import { identityFingerprint, type ExternalCandidate } from "./external-candidates";

/**
 * The §6 fingerprint builder — the one place external candidate data is turned
 * into the attributes the resolver searches on. It is load-bearing: §52 makes a
 * wrong employer the most expensive mistake, so the tests pin both what it
 * harvests and what it refuses to guess.
 *
 * allow-hardcode: the strings below are test fixtures.
 */

function candidate(overrides: Partial<ExternalCandidate> = {}): ExternalCandidate {
  return {
    id: "c-1",
    title: "Claire Sze Wei Chew",
    subtitle: "Product Control at Standard Chartered",
    location: "Singapore, Singapore",
    source: "linkedin_people",
    source_platform: "LinkedIn",
    source_url: "https://www.linkedin.com/in/claire-chew/",
    match_score: 90,
    match_reason: "Previously at Credit Suisse. Basel III reporting strength.",
    gaps: [],
    recommended_action: null,
    status: "new",
    summary: "Product controller with 12 years in banking.",
    skills: ["Product Control", "Basel III", "Regulatory Reporting", "FRTB"],
    credibility: null,
    ...overrides,
  };
}

describe("identityFingerprint", () => {
  it("splits the subtitle and harvests the full candidate context", () => {
    const fp = identityFingerprint(candidate());
    expect(fp.name).toBe("Claire Sze Wei Chew");
    expect(fp.current_title).toBe("Product Control");
    expect(fp.current_company).toBe("Standard Chartered");
    expect(fp.location).toBe("Singapore, Singapore");
    expect(fp.skills).toEqual(["Product Control", "Basel III", "Regulatory Reporting", "FRTB"]);
    expect(fp.context).toContain("Product controller with 12 years");
    expect(fp.source_profile_url).toBe("https://www.linkedin.com/in/claire-chew/");
    expect(fp.source_provider).toBe("LinkedIn");
  });

  it("recovers a previous employer only from an explicit marker", () => {
    expect(identityFingerprint(candidate()).previous_companies).toEqual(["Credit Suisse"]);
    expect(
      identityFingerprint(
        candidate({ match_reason: "Strong banking background.", summary: null }),
      ).previous_companies,
    ).toEqual([]);
  });

  it("recovers employers from an 'at X and Y' list, pre-dedup against current", () => {
    // The Andrew Ng / JobStreet prose shape: no "previously" marker at all.
    const prose =
      "14+ years Product Control at UBS and Barclays in Singapore; exact domain match with deep tenure";
    const fp = identityFingerprint(
      candidate({
        title: "Andrew Ng",
        subtitle: "Product Control at UBS",
        summary: prose,
        match_reason: null,
      }),
    );
    // Both names surface from the list (first mention = current employer), and
    // the current employer is dropped from previous.
    expect(fp.current_company).toBe("UBS");
    expect(fp.previous_companies).toEqual(["Barclays"]);
  });

  it("ignores a long prose phrase on either side of 'and'", () => {
    // "at UBS and a very long descriptive phrase" must not be guessed at (§52).
    const fp = identityFingerprint(
      candidate({
        subtitle: "Product Control at UBS",
        summary: "at UBS and several other Banks in the region",
        match_reason: null,
      }),
    );
    expect(fp.previous_companies).toEqual([]);
  });

  it("prefers an explicit company field over the subtitle split", () => {
    const fp = identityFingerprint(candidate({ company: "UBS" } as never));
    expect(fp.current_company).toBe("UBS");
  });

  it("caps skills at 8 and truncates context to 500 chars", () => {
    const many = Array.from({ length: 20 }, (_, i) => `Skill ${i}`);
    const fp = identityFingerprint(
      candidate({ skills: many, summary: "x".repeat(900), match_reason: null }),
    );
    expect(fp.skills).toHaveLength(8);
    expect(fp.context?.length).toBe(500);
  });

  it("leaves employer null when the subtitle has no 'at' marker", () => {
    const fp = identityFingerprint(
      candidate({ subtitle: "Hands-On Software Architect" }),
    );
    expect(fp.current_title).toBe("Hands-On Software Architect");
    expect(fp.current_company).toBeNull();
  });
});
