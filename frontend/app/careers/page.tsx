import { StubPage, stubMetadata } from "../stub-page";

export const metadata = stubMetadata("Careers");

export default function Page() {
  return (
    <StubPage
      title="Careers"
      blurb="There is nothing posted here yet. If you would like to reach out anyway, email us and
        tell us a bit about yourself."
      next={[
        { href: "mailto:hello@expressautomate.app", label: "Email us" },
        { href: "/", label: "Back to the home page" },
      ]}
    />
  );
}
