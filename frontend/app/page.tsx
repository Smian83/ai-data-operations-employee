import Link from "next/link";
import type { ReactNode, SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

function Icon({ children, ...props }: IconProps & { children: ReactNode }) {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>{children}</svg>;
}

const icons = {
  dashboard: <Icon><rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/></Icon>,
  upload: <Icon><path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5"/><path d="M5 14v5a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-5"/></Icon>,
  database: <Icon><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v7c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12v7c0 1.7 3.6 3 8 3s8-1.3 8-3v-7"/></Icon>,
  sparkle: <Icon><path d="m12 3 1.1 3.4a5 5 0 0 0 3.2 3.2l3.4 1.1-3.4 1.1a5 5 0 0 0-3.2 3.2L12 18.5 10.9 15a5 5 0 0 0-3.2-3.2l-3.4-1.1 3.4-1.1a5 5 0 0 0 3.2-3.2L12 3Z"/><path d="m19 17 .5 1.5L21 19l-1.5.5L19 21l-.5-1.5L17 19l1.5-.5L19 17Z"/></Icon>,
  sliders: <Icon><path d="M4 6h10M18 6h2M4 12h2M10 12h10M4 18h7M15 18h5"/><circle cx="16" cy="6" r="2"/><circle cx="8" cy="12" r="2"/><circle cx="13" cy="18" r="2"/></Icon>,
  merge: <Icon><path d="M5 4v4c0 2.2 1.8 4 4 4h6m0 0-3-3m3 3-3 3M5 20v-4c0-2.2 1.8-4 4-4"/></Icon>,
  check: <Icon><path d="m5 12 4 4L19 6"/><circle cx="12" cy="12" r="9"/></Icon>,
  review: <Icon><path d="M9 11l2 2 4-4"/><path d="M5 3h14v18H5z"/><path d="M9 17h6"/></Icon>,
  chart: <Icon><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></Icon>,
  clock: <Icon><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></Icon>,
  settings: <Icon><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.6v-.2h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/></Icon>,
  health: <Icon><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7Z"/><path d="M3.22 12H9.5l.5-1 2 4.5 2-7 1.5 3.5h5.27"/></Icon>,
  file: <Icon><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v6h6M12 18v-6m-3 3 3-3 3 3"/></Icon>,
  play: <Icon><circle cx="12" cy="12" r="10"/><path d="m10 8 6 4-6 4Z"/></Icon>,
  warning: <Icon><path d="m21.73 18-8-14a2 2 0 0 0-3.46 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4M12 17h.01"/></Icon>,
  arrow: <Icon><path d="M5 12h14m-5-5 5 5-5 5"/></Icon>,
};

const navItems = [
  ["Dashboard", "dashboard"], ["Upload Data", "upload"], ["Data Sources", "database"],
  ["Cleaning", "sparkle"], ["Standardization", "sliders"], ["Match & Merge", "merge"],
  ["Validation", "check"], ["Approval Queue", "review"], ["Reports", "chart"],
  ["Audit Log", "clock"], ["Settings", "settings"],
] as const;

const uploads = [
  { file: "Customer_Data.csv", meta: "5.2 MB · 2 min ago", status: "Complete", tone: "#22C55E" },
  { file: "Sales_Records.xlsx", meta: "12.8 MB · 8 min ago", status: "Processing", tone: "#3B82F6" },
  { file: "Inventory_List.csv", meta: "7.1 MB · 1 hour ago", status: "Needs Review", tone: "#F59E0B" },
];
const navRoutes: Record<string, string> = { Dashboard:"/", "Upload Data":"/upload-data", "Data Sources":"/data-sources", Cleaning:"/cleaning", Standardization:"/standardization", "Match & Merge":"/match-merge", Validation:"/validation", "Approval Queue":"/approval-queue", Reports:"/reports", "Audit Log":"/audit-log", Settings:"/settings" };

const approvals = [["Duplicate customer records", "24 possible duplicates"], ["Unmatched sales records", "98 records need review"]];

function BrandLogo() {
  return <div className="brand-logo" aria-hidden="true"><svg viewBox="0 0 42 42" fill="none"><defs><linearGradient id="logoMark" x1="8" y1="7" x2="35" y2="35" gradientUnits="userSpaceOnUse"><stop stopColor="#C4B5FD"/><stop offset=".52" stopColor="#A855F7"/><stop offset="1" stopColor="#3B82F6"/></linearGradient></defs><path className="logo-links" d="m21 10-9 5.5v11L21 32l9-5.5v-11L21 10Zm0 0v8.5m-9-3 9 5 9-5m-9 5V32"/><path className="logo-core" d="m21 16.5 4 2.3v4.5l-4 2.2-4-2.2v-4.5l4-2.3Z"/><circle cx="21" cy="8" r="2.2"/><circle cx="10" cy="14.5" r="2.2"/><circle cx="32" cy="14.5" r="2.2"/><circle cx="10" cy="27.5" r="2.2"/><circle cx="32" cy="27.5" r="2.2"/><circle cx="21" cy="34" r="2.2"/></svg></div>;
}

function MetricCard({ label, value, status, note, action, accent, icon }: { label: string; value: string; status?: string; note: string; action?: string; accent: string; icon: ReactNode }) {
  return <button className="metric-card card group h-44 w-full p-4 text-left focus-visible:outline-none" type="button">
    <div className="flex items-center justify-between"><span className="text-sm font-medium text-slate-300">{label}</span><span className="metric-icon" style={{ color: accent, backgroundColor: `${accent}12`, borderColor: `${accent}28` }}>{icon}</span></div>
    <div className="mt-3 flex items-end gap-3"><span className="text-3xl font-semibold tracking-[-0.035em] text-white">{value}</span>{status && <span className="mb-1 rounded-full px-2.5 py-1 text-xs font-medium" style={{ color: accent, backgroundColor: `${accent}13` }}>{status}</span>}</div>
    <div className="mt-2 text-sm text-slate-400">{note}</div>
    {action && <span className="mt-2 inline-flex items-center gap-1.5 text-sm font-medium transition-all duration-200 group-hover:gap-2.5" style={{ color: accent }}>{action}<span className="h-4 w-4">{icons.arrow}</span></span>}
  </button>;
}

export default function DashboardPage() {
  return <main className="dashboard-shell min-h-screen">
    <aside className="desktop-sidebar flex flex-col border-r border-slate-800/70 bg-[#070a16]/90 p-3 backdrop-blur-xl">
      <div className="flex items-center gap-3 px-2 py-3"><BrandLogo/><div><div className="font-semibold tracking-tight">AI Data</div><div className="text-xs text-slate-400">Operations</div></div></div>
      <nav className="mt-5 space-y-1" aria-label="Main navigation">{navItems.map(([item, icon]) => <Link key={item} href={navRoutes[item]} className="nav-item"><span className="h-[18px] w-[18px]">{icons[icon]}</span><span>{item}</span>{item === "Approval Queue" && <span className="ml-auto rounded-full bg-[#8B5CF6]/20 px-2 py-0.5 text-[11px] text-[#C4B5FD]">2</span>}</Link>)}</nav>
      <div className="mt-auto rounded-xl border border-slate-800/80 bg-white/[.025] p-3"><div className="flex items-center gap-3"><div className="grid h-9 w-9 place-items-center rounded-full bg-[#211A45] text-sm font-medium text-[#C4B5FD]">S</div><div><div className="text-sm font-medium">Salman</div><div className="text-xs text-slate-500">Admin</div></div></div></div>
    </aside>

    <div className="dashboard-content overflow-hidden px-5 py-7 sm:px-7 lg:px-9 lg:py-9">
      <header className="flex items-start justify-between gap-4"><div><h1 className="text-3xl font-semibold tracking-[-0.035em]">Dashboard</h1><p className="mt-2 text-sm text-slate-400 sm:text-base">Monitor your data operations at a glance.</p></div><Link href="/upload-data" className="glow-button inline-flex items-center gap-2 rounded-xl bg-gradient-to-r from-[#8B5CF6] to-[#A855F7] px-4 py-3 text-sm font-medium text-white focus-visible:outline-none"><span className="h-[18px] w-[18px]">{icons.upload}</span><span className="hidden sm:inline">Upload Data</span></Link></header>

      <section className="mt-7 grid gap-4 sm:grid-cols-2 xl:grid-cols-4" aria-label="Dashboard summary">
        <MetricCard label="Data Health" value="96%" status="Excellent" note="Up 4% this week" accent="#22C55E" icon={icons.health}/>
        <MetricCard label="Recent Uploads" value="3" note="Uploaded in the last 7 days" action="View Files" accent="#A855F7" icon={icons.file}/>
        <MetricCard label="Active Jobs" value="2 Running" note="Currently processing" action="View Progress" accent="#3B82F6" icon={icons.play}/>
        <MetricCard label="Needs Review" value="2 Items" note="Waiting for your approval" action="Review Now" accent="#F59E0B" icon={icons.warning}/>
      </section>

      <section className="mt-5 grid gap-5 xl:grid-cols-[1.15fr_.85fr]">
        <div className="card p-5 sm:p-6"><div className="flex items-start justify-between gap-3"><div><h2 className="font-semibold">Quality Trend</h2><p className="mt-1 text-xs text-slate-500">Overall data quality over the last 7 days</p></div><span className="shrink-0 rounded-full bg-[#8B5CF6]/10 px-3 py-1 text-xs text-[#C4B5FD]">96% today</span></div><div className="chart-panel mt-5 h-52 p-4"><svg viewBox="0 0 600 220" className="h-full w-full" role="img" aria-label="Quality trend rising to 96 percent"><defs><linearGradient id="qualityFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#8B5CF6" stopOpacity=".36"/><stop offset="1" stopColor="#3B82F6" stopOpacity="0"/></linearGradient><pattern id="chartGrid" width="60" height="44" patternUnits="userSpaceOnUse"><path d="M60 0H0V44" fill="none" stroke="#334155" strokeOpacity=".22" strokeWidth="1"/></pattern></defs><rect width="600" height="220" fill="url(#chartGrid)"/><path className="trend-area" d="M20 165 C90 90 130 60 190 95S280 165 340 115 430 90 480 70 550 58 580 35L580 205H20Z" fill="url(#qualityFill)"/><path className="trend-line" d="M20 165 C90 90 130 60 190 95S280 165 340 115 430 90 480 70 550 58 580 35" fill="none" stroke="#8B5CF6" strokeWidth="4" strokeLinecap="round"/><circle className="trend-point" cx="580" cy="35" r="6" fill="#A855F7" stroke="#D8B4FE" strokeWidth="3"/></svg></div></div>
        <div className="card p-5 sm:p-6"><SectionHeader title="Recent Uploads"/><div className="mt-3 divide-y divide-slate-800/80">{uploads.map(upload => <div key={upload.file} className="flex items-center gap-3 py-4"><span className="list-icon text-[#A855F7]">{icons.file}</span><div className="min-w-0 flex-1"><div className="truncate text-sm font-medium">{upload.file}</div><div className="mt-1 text-xs text-slate-500">{upload.meta}</div></div><span className="rounded-full px-2.5 py-1 text-xs" style={{color:upload.tone,backgroundColor:`${upload.tone}12`}}>{upload.status}</span></div>)}</div></div>
      </section>

      <section className="mt-5 grid gap-5 lg:grid-cols-3">
        <div className="card p-5 sm:p-6"><SectionHeader title="Active Jobs"/><div className="mt-5 space-y-5">{[["Data cleaning","75%"],["Standardization","45%"]].map(([name,progress]) => <div key={name}><div className="flex justify-between text-sm"><span>{name}</span><span className="text-slate-400">{progress}</span></div><div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-800"><div className="h-full rounded-full bg-gradient-to-r from-[#3B82F6] to-[#8B5CF6]" style={{width:progress}}/></div></div>)}</div></div>
        <div className="card p-5 sm:p-6"><SectionHeader title="Needs Review"/><div className="mt-4 space-y-3">{approvals.map(([title,sub]) => <button type="button" key={title} className="subtle-row group w-full p-4 text-left focus-visible:outline-none"><div className="flex gap-3"><span className="mt-0.5 h-4 w-4 shrink-0 text-[#F59E0B]">{icons.warning}</span><div><div className="text-sm font-medium">{title}</div><div className="mt-1 text-xs text-slate-500">{sub}</div><span className="mt-3 inline-flex items-center gap-1 text-xs font-medium text-[#C4B5FD]">Review <span className="h-3.5 w-3.5">{icons.arrow}</span></span></div></div></button>)}</div></div>
        <div className="card p-5 sm:p-6"><h2 className="font-semibold">Quick Actions</h2><div className="mt-4 space-y-3">{[["Upload Data","upload"],["Review Approvals","review"],["View Reports","chart"]] .map(([action,icon],index) => <button type="button" key={action} className={`quick-action group ${index === 0 ? "quick-action-primary" : ""}`}><span className="h-[18px] w-[18px] text-[#A78BFA]">{icons[icon as keyof typeof icons]}</span><span>{action}</span><span className="ml-auto h-4 w-4 text-slate-500 transition-transform duration-200 group-hover:translate-x-0.5">{icons.arrow}</span></button>)}</div></div>
      </section>
    </div>
  </main>;
}

function SectionHeader({ title }: { title: string }) {
  return <div className="flex items-center justify-between"><h2 className="font-semibold">{title}</h2><button type="button" className="text-link rounded-md text-sm text-[#60A5FA] focus-visible:outline-none">View all</button></div>;
}
