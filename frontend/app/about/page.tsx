import { CONTACT_MAILTO } from "../api";
import { StubPage, stubMetadata } from "../stub-page";

export const metadata = stubMetadata("About us");

export default function Page() {
  return (
    <StubPage
      title="About us"
      blurb="This page is not written yet. expressautomate.app is an AI recruitment intelligence and
        operations platform for small Singapore recruitment agencies on Microsoft 365, and it is
        early — the team has not yet taken on its first agencies."
      next={[
        { href: "/#security", label: "Read about security" },
        { href: CONTACT_MAILTO, label: "Email us" },
        { href: "/", label: "Back to the home page" },
      ]}
    />
  );
}
