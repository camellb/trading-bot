"use client";

/**
 * CelebrationBurst - sparkle animation for unlocked milestones.
 *
 * CSS-only confetti burst. Renders absolutely positioned particles
 * that expand outward and fade over 1.5s.
 */

export function CelebrationBurst({ active }: { active: boolean }) {
  if (!active) return null;

  return (
    <div className="absolute inset-0 pointer-events-none overflow-hidden z-20">
      {/* Center burst particles */}
      {[...Array(8)].map((_, i) => (
        <span
          key={i}
          className="absolute left-1/2 top-1/2 w-1.5 h-1.5 rounded-full animate-celebration"
          style={{
            background: i % 3 === 0 ? "#00ffff" : i % 3 === 1 ? "#00cccc" : "#009999",
            animationDelay: `${i * 80}ms`,
            transform: `translate(-50%, -50%) rotate(${i * 45}deg) translateY(-20px)`,
          }}
        />
      ))}
    </div>
  );
}
