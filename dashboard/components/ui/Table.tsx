export function Th({
  children, right, className,
}: { children: React.ReactNode; right?: boolean; className?: string }) {
  return (
    <th className={`px-3 py-2 ${right ? "text-right" : "text-left"} font-normal ${className ?? ""}`}>
      {children}
    </th>
  );
}

export function Empty({ text }: { text: string }) {
  return <div className="px-3 py-6 text-center text-xs text-neutral-600">{text}</div>;
}

export function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[9px] uppercase tracking-widest text-neutral-600">{label}</div>
      <div className="font-mono text-neutral-200">{value}</div>
    </div>
  );
}
