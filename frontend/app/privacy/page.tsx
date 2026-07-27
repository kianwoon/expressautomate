import { StubPage, stubMetadata } from "../stub-page";

export const metadata = stubMetadata("Privacy");

export default function Page() {
  return (
    <StubPage
      title="Privacy"
      blurb="Our privacy policy is not published yet. Nothing on this page is a substitute for it, or
        any part of it. In the meantime, read how we handle mailbox access and data, or email us
        with questions."
      next={[
        { href: "/#security", label: "Read about security" },
        { href: "mailto:hello@expressautomate.app", label: "Email us" },
        { href: "/", label: "Back to the home page" },
      ]}
    />
  );
}
