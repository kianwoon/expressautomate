"use client";

import { useEffect, useRef, type ReactNode } from "react";

/**
 * The one dialog in this codebase.
 *
 * It started as the WhatsApp draft's own overlay, with a comment saying no
 * dialog primitive existed anywhere else. The candidate form became the
 * second caller, and a second private copy is how two dialogs end up trapping
 * focus differently and only one of them closing on Escape.
 *
 * Not `<dialog showModal()>`, which would give the focus trap and the inert
 * background for free: the existing overlay is a positioned `div` that the
 * WhatsApp panel already renders inside a normal React tree, and swapping the
 * element out would move that panel's styling and stacking onto the top layer
 * in the same change that generalises it. Worth doing; not worth doing blind.
 */
export function Dialog({
  title,
  titleId,
  onClose,
  className,
  children,
}: {
  /** Rendered as the heading, and named by `aria-labelledby`. */
  title: ReactNode;
  /** Unique per open dialog — two dialogs sharing an id label each other. */
  titleId: string;
  onClose: () => void;
  /** Extra classes on the card, for callers that size it themselves. */
  className?: string;
  children: ReactNode;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  // Captured before the dialog takes focus, so closing can hand it back to
  // whatever opened it. Without this, dismissing drops the caret at the top of
  // the document and a keyboard user starts the page again.
  const openerRef = useRef<Element | null>(null);

  useEffect(() => {
    openerRef.current = document.activeElement;
    headingRef.current?.focus();
    // Captured now rather than read in the cleanup: by the time cleanup runs
    // the ref may already be detached, and `dialogRef.current?.contains(...)`
    // would then be false for a focus that really is still inside — so focus
    // would silently never go back to whatever opened the dialog. The node
    // object itself stays valid.
    const node = dialogRef.current;

    // The list behind stays put while the dialog is open. Restoring the exact
    // previous value rather than clearing it: another overlay may already own
    // this, and clearing would silently release its lock too.
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;

      // Focus stays inside while the dialog is up. `<dialog>` would do this
      // itself; until this is one, Tab off either end wraps by hand.
      const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      if (!focusable || focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;

      if (event.shiftKey && (active === first || active === headingRef.current)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      // Only if focus is still somewhere in here. A caller that moved focus
      // deliberately on close — to a row it just saved, say — should keep it.
      if (node?.contains(document.activeElement)) {
        (openerRef.current as HTMLElement | null)?.focus?.();
      }
    };
  }, [onClose]);

  return (
    <div
      className="dlg-overlay"
      // `mousedown`, not `click`: a click fires on the element the button is
      // released over, so a drag that starts inside the card and ends on the
      // backdrop — selecting text to the edge, which a long form invites —
      // would otherwise discard what the recruiter just typed.
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        ref={dialogRef}
        className={`card dlg-modal${className ? ` ${className}` : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <h3 id={titleId} ref={headingRef} tabIndex={-1} className="jo-detail-title dlg-title">
          {title}
        </h3>
        {children}
      </div>
    </div>
  );
}
