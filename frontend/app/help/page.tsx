import { CONTACT_MAILTO } from "../api";
import { StubPage, stubMetadata } from "../stub-page";

export const metadata = stubMetadata("Help centre");

export default function Page() {
  return (
    <StubPage
      title="Help centre"
      blurb="There is no help centre here yet — the team is small and not yet taken on its first
        agencies. Email us and a person will answer."
      next={[
        { href: CONTACT_MAILTO, label: "Email us" },
        { href: "/#how", label: "See how it works" },
        { href: "/", label: "Back to the home page" },
      ]}
    />
  );
}
