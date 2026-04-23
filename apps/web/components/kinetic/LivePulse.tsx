"use client";

/**
 * LivePulse - breathing heartbeat indicator.
 *
 * Replaces static dots with a richer "alive" animation
 * that subtly pulses to signal active processing.
 */

export function LivePulse({
  active = true,
  size = "sm",
  color = "accent",
}: {
  active?: boolean;
  size?: "xs" | "sm" | "md";
  color?: "accent" | "danger" | "warn";
}) {
  const sizeMap = {
    xs: "w-1.5 h-1.5",
    sm: "w-2 h-2",
    md: "w-2.5 h-2.5",
  };

  const colorMap = {
    accent: "bg-accent",
    danger: "bg-red-400",
    warn: "bg-amber-400",
  };

  const glowMap = {
    accent: "shadow-[0_0_6px_rgba(0,255,255,0.5)]",
    danger: "shadow-[0_0_6px_rgba(239,68,68,0.5)]",
    warn: "shadow-[0_0_6px_rgba(245,158,11,0.5)]",
  };

  return (
    <span className="relative inline-flex items-center justify-center">
      <span
        className={`
          ${sizeMap[size]} rounded-full ${colorMap[color]}
          ${active ? `animate-breathe ${glowMap[color]}` : "opacity-40"}
          transition-opacity duration-500
        `}
      />
      {active && (
        <span
          className={`
            absolute ${sizeMap[size]} rounded-full ${colorMap[color]}
            opacity-30 animate-ping
          `}
        />
      )}
    </span>
  );
}
