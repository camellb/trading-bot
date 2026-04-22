import "../styles/dash.css";
import { DashboardShell } from "./shell";

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  return <DashboardShell>{children}</DashboardShell>;
}
