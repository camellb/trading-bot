export function DetailField({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-widest text-slate-600">{label}</div>
      <div className="font-[family-name:var(--font-mono)] text-slate-300">{value}</div>
    </div>
  );
}
