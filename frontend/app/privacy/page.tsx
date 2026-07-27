import { CONTACT_EMAIL, CONTACT_MAILTO } from "../api";
import { LegalPage, PROVIDER, legalMetadata } from "../legal-page";

export const metadata = legalMetadata(
  "Privacy",
  "What expressautomate.app collects, who processes it, and what has not been built yet.",
);

/**
 * Written against what the system actually does today, not what the plan
 * describes. Ingestion and AI extraction are not built, so this says so plainly
 * rather than describing processing that is not happening. A policy written as
 * though the product were live would be the one thing the product promises not
 * to do.
 */
export default function Page() {
  return (
    <LegalPage
      title="Privacy"
      summary="We are early, and this policy describes what is true today: you can sign in, and
        that is nearly all. No mailbox is being read, because we have not switched mailbox
        ingestion on for anyone. Everything below separates what happens now from what will happen
        when it does."
    >
      <h2>Who we are</h2>
      <p>
        {PROVIDER.name} (UEN {PROVIDER.uen}), of {PROVIDER.address.join(", ")}, a company
        incorporated in Singapore, operates expressautomate.app. We are the organisation
        responsible for the personal data described here. Reach us at{" "}
        <a href={CONTACT_MAILTO}>{CONTACT_EMAIL}</a> about anything on this page, including a
        request to see, correct or delete your data.
      </p>

      <h2>Where we are today</h2>
      <p>
        Mailbox ingestion is not running. No email has been read from any account, no job records
        exist, and no message text has been sent to an AI model. Signing in is built; reading mail
        is not. We say this because a policy describing ingestion in the present tense would
        misrepresent what we hold — which today is your sign-in identity, and nothing more.
      </p>

      <h2>What we collect when you sign in</h2>
      <p>Signing in with Microsoft gives us, from your Microsoft account:</p>
      <ul>
        <li>your email address or user principal name;</li>
        <li>your display name, where Microsoft supplies one;</li>
        <li>
          your Microsoft object id and your organisation&rsquo;s tenant id — identifiers we use as
          the durable key for your account, so that renaming your mailbox address updates your
          record instead of creating a second one;
        </li>
        <li>the time you last signed in;</li>
        <li>
          a refresh token, held encrypted, which is what would later let us read mail on your
          behalf if you connect a mailbox.
        </li>
      </ul>
      <p>
        If you submitted an email address to the early-access form before sign-in existed, we still
        hold that address and the page it came from. Ask us and we will delete it.
      </p>

      <h2>Mailbox data, when you connect one</h2>
      <p>
        Connecting a mailbox is a separate, explicit step, and a separate permission from signing
        in. We request <strong>Mail.Read</strong> only. We cannot send, modify or delete your
        email; that permission is never requested and never granted. When ingestion is switched on:
      </p>
      <ul>
        <li>
          we read messages from the connected mailbox and store them, including sender, recipients,
          subject, body and attachments;
        </li>
        <li>
          we send message text to an AI model to extract recruitment details — vacancies, companies,
          requirements — and store what it extracts alongside the evidence it drew on;
        </li>
        <li>
          we never discard the original email. Every extracted field keeps the wording it came from,
          so any record can be traced back to its source, and so an extraction can be checked rather
          than trusted.
        </li>
      </ul>
      <p>
        Mail in a recruitment mailbox contains other people&rsquo;s personal data — candidates,
        clients, colleagues. Where we process that, we do so on your instructions and on your
        organisation&rsquo;s behalf. You remain responsible for having a lawful basis to hold it and
        to have it processed by us.
      </p>

      <h2>Who else processes it</h2>
      <p>We do not sell personal data, and we do not use it to train AI models. We rely on:</p>
      <ul>
        <li>
          <strong>Microsoft</strong> — identity, and the Graph API your mail would be read through.
        </li>
        <li>
          <strong>Koyeb</strong> — hosting for the application and the managed PostgreSQL database
          where your data is stored.
        </li>
        <li>
          <strong>OpenRouter</strong> — routing message text to the AI models that perform
          extraction. Relevant only once ingestion is running. OpenRouter is a router, so the model
          provider it forwards a request to also receives that text. We would rather name the
          arrangement than let &ldquo;we use OpenRouter&rdquo; imply the text stops there.
        </li>
        <li>
          <strong>Cloudflare</strong> — DNS for expressautomate.app.
        </li>
      </ul>
      <p>
        If you need to know the specific region our database runs in for a compliance review, ask
        and we will tell you rather than leave you to infer it.
      </p>

      <h2>How your data is kept apart, and how it is protected</h2>
      <p>
        Every business record carries the identity of the agency it belongs to, and that separation
        is enforced in the database itself through row-level security — not only in application
        code, where one missed condition would be enough to leak. One agency cannot read
        another&rsquo;s records.
      </p>
      <p>
        Refresh tokens are encrypted at rest with a key held outside the database, and traffic to
        and from the service is encrypted in transit. To be precise rather than reassuring: other
        stored fields, such as your name and email address, are protected by the database&rsquo;s
        access controls and our host&rsquo;s disk encryption, not by application-level encryption of
        each field.
      </p>

      <h2>Cookies</h2>
      <p>
        Every cookie we set is functional: one recording that you are signed in, and short-lived
        ones that hold a sign-in in progress and are discarded when it completes. None is readable
        by scripts in your browser and none is shared with anyone. There is no advertising or
        analytics cookie on this site, which is why you have not been shown a consent banner — we
        have nothing to ask you to consent to.
      </p>

      <h2>How long we keep it</h2>
      <p>
        Retention periods for mail and extracted records are not set yet, and we would rather say so
        than publish a period we have not decided. We will publish them here before ingestion is
        switched on for anyone. Account data is kept while your account exists; ask us to delete it
        and we will.
      </p>

      <h2>Your choices</h2>
      <ul>
        <li>
          <strong>Disconnect at any time.</strong> Revoke our access from your Microsoft account and
          we can no longer read anything.
        </li>
        <li>
          <strong>Ask what we hold.</strong> Under Singapore&rsquo;s Personal Data Protection Act
          you may ask for access to your personal data and for it to be corrected. If you are in the
          UK or EU, you may also have rights to erasure, portability and objection. Email us and we
          will act on it — we will not require a particular form of words.
        </li>
        <li>
          <strong>Complain.</strong> If we have not resolved something, you may complain to
          Singapore&rsquo;s Personal Data Protection Commission, or to your local supervisory
          authority.
        </li>
      </ul>

      <h2>Changes</h2>
      <p>
        When this policy changes materially we will change the date at the top and tell account
        holders by email. We will not change it quietly and rely on your having re-read it.
      </p>
    </LegalPage>
  );
}
