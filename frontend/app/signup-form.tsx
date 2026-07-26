"use client";

import { useState } from "react";

import { EARLY_ACCESS_PATH } from "./api";

type State = "idle" | "sending" | "ok" | "error";

export function SignupForm() {
  const [email, setEmail] = useState("");
  const [state, setState] = useState<State>("idle");
  const [message, setMessage] = useState("");

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!email.includes("@") || email.startsWith("@")) {
      setState("error");
      setMessage("Please enter a valid work email.");
      return;
    }
    setState("sending");
    setMessage("Sending…");
    try {
      const res = await fetch(EARLY_ACCESS_PATH, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      if (!res.ok) throw new Error(String(res.status));
      setEmail("");
      setState("ok");
      setMessage("Thank you — we will be in touch.");
    } catch {
      // Say plainly that it failed rather than showing a false success.
      setState("error");
      setMessage("Could not reach the server. Please try again shortly.");
    }
  }

  return (
    <>
      <form className="signup" onSubmit={onSubmit} noValidate>
        <input
          className="input"
          type="email"
          name="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="you@agency.com"
          aria-label="Work email"
          required
        />
        <button className="btn btn-primary" type="submit" disabled={state === "sending"}>
          {state === "sending" ? "Sending…" : "Request access"}
        </button>
      </form>
      <p
        className="note"
        role="status"
        aria-live="polite"
        data-state={state === "ok" ? "ok" : state === "error" ? "error" : undefined}
      >
        {message}
      </p>
    </>
  );
}
