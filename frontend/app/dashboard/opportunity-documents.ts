"use client";

import {
  OPPORTUNITIES_DOCUMENTS_PATH,
  opportunityDocumentDownloadPath,
  opportunityDocumentPath,
} from "../api";
import { readError, ApiError } from "./candidates";

/**
 * The New job order dialog's document upload half.
 *
 * Split out of `job-order-form.tsx` so the form stays about typing a vacancy
 * and this file stays about bytes: upload a job-description PDF/Word file,
 * poll the extraction, read the prefill back, download the original, remove
 * it. Same shapes as the candidate-document helpers, so the two upload paths
 * read alike.
 */

export type OpportunityDocumentExtractState =
  | "pending"
  | "extracting"
  | "extracted"
  | "unreadable"
  | "failed";

export type OpportunityDocument = {
  id: string;
  filename: string;
  content_type: string;
  byte_size: number;
  extract_state: OpportunityDocumentExtractState;
  extract_error: string | null;
  /** The extracted values, keyed by the form's own field names. Absent until
   *  the worker finishes; null per field when the document never mentioned it. */
  prefill: Record<string, string | null> | null;
  created_at: string | null;
};

/** Uploads a job-description file with no job order yet. 201: the file is
 *  stored and queued for reading, which is why the document comes back in a
 *  `pending` state rather than with values attached. */
export async function uploadOpportunityDocument(
  file: File,
): Promise<OpportunityDocument> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(OPPORTUNITIES_DOCUMENTS_PATH, {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
    body: form,
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as OpportunityDocument;
}

/** Polls the extraction: state plus, once `extracted`, the values the form
 *  pre-fills from. A 404 means the file was removed or never existed. */
export async function getOpportunityDocument(
  documentId: string,
): Promise<OpportunityDocument> {
  const res = await fetch(opportunityDocumentPath(documentId), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res), res.status);
  return (await res.json()) as OpportunityDocument;
}

export type DocumentUrl = { url: string; expires_in: number };

/** A short-lived presigned URL for the original file. Fetched at the moment
 *  the recruiter asks for it and never held: the link stops working within
 *  minutes, so anything longer-lived than the click is a broken link waiting
 *  to be found. */
export async function getOpportunityDocumentUrl(
  documentId: string,
): Promise<DocumentUrl> {
  const res = await fetch(opportunityDocumentDownloadPath(documentId), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as DocumentUrl;
}

/** Removes the file: the object in R2 first, then the row. */
export async function deleteOpportunityDocument(
  documentId: string,
): Promise<void> {
  const res = await fetch(opportunityDocumentPath(documentId), {
    method: "DELETE",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}
