import { StubPage, stubMetadata } from "../stub-page";

export const metadata = stubMetadata("Product updates");

export default function Page() {
  return (
    <StubPage
      title="Product updates"
      blurb="There is nothing posted here yet. The product is early — ingestion today reads Outlook
        mail read-only and turns it into structured job records — and this page will start
        recording changes once there is a real history to show."
      next={[
        { href: "/#what", label: "See what you can do" },
        { href: "/", label: "Back to the home page" },
      ]}
    />
  );
}
