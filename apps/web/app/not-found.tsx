import Link from "next/link";
import "./styles/content.css";

export const metadata = { title: "Not found - Delfi" };

export default function NotFound() {
  return (
    <div className="nf-page">
      <div className="nf-code">404</div>
      <h1 className="nf-title">This prophecy hasn&apos;t been written.</h1>
      <p className="nf-body">
        The page you were looking for doesn&apos;t exist, or was moved. Delfi is still scanning the markets
        that do.
      </p>
      <div className="nf-ctas">
        <Link href="/" className="btn-primary">Back to home</Link>
      </div>
    </div>
  );
}
