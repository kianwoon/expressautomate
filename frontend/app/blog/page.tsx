import { StubPage, stubMetadata } from "../stub-page";

export const metadata = stubMetadata("Blog");

export default function Page() {
  return (
    <StubPage
      title="Blog"
      blurb="There is nothing published here yet. We are early — setting out to work with a small number of
        agencies and building the product with them — and would rather post nothing than pad this
        space out."
      next={[
        { href: "/#how", label: "See how it works" },
        { href: "/", label: "Back to the home page" },
      ]}
    />
  );
}
