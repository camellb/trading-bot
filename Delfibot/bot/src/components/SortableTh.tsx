import { useMemo, useState } from "react";

/**
 * Lightweight sortable-table primitives.
 *
 * Why this is a single file with two tiny helpers instead of a full
 * "headless table" library:
 *   - We only have a handful of tables and the column sets are
 *     known statically. A full library would be bigger than the
 *     code it replaces.
 *   - Sorting always happens client-side: data sets are small
 *     (hundreds, not millions of rows) and arrive over a single
 *     fetch.
 *
 * Usage from a page:
 *
 *     type Sk = "market" | "side" | "pnl" | "settled";
 *     const sort = useSort<Sk>("settled", "desc");
 *     const rows = useMemo(
 *       () => sort.apply(raw, getKpi),
 *       [raw, sort.field, sort.dir],
 *     );
 *     ...
 *     <thead>
 *       <tr>
 *         <SortableTh field="market" sort={sort}>Market</SortableTh>
 *         <SortableTh field="pnl"    sort={sort}>P&L</SortableTh>
 *         ...
 *       </tr>
 *     </thead>
 *
 * `getKpi(row, field)` returns the raw sort key for that column on
 * that row (number, string, or null). Nulls always sort to the end.
 */

export interface SortState<F extends string> {
  field: F;
  dir: "asc" | "desc";
  setField: (f: F) => void;
  apply: <T>(rows: T[], getKpi: (row: T, field: F) => SortKey) => T[];
}

export type SortKey = number | string | boolean | null | undefined;

export function useSort<F extends string>(
  initialField: F,
  initialDir: "asc" | "desc" = "desc",
): SortState<F> {
  const [field, setFieldRaw] = useState<F>(initialField);
  const [dir, setDir] = useState<"asc" | "desc">(initialDir);

  // Click on the SAME column flips direction; click on a different
  // column resets to desc (the natural default for "show me the
  // biggest first" — most KPI columns).
  const setField = (f: F) => {
    if (f === field) {
      setDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setFieldRaw(f);
      setDir("desc");
    }
  };

  const apply = <T,>(rows: T[], getKpi: (row: T, f: F) => SortKey): T[] => {
    const out = rows.slice();
    out.sort((a, b) => {
      const ka = getKpi(a, field);
      const kb = getKpi(b, field);
      // Nulls always at the end regardless of dir, so an
      // ascending sort by P&L doesn't push pending rows to the
      // top.
      const aNull = ka == null;
      const bNull = kb == null;
      if (aNull && bNull) return 0;
      if (aNull) return 1;
      if (bNull) return -1;
      let cmp: number;
      if (typeof ka === "number" && typeof kb === "number") {
        cmp = ka - kb;
      } else if (typeof ka === "boolean" && typeof kb === "boolean") {
        cmp = (ka ? 1 : 0) - (kb ? 1 : 0);
      } else {
        cmp = String(ka).localeCompare(String(kb));
      }
      return dir === "asc" ? cmp : -cmp;
    });
    return out;
  };

  return useMemo(
    () => ({ field, dir, setField, apply }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [field, dir],
  );
}

/**
 * Header cell that renders a clickable label + a tiny up/down
 * indicator. Clicking it tells the SortState to switch sort.
 */
export function SortableTh<F extends string>({
  field,
  sort,
  children,
  align = "left",
  width,
}: {
  field: F;
  sort: SortState<F>;
  children: React.ReactNode;
  align?: "left" | "right" | "center";
  width?: number | string;
}) {
  const active = sort.field === field;
  const arrow = !active ? "" : sort.dir === "asc" ? " ▲" : " ▼";
  return (
    <th
      onClick={() => sort.setField(field)}
      style={{
        cursor: "pointer",
        userSelect: "none",
        textAlign: align,
        width,
        whiteSpace: "nowrap",
      }}
      aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
      title={active
        ? `Sorted ${sort.dir === "asc" ? "ascending" : "descending"} - click to flip`
        : "Click to sort by this column"}
    >
      {children}
      <span style={{
        opacity: active ? 0.9 : 0.25,
        marginLeft: 4,
        fontSize: "0.85em",
      }}>
        {active ? arrow : " ↕"}
      </span>
    </th>
  );
}
