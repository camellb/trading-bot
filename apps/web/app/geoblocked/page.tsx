import Link from "next/link";

import { countryForCode } from "@/lib/geoblock/countries";

import "../styles/content.css";

export const metadata = { title: "Not available in your region - Delfi" };
export const dynamic = "force-dynamic";

type SearchParams = Promise<{ cc?: string; sub?: string }>;

export default async function GeoblockedPage({
  searchParams,
}: {
  searchParams: SearchParams;
}) {
  const params = await searchParams;
  const cc = (params.cc ?? "").toUpperCase();
  const sub = (params.sub ?? "").toUpperCase();
  const country = cc ? countryForCode(cc) : null;

  const regionLabel = country
    ? sub
      ? `${country.flag} ${country.name} (${sub})`
      : `${country.flag} ${country.name}`
    : "your region";

  return (
    <div className="nf-page">
      <div className="nf-code" aria-hidden="true">451</div>
      <h1 className="nf-title">Delfi is not available in {regionLabel}.</h1>
      <p className="nf-body">
        Prediction markets are regulated differently across jurisdictions. To
        stay on the right side of local rules, Delfi does not operate in a
        handful of regions where the product cannot be offered compliantly.
        This list changes over time; if you believe you are seeing this in
        error, get in touch and we will take a look.
      </p>
      <p className="nf-body" style={{ marginTop: 12 }}>
        If you reached this page while travelling, Delfi will be available
        again as soon as you return to a supported region.
      </p>
      <div className="nf-ctas">
        <Link href="/" className="btn-primary">Back to home</Link>
        <a href="mailto:support@delfi.bot?subject=Geoblock%20question" className="btn-ghost">
          Contact support
        </a>
      </div>
    </div>
  );
}
