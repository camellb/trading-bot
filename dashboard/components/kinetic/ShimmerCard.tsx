"use client";

/**
 * ShimmerCard — loading overlay for cards.
 *
 * Wraps any card content. When `loading` is true, overlays a
 * sweeping gradient shimmer effect to signal data refresh.
 */

export function ShimmerCard({
  loading,
  children,
  className = "",
}: {
  loading: boolean;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={`relative overflow-hidden ${className}`}>
      {children}
      {loading && (
        <div className="absolute inset-0 pointer-events-none z-10">
          <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white/[0.03] to-transparent animate-shimmer" />
        </div>
      )}
    </div>
  );
}
