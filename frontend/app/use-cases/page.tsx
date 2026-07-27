import { StubPage, stubMetadata } from "../stub-page";

export const metadata = stubMetadata("Use cases");

export default function Page() {
  return (
    <StubPage
      title="Use cases"
      blurb="This page is not written yet. We are onboarding a small number of Singapore recruitment
        agencies and building it with them, so we would rather wait and describe real use rather
        than guess at it."
      next={[
        { href: "/#how", label: "See how it works" },
        { href: "/#what", label: "See what you can do" },
        { href: "/", label: "Back to the home page" },
      ]}
    />
  );
}
