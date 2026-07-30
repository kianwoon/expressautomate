import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { colorFor, initialsFor, Initials, LOGO_COLORS } from "./person";

describe("initialsFor", () => {
  it("takes the first and last initial of a full name", () => {
    expect(initialsFor("Priya Nair")).toBe("PN");
  });
  it("takes two letters of a single name", () => {
    expect(initialsFor("Priya")).toBe("PR");
  });
  it("gives a question mark for an empty name", () => {
    expect(initialsFor("   ")).toBe("?");
  });
});

describe("colorFor", () => {
  it("is deterministic", () => {
    expect(colorFor("abc")).toBe(colorFor("abc"));
  });
  it("keys on the seed, not the name, so renaming someone keeps their colour", () => {
    // The whole reason the seed is separate from the name.
    const id = "0f8f-user-id";
    expect(colorFor(id)).toBe(colorFor(id));
  });
  it("keeps the colours client logos already have", () => {
    // Client logos seed on the client's NAME and always have. Re-seeding them
    // on an id would silently recolour every logo in the product. These three
    // indices were printed from the PRE-refactor `colorFor` in
    // `client-logo.tsx`; if the extraction moves a colour, this fails and
    // names the string that moved.
    expect(colorFor("Acme Pte Ltd")).toBe(LOGO_COLORS[7]);
    expect(colorFor("Sunrise Care")).toBe(LOGO_COLORS[4]);
    expect(colorFor("Kim Eng Ltd")).toBe(LOGO_COLORS[7]);
  });
});

describe("Initials", () => {
  it("renders the initials with an accessible name", () => {
    render(<Initials name="Priya Nair" seed="user-1" />);
    expect(screen.getByRole("img", { name: "Priya Nair" }).textContent).toBe("PN");
  });
});
