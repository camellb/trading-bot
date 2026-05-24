import Script from "next/script";

// Third-party analytics and heatmap injection.
//
// Each provider renders only when its NEXT_PUBLIC_ env var is set,
// so staging/dev deploys stay clean until the IDs are added in Vercel.
//
// Required env (add in Vercel project settings):
//   NEXT_PUBLIC_GA_ID          Google Analytics 4 measurement ID (G-XXXXXXXXXX)
//   NEXT_PUBLIC_META_PIXEL_ID  Meta Pixel ID (numeric string)
//   NEXT_PUBLIC_CLARITY_ID     Microsoft Clarity project ID (10-char string)
//
// Clarity covers heatmaps and session recordings. It is free and
// unlimited, replacing the earlier PostHog integration. If we later
// want event analytics / funnels / feature flags we can add PostHog
// back alongside Clarity; GA4 handles events today.
//
// Each snippet is a vendor-supplied loader, not our code; the only
// interpolation is the ID.
export function Analytics() {
  const gaId = process.env.NEXT_PUBLIC_GA_ID;
  const metaId = process.env.NEXT_PUBLIC_META_PIXEL_ID;
  const clarityId = process.env.NEXT_PUBLIC_CLARITY_ID;

  return (
    <>
      {gaId ? (
        <>
          <Script
            src={`https://www.googletagmanager.com/gtag/js?id=${gaId}`}
            strategy="afterInteractive"
          />
          <Script id="ga-inline" strategy="afterInteractive">
            {`window.dataLayer = window.dataLayer || [];
function gtag(){dataLayer.push(arguments);}
gtag('js', new Date());
gtag('config', '${gaId}');`}
          </Script>
        </>
      ) : null}

      {metaId ? (
        <>
          <Script id="meta-pixel" strategy="afterInteractive">
            {`!function(f,b,e,v,n,t,s)
{if(f.fbq)return;n=f.fbq=function(){n.callMethod?
n.callMethod.apply(n,arguments):n.queue.push(arguments)};
if(!f._fbq)f._fbq=n;n.push=n;n.loaded=!0;n.version='2.0';
n.queue=[];t=b.createElement(e);t.async=!0;
t.src=v;s=b.getElementsByTagName(e)[0];
s.parentNode.insertBefore(t,s)}(window, document,'script',
'https://connect.facebook.net/en_US/fbevents.js');
fbq('init', '${metaId}');
fbq('track', 'PageView');`}
          </Script>
          <noscript>
            <img
              alt=""
              height="1"
              width="1"
              style={{ display: "none" }}
              src={`https://www.facebook.com/tr?id=${metaId}&ev=PageView&noscript=1`}
            />
          </noscript>
        </>
      ) : null}

      {clarityId ? (
        <Script id="clarity-inline" strategy="afterInteractive">
          {`(function(c,l,a,r,i,t,y){
c[a]=c[a]||function(){(c[a].q=c[a].q||[]).push(arguments)};
t=l.createElement(r);t.async=1;t.src="https://www.clarity.ms/tag/"+i;
y=l.getElementsByTagName(r)[0];y.parentNode.insertBefore(t,y);
})(window, document, "clarity", "script", "${clarityId}");`}
        </Script>
      ) : null}
    </>
  );
}
