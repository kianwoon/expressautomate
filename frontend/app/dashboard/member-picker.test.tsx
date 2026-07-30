import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AuthState } from "../auth";
import type { MembersState } from "./members";
import { MemberPicker, MemberSelect } from "./member-picker";

/**
 * What these pin, and why each one is worth a test.
 *
 * The picker reads `useAuth()` itself rather than taking the caller's id as a
 * prop, so the exclusion cannot be forgotten at a call site. That makes the
 * hook the thing to fake here, and makes "excludes the signed-in user" the
 * first test: sharing with yourself is a no-op the API silently skips, so
 * offering it only invites the confusion.
 *
 * `useAuth` is a state union, not a bare user. Until it settles there is no
 * answer to "who am I", and the list would have to be drawn and then corrected
 * — a visible flicker that reads as a bug. So the loading case is pinned too.
 *
 * allow-hardcode: every string below is a test fixture — invented colleagues
 * and the sentences this component itself writes. They are the input to an
 * assertion, not a list anything is matched against at runtime.
 */

const auth = vi.hoisted(() => ({ state: { status: "loading" } as AuthState }));
const members = vi.hoisted(() => ({
  state: { status: "loading", members: [] } as MembersState,
}));

vi.mock("../auth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../auth")>()),
  useAuth: () => auth.state,
}));

vi.mock("./members", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./members")>()),
  useMembers: () => members.state,
}));

const MEI = { id: "u-mei", name: "Mei Wong", email: "mei@agency.sg", role: "owner" };
const PRIYA = { id: "u-priya", name: "Priya Nair", email: "priya@agency.sg", role: "recruiter" };
const JO = { id: "u-jo", name: "Jo Tan", email: "jo@agency.sg", role: "recruiter" };

/** A signed-in `Me` with only the fields this component reads filled in
 *  truthfully; the rest is scaffolding the auth type demands. */
function signedInAs(id: string, name: string): AuthState {
  return {
    status: "signed-in",
    me: {
      user: {
        id,
        email: `${name.split(" ")[0].toLowerCase()}@agency.sg`,
        display_name: name,
        preferred_name: null,
        role: "owner",
      },
      tenant: { id: "t-1", name: "Agency", is_personal_account: false },
      mailbox: {
        provider: "microsoft",
        connected: true,
        awaiting_period: false,
        scopes: [],
        status: "active",
        ingestion_active: true,
        ingested: {
          total: 0,
          in_progress: 0,
          awaiting_extraction: 0,
          emails_extracted: 0,
          opportunities: 0,
        },
        oldest_received: null,
        newest_received: null,
        last_activity: null,
      },
    },
  };
}

function ready(...list: { id: string; name: string; email: string; role: string }[]): MembersState {
  return { status: "ready", members: list };
}

function search(label = "Share with"): HTMLInputElement {
  return screen.getByLabelText(label) as HTMLInputElement;
}

beforeEach(() => {
  auth.state = signedInAs(MEI.id, MEI.name);
  members.state = ready(MEI, PRIYA, JO);
});

afterEach(() => {
  cleanup();
});

describe("MemberPicker", () => {
  it("excludes the signed-in user", async () => {
    // Sharing with yourself is a no-op the API silently skips; offering it
    // invites the confusion.
    render(<MemberPicker selected={[]} onChange={() => {}} label="Share with" />);
    fireEvent.focus(search());

    expect(screen.queryByText("Mei Wong")).toBeNull();
    expect(screen.queryByText("Priya Nair")).not.toBeNull();
  });

  it("filters the list as you type", async () => {
    render(<MemberPicker selected={[]} onChange={() => {}} label="Share with" />);
    fireEvent.change(search(), { target: { value: "pri" } });

    expect(screen.queryByText("Priya Nair")).not.toBeNull();
    expect(screen.queryByText("Jo Tan")).toBeNull();
  });

  it("adds a colleague as a chip and reports the id", async () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <MemberPicker selected={[]} onChange={onChange} label="Share with" />,
    );
    fireEvent.focus(search());
    // mousedown, not click: the row commits on the button going down, so that
    // the input's blur cannot unmount it in between.
    fireEvent.mouseDown(screen.getByRole("option", { name: /Priya Nair/ }));

    expect(onChange).toHaveBeenCalledWith([PRIYA.id]);

    rerender(<MemberPicker selected={[PRIYA.id]} onChange={onChange} label="Share with" />);
    expect(screen.getByRole("button", { name: "Remove Priya Nair" })).not.toBeNull();
  });

  it("removes a chip", async () => {
    const onChange = vi.fn();
    render(<MemberPicker selected={[PRIYA.id, JO.id]} onChange={onChange} label="Share with" />);

    fireEvent.click(screen.getByRole("button", { name: "Remove Priya Nair" }));

    expect(onChange).toHaveBeenCalledWith([JO.id]);
  });

  it("excludes anyone named in `exclude`", async () => {
    // Used to hide people a job order is already shared with.
    render(
      <MemberPicker selected={[]} onChange={() => {}} exclude={[JO.id]} label="Share with" />,
    );
    fireEvent.focus(search());

    expect(screen.queryByText("Jo Tan")).toBeNull();
    expect(screen.queryByText("Priya Nair")).not.toBeNull();
  });

  it("shows the owner role beside the name", async () => {
    auth.state = signedInAs(PRIYA.id, PRIYA.name);
    render(<MemberPicker selected={[]} onChange={() => {}} label="Share with" />);
    fireEvent.focus(search());

    const option = screen.getByRole("option", { name: /Mei Wong/ });
    expect(option.textContent).toContain("Owner");
    expect(screen.getByRole("option", { name: /Jo Tan/ }).textContent).not.toContain("Owner");
  });

  it("says so when the list cannot be read", async () => {
    members.state = {
      status: "unreadable",
      members: [],
      message: "Couldn’t load your colleagues. Try again in a moment.",
    };
    render(<MemberPicker selected={[]} onChange={() => {}} label="Share with" />);

    expect(screen.getByRole("status").textContent).toBe(
      "Couldn’t load your colleagues. Try again in a moment.",
    );
    expect(search().disabled).toBe(true);
  });

  it("stays disabled until it knows who is signed in", async () => {
    // Listing yourself for one frame and then removing yourself is a visible
    // flicker that reads as a bug.
    auth.state = { status: "loading" };
    render(<MemberPicker selected={[]} onChange={() => {}} label="Share with" />);

    expect(search().disabled).toBe(true);
    fireEvent.focus(search());
    expect(screen.queryByText("Mei Wong")).toBeNull();
  });

  it("moves through the list with the arrow keys and picks with Enter", async () => {
    const onChange = vi.fn();
    render(<MemberPicker selected={[]} onChange={onChange} label="Share with" />);

    const input = search();
    fireEvent.focus(input);
    // Priya is first, Jo second: down twice lands on Jo.
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(onChange).toHaveBeenCalledWith([JO.id]);
  });

  it("closes the list on Escape without choosing anyone", async () => {
    const onChange = vi.fn();
    render(<MemberPicker selected={[]} onChange={onChange} label="Share with" />);

    const input = search();
    fireEvent.focus(input);
    expect(screen.queryByRole("listbox")).not.toBeNull();

    fireEvent.keyDown(input, { key: "Escape" });

    expect(screen.queryByRole("listbox")).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("MemberSelect", () => {
  it("reports the one id chosen", async () => {
    const onChange = vi.fn();
    render(<MemberSelect value={null} onChange={onChange} label="Assign to" />);

    fireEvent.change(screen.getByLabelText("Assign to"), { target: { value: JO.id } });

    expect(onChange).toHaveBeenCalledWith(JO.id);
  });

  it("offers the signed-in user, and says which row is theirs", async () => {
    // Unlike the picker: assigning to yourself is the commonest case there is.
    // An owner taking on a client could not record it while this excluded
    // them, and a recruiter who already held one was shown through the
    // "someone who has left" branch.
    const onChange = vi.fn();
    render(<MemberSelect value={null} onChange={onChange} label="Assign to" />);

    const mine = screen.queryByRole("option", { name: "Mei Wong · you · Owner" });
    expect(mine).not.toBeNull();
    expect(screen.queryByRole("option", { name: /Priya Nair/ })).not.toBeNull();

    fireEvent.change(screen.getByLabelText("Assign to"), { target: { value: MEI.id } });
    expect(onChange).toHaveBeenCalledWith(MEI.id);
  });

  it("does not call whoever already holds it someone who has left", async () => {
    render(<MemberSelect value={MEI.id} onChange={() => {}} label="Assign to" />);

    expect(screen.queryByRole("option", { name: "Someone who has left" })).toBeNull();
    expect((screen.getByLabelText("Assign to") as HTMLSelectElement).value).toBe(MEI.id);
  });

  it("offers nobody only when asked, and reports null for it", async () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <MemberSelect value={JO.id} onChange={onChange} label="Assign to" />,
    );
    expect(screen.queryByRole("option", { name: "Nobody" })).toBeNull();

    rerender(<MemberSelect value={JO.id} onChange={onChange} allowNone label="Assign to" />);
    expect(screen.queryByRole("option", { name: "Nobody" })).not.toBeNull();

    fireEvent.change(screen.getByLabelText("Assign to"), { target: { value: "" } });
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("stays disabled until it knows who is signed in", async () => {
    auth.state = { status: "loading" };
    render(<MemberSelect value={null} onChange={() => {}} label="Assign to" />);

    expect((screen.getByLabelText("Assign to") as HTMLSelectElement).disabled).toBe(true);
  });

  it("keeps showing whoever is already assigned, even once they have left", async () => {
    // The row names an id the staff list no longer carries. Rendering the
    // select without it would silently reset the assignment to nobody on the
    // next save.
    members.state = ready(MEI, PRIYA);
    render(<MemberSelect value={JO.id} onChange={() => {}} label="Assign to" />);

    expect((screen.getByLabelText("Assign to") as HTMLSelectElement).value).toBe(JO.id);
  });

  it("says so when the list cannot be read", async () => {
    members.state = {
      status: "unreadable",
      members: [],
      message: "Couldn’t load your colleagues. Try again in a moment.",
    };
    render(<MemberSelect value={null} onChange={() => {}} label="Assign to" />);

    expect(screen.getByRole("status").textContent).toBe(
      "Couldn’t load your colleagues. Try again in a moment.",
    );
  });
});
