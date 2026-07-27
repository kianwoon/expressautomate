import { StubPage, stubMetadata } from "../stub-page";

export const metadata = stubMetadata("Pricing");

export default function Page() {
  return (
    <StubPage
      title="Pricing"
      blurb="Pricing is not set yet. We are still working directly with a small number of early
        agencies to get the product right, and a price without that experience behind it would be
        a guess."
      next={[
        { href: "/#what", label: "See what you can do" },
        { href: "mailto:hello@expressautomate.app", label: "Email us" },
        { href: "/", label: "Back to the home page" },
      ]}
    />
  );
}
