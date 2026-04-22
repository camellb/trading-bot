"use client";

import React, { useState, useEffect, useActionState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  signIn,
  signUp,
  requestPasswordReset,
  signInWithGoogle,
  type AuthState,
} from "./actions";
import "../styles/auth.css";

const INITIAL: AuthState = {};

type Mode = "login" | "signup" | "forgot" | "sent";
const MODES: Mode[] = ["login", "signup", "forgot", "sent"];

function readHash(): Mode {
  if (typeof window === "undefined") return "signup";
  const h = (window.location.hash || "").replace("#", "") as Mode;
  return MODES.includes(h) ? h : "signup";
}

export default function AuthPage() {
  const [mode, setMode] = useState<Mode>("signup");

  useEffect(() => {
    setMode(readHash());
    const onHash = () => setMode(readHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const setModeAndHash = (m: Mode) => {
    setMode(m);
    if (typeof window !== "undefined" && window.location.hash !== "#" + m) {
      history.replaceState(null, "", "#" + m);
    }
  };

  const tweaks = { showGoogle: true, showReferral: true, oracleBackdrop: true };

  return (
    <div className="auth-page" data-screen-label="Auth">
      {tweaks.oracleBackdrop && (
        <div className="auth-backdrop">
          <img src="/brand/oracle-hero.jpg" alt="" />
          <div className="auth-backdrop-veil" />
        </div>
      )}
      <Link href="/" className="auth-back">← Back to home</Link>

      <div className="auth-shell">
        <AuthContext mode={mode} />
        <AuthForm mode={mode} setMode={setModeAndHash} tweaks={tweaks} />
      </div>
    </div>
  );
}

function AuthPress() {
  const names = ["Bloomberg", "TechCrunch", "CoinDesk", "Wired"];
  return (
    <div className="auth-press">
      <div className="auth-press-label">As Seen In</div>
      <div className="auth-press-row">
        {names.map((n) => <span className="auth-press-item" key={n}>{n}</span>)}
      </div>
    </div>
  );
}

function AuthContext({ mode }: { mode: Mode }) {
  return (
    <aside className="auth-context">
      <Link href="/" className="auth-brand">
        <img src="/brand/mark.svg" alt="" className="wordmark-mark" />
        <span className="wordmark-text">DELFI</span>
      </Link>

      <div className="auth-context-body">
        <h1 className="auth-context-head balanced">
          {mode === "signup" ? (
            <>Your autonomous<br />Polymarket agent<br />is waiting.</>
          ) : mode === "forgot" || mode === "sent" ? (
            <>Password reset.<br />We&apos;ll get you back<br />in.</>
          ) : (
            <>Welcome back.<br />The markets<br />don&apos;t sleep.</>
          )}
        </h1>
        <p className="auth-context-sub">
          {mode === "signup"
            ? "Sign up in under three minutes and join 11,500 traders who already trade with Delfi."
            : mode === "forgot"
            ? "Enter the email tied to your account. We'll send a secure link to reset your password. The link is valid for 30 minutes."
            : mode === "sent"
            ? "Check your inbox. We've sent a reset link to the email on file. If it doesn't arrive within a couple of minutes, check spam or request another."
            : "Delfi has been working while you were away. Sign in to review overnight activity, read the morning summary, and adjust risk parameters."}
        </p>

        <div className="auth-context-stats">
          <div className="auth-stat">
            <div className="auth-stat-num gold t-num">34,788</div>
            <div className="auth-stat-label">Predictions resolved</div>
          </div>
          <div className="auth-stat">
            <div className="auth-stat-num vellum t-num">0.087</div>
            <div className="auth-stat-label">30-day Brier score</div>
          </div>
          <div className="auth-stat">
            <div className="auth-stat-num teal t-num">99.2%</div>
            <div className="auth-stat-label">Uptime</div>
          </div>
        </div>

        <ul className="auth-context-points">
          <li><span className="auth-point-dot"></span>Free to start. Works in simulation mode until you&apos;re ready.</li>
          <li><span className="auth-point-dot"></span>Every trade comes with full reasoning.</li>
          <li><span className="auth-point-dot"></span>Connect Telegram and get summaries wherever you are.</li>
        </ul>

        <AuthPress />
      </div>

      <div className="auth-context-foot">
        <span>© 2026 Delfi</span>
        <Link href="/legal/terms">Terms</Link>
        <Link href="/legal/privacy">Privacy</Link>
        <Link href="/legal/risk">Risk</Link>
      </div>
    </aside>
  );
}

type Tweaks = { showGoogle: boolean; showReferral: boolean; oracleBackdrop: boolean };

function AuthForm({ mode, setMode, tweaks }: { mode: Mode; setMode: (m: Mode) => void; tweaks: Tweaks }) {
  const searchParams = useSearchParams();
  const redirectTo = searchParams.get("redirect") ?? "/dashboard";
  const urlError = searchParams.get("error");

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [referral, setReferral] = useState("");
  const [tosOk, setTosOk] = useState(false);
  const [riskOk, setRiskOk] = useState(false);
  const [showPw, setShowPw] = useState(false);

  const isSignup = mode === "signup";
  const isForgot = mode === "forgot";
  const isSent = mode === "sent";
  const canSubmit = !!email && !!password && (!isSignup || (tosOk && riskOk));

  const [signInState, signInAction, signInPending] = useActionState(signIn, INITIAL);
  const [signUpState, signUpAction, signUpPending] = useActionState(signUp, INITIAL);
  const [resetState, resetAction, resetPending] = useActionState(requestPasswordReset, INITIAL);

  useEffect(() => {
    if (resetState.ok) setMode("sent");
  }, [resetState.ok, setMode]);

  const pending = signInPending || signUpPending;
  const submitError =
    urlError ??
    (isSignup ? signUpState.error : signInState.error) ??
    null;
  const forgotError = resetState.error ?? null;

  if (isForgot || isSent) {
    return (
      <section className="auth-form-panel">
        <div className="auth-form-wrap">
          <button type="button" className="auth-back-inline" onClick={() => setMode("login")}>
            ← Back to sign in
          </button>

          <div className="auth-form-head">
            <h2 className="auth-form-title">
              {isSent ? "Check your email" : "Reset your password"}
            </h2>
            <p className="auth-form-sub">
              {isSent ? (
                <>We sent a reset link to <strong>{email || "your email"}</strong>. It&apos;s valid for 30 minutes.</>
              ) : (
                <>Enter your email and we&rsquo;ll send you a secure reset link.</>
              )}
            </p>
          </div>

          {isSent ? (
            <div className="auth-form">
              <div className="auth-sent-card">
                <div className="auth-sent-icon" aria-hidden="true">
                  <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" strokeWidth="1.6">
                    <rect x="3" y="5" width="18" height="14" rx="2" />
                    <path d="M3 7l9 7 9-7" />
                  </svg>
                </div>
                <div className="auth-sent-body">
                  <div className="auth-sent-title">Reset link sent</div>
                  <div className="auth-sent-meta">Delivered to {email || "your inbox"} · Expires in 30 min</div>
                </div>
              </div>

              <button type="button" className="auth-submit" onClick={() => { window.location.href = "mailto:"; }}>
                Open email app
              </button>

              <p className="auth-switch">
                Didn&apos;t get it? <a href="#" onClick={(e) => { e.preventDefault(); setMode("forgot"); }}>Try again</a>.
              </p>

              <ul className="auth-help-list">
                <li>Links can take up to a minute to arrive.</li>
                <li>Check your spam or promotions folder.</li>
                <li>Still stuck? <a href="mailto:support@delfibot.com">support@delfibot.com</a></li>
              </ul>
            </div>
          ) : (
            <form action={resetAction} className="auth-form" noValidate>
              <Field label="Email" htmlFor="auth-forgot-email">
                <input
                  id="auth-forgot-email"
                  name="email"
                  type="email"
                  autoComplete="email"
                  placeholder="you@domain.com"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
                  autoFocus
                />
              </Field>

              {forgotError && <div className="auth-error" role="alert">{forgotError}</div>}

              <button type="submit" className="auth-submit" disabled={!email || resetPending}>
                {resetPending ? "Sending…" : "Send reset link"}
              </button>

              <p className="auth-switch">
                Remembered it? <a href="#" onClick={(e) => { e.preventDefault(); setMode("login"); }}>Sign in</a>
              </p>
            </form>
          )}
        </div>
      </section>
    );
  }

  return (
    <section className="auth-form-panel">
      <div className="auth-form-wrap">
        <div className="auth-tabs" role="tablist">
          <button
            role="tab"
            aria-selected={!isSignup}
            className={`auth-tab ${!isSignup ? "on" : ""}`}
            onClick={() => setMode("login")}
          >Log in</button>
          <button
            role="tab"
            aria-selected={isSignup}
            className={`auth-tab ${isSignup ? "on" : ""}`}
            onClick={() => setMode("signup")}
          >Sign up</button>
          <span className={`auth-tab-indicator ${isSignup ? "right" : "left"}`} aria-hidden="true" />
        </div>

        <div className="auth-form-head">
          <h2 className="auth-form-title">
            {isSignup ? "Create your account" : "Welcome back"}
          </h2>
          <p className="auth-form-sub">
            {isSignup
              ? "Free to register. No Polymarket account required."
              : "Sign in to your Delfi dashboard."}
          </p>
        </div>

        {tweaks.showGoogle && (
          <>
            <div className="auth-oauth">
              <form action={signInWithGoogle} style={{ width: "100%" }}>
                <input type="hidden" name="redirect" value={redirectTo} />
                <button type="submit" className="auth-oauth-btn">
                  <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
                    <path fill="#EA4335" d="M12 10.2v3.9h5.5c-.24 1.4-1.7 4.1-5.5 4.1-3.3 0-6-2.7-6-6.2S8.7 5.8 12 5.8c1.9 0 3.1.8 3.9 1.5l2.7-2.6C16.9 3.2 14.7 2.3 12 2.3c-5.5 0-10 4.5-10 10s4.5 10 10 10c5.8 0 9.6-4.1 9.6-9.8 0-.7-.1-1.2-.2-1.7H12z" />
                  </svg>
                  <span>Continue with Google</span>
                </button>
              </form>
            </div>
            <div className="auth-divider">
              <span>or continue with email</span>
            </div>
          </>
        )}

        <form
          action={isSignup ? signUpAction : signInAction}
          className="auth-form"
          noValidate
        >
          <input type="hidden" name="redirect" value={redirectTo} />
          <Field label="Email" htmlFor="auth-email">
            <input
              id="auth-email"
              name="email"
              type="email"
              autoComplete="email"
              placeholder="you@domain.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </Field>

          <Field
            label="Password"
            htmlFor="auth-password"
            right={!isSignup && (
              <a href="#" className="auth-forgot" onClick={(e) => { e.preventDefault(); setMode("forgot"); }}>Forgot?</a>
            )}
          >
            <div className="auth-pw">
              <input
                id="auth-password"
                name="password"
                type={showPw ? "text" : "password"}
                autoComplete={isSignup ? "new-password" : "current-password"}
                placeholder={isSignup ? "At least 8 characters" : "••••••••"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                minLength={isSignup ? 8 : undefined}
              />
              <button
                type="button"
                className="auth-pw-toggle"
                onClick={() => setShowPw((s) => !s)}
                aria-label={showPw ? "Hide password" : "Show password"}
              >{showPw ? "Hide" : "Show"}</button>
            </div>
          </Field>

          {isSignup && tweaks.showReferral && (
            <Field label="Referral or promo code" htmlFor="auth-ref" optional>
              <input
                id="auth-ref"
                name="referral"
                type="text"
                placeholder="Optional"
                value={referral}
                onChange={(e) => setReferral(e.target.value)}
              />
            </Field>
          )}

          {isSignup && (
            <div className="auth-legal">
              <label className="auth-check">
                <input type="checkbox" checked={tosOk} onChange={(e) => setTosOk(e.target.checked)} required />
                <span className="auth-check-box" aria-hidden="true"></span>
                <span className="auth-check-text">
                  I agree to Delfi&apos;s <Link href="/legal/terms">Terms of Service</Link> and <Link href="/legal/privacy">Privacy Policy</Link>.
                </span>
              </label>
              <label className="auth-check">
                <input type="checkbox" checked={riskOk} onChange={(e) => setRiskOk(e.target.checked)} required />
                <span className="auth-check-box" aria-hidden="true"></span>
                <span className="auth-check-text">
                  I understand that prediction market trading involves real financial risk, and I&apos;ve reviewed the <Link href="/legal/risk">Risk Disclosure</Link>.
                </span>
              </label>
            </div>
          )}

          {submitError && <div className="auth-error" role="alert">{submitError}</div>}
          {isSignup && signUpState.ok && !submitError && (
            <div className="auth-notice" role="status">
              Check your email for a confirmation link before signing in.
            </div>
          )}

          <button type="submit" className="auth-submit" disabled={!canSubmit || pending}>
            {pending ? (isSignup ? "Creating account…" : "Signing in…") : isSignup ? "Create account" : "Sign in"}
          </button>

          <p className="auth-switch">
            {isSignup ? (
              <>Already have an account? <a href="#" onClick={(e) => { e.preventDefault(); setMode("login"); }}>Log in</a></>
            ) : (
              <>New to Delfi? <a href="#" onClick={(e) => { e.preventDefault(); setMode("signup"); }}>Create an account</a></>
            )}
          </p>
        </form>
      </div>
    </section>
  );
}

function Field({
  label,
  htmlFor,
  children,
  right,
  optional,
}: {
  label: string;
  htmlFor: string;
  children: React.ReactNode;
  right?: React.ReactNode;
  optional?: boolean;
}) {
  return (
    <div className="auth-field">
      <div className="auth-field-head">
        <label htmlFor={htmlFor}>
          {label}
          {optional && <span className="auth-field-opt"> (optional)</span>}
        </label>
        {right}
      </div>
      {children}
    </div>
  );
}
