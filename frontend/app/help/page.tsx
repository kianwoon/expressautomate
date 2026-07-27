import { StubPage, stubMetadata } from "../stub-page";

export const metadata = stubMetadata("Help centre");

export default function Page() {
  return (
    <StubPage
      title="Help centre"
      blurb="There is no help centre here yet — the team is small and still onboarding its first
        agencies directly. Email us and a person will answer."
      next={[
        { href: "mailto:hello@expressautomate.app", label: "Email us" },
        { href: "/#how", label: "See how it works" },
        { href: "/", label: "Back to the home page" },
      ]}
    />
  );
}
