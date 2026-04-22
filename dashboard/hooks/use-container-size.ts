"use client";

import { useEffect, useState } from "react";

export function useContainerSize(ref: React.RefObject<HTMLDivElement | null>) {
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      if (width > 0 && height > 0) {
        setSize((prev) =>
          prev && prev.w === Math.floor(width) && prev.h === Math.floor(height)
            ? prev
            : { w: Math.floor(width), h: Math.floor(height) },
        );
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return size;
}
