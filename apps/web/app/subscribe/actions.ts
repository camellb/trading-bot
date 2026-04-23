"use server";

import { redirect } from "next/navigation";

import { createClient } from "@/lib/supabase/server";

export type SubscriptionPlan = "monthly" | "annual";

function isPlan(value: unknown): value is SubscriptionPlan {
  return value === "monthly" || value === "annual";
}

// Paper-pay checkout. Marks the user's subscription as active without any
// payment. This exists so the full register -> subscribe -> onboarding flow
// can be built and tested before Stripe is wired up.
//
// TODO(stripe): replace this with a real Stripe Checkout session. The shape:
//   1. create or reuse a Stripe customer for this user.id
//   2. stripe.checkout.sessions.create({ mode: 'subscription', price: <priceId> })
//   3. redirect(session.url)
//   4. a /api/stripe/webhook handler listens for checkout.session.completed
//      and flips subscription_status='active' there, not here.
//
// When that happens, this file stops writing to user_config directly and
// becomes a thin "create session + redirect" function.
export async function startCheckout(formData: FormData) {
  const rawPlan = String(formData.get("plan") ?? "");
  if (!isPlan(rawPlan)) {
    redirect("/subscribe?error=invalid_plan");
  }
  const plan: SubscriptionPlan = rawPlan;

  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) redirect("/auth#login");

  const { error } = await supabase
    .from("user_config")
    .upsert(
      {
        user_id: user.id,
        subscription_status: "active",
        subscription_plan: plan,
        subscription_started_at: new Date().toISOString(),
      },
      { onConflict: "user_id" },
    );

  if (error) {
    console.error("[subscribe] upsert user_config failed", {
      userId: user.id,
      code: error.code,
      message: error.message,
      details: error.details,
      hint: error.hint,
    });
    const params = new URLSearchParams({
      error: "save_failed",
      code: error.code ?? "",
      message: error.message ?? "",
    });
    redirect(`/subscribe?${params.toString()}`);
  }

  const { data: cfg } = await supabase
    .from("user_config")
    .select("onboarded_at")
    .eq("user_id", user.id)
    .maybeSingle();

  redirect(cfg?.onboarded_at ? "/dashboard" : "/onboarding");
}
