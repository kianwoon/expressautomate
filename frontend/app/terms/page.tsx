import { CONTACT_EMAIL, CONTACT_MAILTO } from "../api";
import { LegalPage, PROVIDER, legalMetadata } from "../legal-page";

export const metadata = legalMetadata(
  "Terms",
  "The terms on which expressautomate.app is provided, while it is still early access.",
);

/**
 * Scoped to what is actually offered: early access, no fee, no availability
 * promise. Terms that described a mature paid service would be unenforceable
 * where they overreach and misleading everywhere else.
 */
export default function Page() {
  return (
    <LegalPage
      title="Terms"
      summary="expressautomate.app is early access. It is provided free, without an availability
        promise, and parts of it described on this site are not built yet. These terms say what you
        can expect from us and what we need from you — in particular, that you have the authority
        to let us read the mailbox you connect."
    >
      <h2>Who you are agreeing with</h2>
      <p>
        These terms are between you, and {PROVIDER.name} of {PROVIDER.address.join(", ")}. They
        apply when you sign in to or use expressautomate.app. If you are using it for an agency, you
        confirm you may accept these terms on that agency&rsquo;s behalf.
      </p>

      <h2>What the service is right now</h2>
      <p>
        Early access, and incomplete. Sign-in works. Mailbox ingestion and AI extraction are
        described on this site but are not switched on, so today the service does not read mail or
        produce records. We will not pretend otherwise to make the product look further along than
        it is.
      </p>

      <h2>Connecting a mailbox</h2>
      <p>
        Connecting a mailbox is a separate decision from signing in, and you make it. When you do,
        you confirm that you are entitled to grant access to that mailbox, and that letting us
        process the personal data in it — including candidates&rsquo; and clients&rsquo; — is
        lawful. If your organisation requires an administrator&rsquo;s approval, that approval is
        yours to obtain, not ours to work around.
      </p>
      <p>
        We request read-only access. We cannot send, modify or delete your mail. You may revoke our
        access from your Microsoft account at any time, without asking us.
      </p>

      <h2>What you may not do</h2>
      <ul>
        <li>
          use the service for anything unlawful, or to process data you have no right to process;
        </li>
        <li>
          attempt to reach another agency&rsquo;s data, probe for a way to, or interfere with the
          service&rsquo;s operation;
        </li>
        <li>resell or provide the service to others as your own.</li>
      </ul>

      <h2>What the AI produces, and what it is not</h2>
      <p>
        Extraction is performed by AI models and will sometimes be wrong. The system is built to
        return &ldquo;Not mentioned&rdquo; rather than invent a value it cannot find, and to keep
        every extracted field traceable to the wording it came from — but that makes errors
        checkable, not absent.
      </p>
      <p>
        So: what the service produces is a draft for a person to review. It is not advice, and it
        is not a basis on which to make a hiring, rejection or shortlisting decision on its own. You
        remain responsible for decisions you take about people.
      </p>

      <h2>Your data stays yours</h2>
      <p>
        Your email and the records extracted from it belong to you. We process them to provide the
        service to you, as described in our <a href="/privacy">privacy policy</a>. We do not sell
        them, and we do not use them to train AI models. Ask us for a copy or a deletion and we will
        do it.
      </p>

      <h2>Availability, changes and cost</h2>
      <p>
        There is no service level and no uptime promise. We may change, suspend or discontinue any
        part of the service while it is in early access, and features on this site may be altered or
        dropped before release. We will give account holders reasonable notice of a change that
        removes something they rely on.
      </p>
      <p>
        The service is free today. If we begin charging, we will tell you before it applies to you,
        and you will be free to stop using it instead.
      </p>

      <h2>Ending it</h2>
      <p>
        You may stop at any time — revoke access, and email us to have your account and data
        deleted. We may suspend or end access if these terms are breached in a way that puts other
        agencies&rsquo; data or the service at risk. If we end your access for any other reason, we
        will give you a fair opportunity to export your data first.
      </p>

      <h2>Liability</h2>
      <p>
        The service is provided as it is, without warranties beyond those the law does not allow us
        to exclude. Nothing here limits liability for death or personal injury caused by negligence,
        for fraud, or for anything else that cannot lawfully be limited. Subject to that, and given
        the service is currently provided free of charge, our aggregate liability to you is limited
        to SGD 100. We are not liable for loss of profit, business or goodwill, or for decisions
        taken on the strength of AI output that was not reviewed by a person.
      </p>

      <h2>Governing law</h2>
      <p>
        These terms are governed by the laws of Singapore, and the courts of Singapore have
        exclusive jurisdiction over any dispute arising from them.
      </p>

      <h2>Changes to these terms</h2>
      <p>
        When these terms change materially we will change the date at the top and tell account
        holders by email. If you do not accept a change, stop using the service and ask us to delete
        your data.
      </p>

      <h2>Contact</h2>
      <p>
        Questions about these terms go to <a href={CONTACT_MAILTO}>{CONTACT_EMAIL}</a>, and we will
        answer them.
      </p>
    </LegalPage>
  );
}
