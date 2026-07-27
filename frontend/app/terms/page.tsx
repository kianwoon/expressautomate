import { StubPage, stubMetadata } from "../stub-page";

export const metadata = stubMetadata("Terms");

export default function Page() {
  return (
    <StubPage
      title="Terms"
      blurb="Our terms of service are not published yet. Nothing on this page is a substitute for
        them, or any part of them. In the meantime, read how we handle mailbox access and data, or
        email us with questions."
      next={[
        { href: "/#security", label: "Read about security" },
        { href: "mailto:hello@expressautomate.app", label: "Email us" },
        { href: "/", label: "Back to the home page" },
      ]}
    />
  );
}
