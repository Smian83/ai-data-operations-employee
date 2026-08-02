const navItems = [
  "Dashboard", "Upload Data", "Data Sources", "Cleaning", "Standardization",
  "Match & Merge", "Validation", "Approval Queue", "Reports", "Audit Log", "Settings",
];

const uploads = [
  { file: "Customer_Data.csv", meta: "5.2 MB · 2 min ago", status: "Complete", tone: "#22c55e" },
  { file: "Sales_Records.xlsx", meta: "12.8 MB · 8 min ago", status: "Processing", tone: "#3b82f6" },
  { file: "Inventory_List.csv", meta: "7.1 MB · 1 hour ago", status: "Needs Review", tone: "#f59e0b" },
];

const approvals = [
  ["Duplicate customer records", "24 possible duplicates"],
  ["Unmatched sales records", "98 records need review"],
];

function BrandLogo() {
  return (
    <div className="relative h-11 w-11 rounded-2xl bg-gradient-to-br from-[#8b5cf6] via-[#a855f7] to-[#3b82f6] shadow-[0_0_28px_rgba(139,92,246,.36)]">
      <svg viewBox="0 0 44 44" className="h-full w-full p-2" aria-label="AI Data Operations logo">
        <circle cx="22" cy="22" r="4.5" fill="white" />
        <circle cx="10" cy="12" r="3" fill="white" fillOpacity=".95" />
        <circle cx="34" cy="12" r="3" fill="white" fillOpacity=".95" />
        <circle cx="10" cy="32" r="3" fill="white" fillOpacity=".95" />
        <circle cx="34" cy="32" r="3" fill="white" fillOpacity=".95" />
        <path d="M13 14.5 18.5 19M31 14.5 25.5 19M13 29.5 18.5 25M31 29.5 25.5 25" stroke="white" strokeWidth="2" strokeLinecap="round" />
      </svg>
    </div>
  );
}

function Icon({ children, accent }: { children: React.ReactNode; accent: string }) {
  return <span className="icon-tile h-10 w-10 rounded-xl text-sm" style={{ background: `${accent}18`, color: accent }}>{children}</span>;
}

function MetricCard({ label, value, note, accent, icon }: { label: string; value: string; note: string; accent: string; icon: string }) {
  return (
    <section className="card min-h-40 p-5">
      <div className="flex items-center gap-3 text-sm text-slate-300"><Icon accent={accent}>{icon}</Icon><span>{label}</span></div>
      <div className="mt-5 text-4xl font-semibold tracking-tight">{value}</div>
      <p className="mt-2 text-sm text-slate-400">{note}</p>
    </section>
  );
}

export default function DashboardPage() {
  return (
    <main className="dashboard-shell grid min-h-screen" style={{ gridTemplateColumns: "236px 1fr" }}>
      <aside className="desktop-sidebar flex flex-col border-r border-slate-800/80 bg-[#070b18]/92 p-4 backdrop-blur-xl">
        <div className="flex items-center gap-3 px-2 py-3">
          <BrandLogo />
          <div><div className="font-semibold tracking-tight">AI Data</div><div className="text-sm text-slate-400">Operations</div></div>
        </div>
        <nav className="mt-7 space-y-1">
          {navItems.map((item, index) => (
            <button key={item} className={`w-full rounded-xl px-4 py-3 text-left text-sm transition ${index === 0 ? "bg-gradient-to-r from-[#7c3aed] to-[#5b21b6] text-white shadow-[0_8px_24px_rgba(124,58,237,.22)]" : "text-slate-300 hover:bg-white/5 hover:text-white"}`}>
              {item}{item === "Approval Queue" && <span className="float-right rounded-full bg-[#8b5cf6] px-2 py-0.5 text-xs">2</span>}
            </button>
          ))}
        </nav>
        <div className="mt-auto rounded-2xl border border-slate-800 bg-white/[.025] p-3">
          <div className="flex items-center gap-3"><div className="grid h-9 w-9 place-items-center rounded-full bg-[#211a45] text-sm">S</div><div><div className="text-sm font-medium">Salman</div><div className="text-xs text-slate-500">Admin</div></div></div>
        </div>
      </aside>

      <div className="dashboard-content overflow-hidden p-8 lg:p-10">
        <header className="flex items-start justify-between gap-4">
          <div><h1 className="text-3xl font-semibold tracking-tight">Dashboard</h1><p className="mt-2 text-slate-400">See what is happening and what needs your attention.</p></div>
          <button className="glow-button rounded-xl bg-gradient-to-r from-[#8b5cf6] to-[#a855f7] px-5 py-3 text-sm font-medium shadow-[0_10px_28px_rgba(139,92,246,.22)]">Upload Data</button>
        </header>

        <section className="mt-8 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <MetricCard label="Data Health" value="96%" note="Excellent · up 4% this week" accent="#8b5cf6" icon="✓" />
          <MetricCard label="Active Jobs" value="2" note="Currently processing" accent="#3b82f6" icon="↻" />
          <MetricCard label="Needs Your Approval" value="2" note="Items waiting for review" accent="#f59e0b" icon="!" />
          <MetricCard label="Recent Uploads" value="3" note="Uploaded in the last 7 days" accent="#22c55e" icon="↑" />
        </section>

        <section className="mt-5 grid gap-5 xl:grid-cols-[1.15fr_.85fr]">
          <div className="card p-6">
            <div className="flex items-center justify-between"><div><h2 className="font-semibold">Quality Trend</h2><p className="mt-1 text-xs text-slate-500">Overall data quality over the last 7 days</p></div><span className="rounded-full bg-[#8b5cf6]/10 px-3 py-1 text-xs text-[#c4b5fd]">96% today</span></div>
            <div className="mt-5 h-52 rounded-xl bg-[#080d1d] p-4">
              <svg viewBox="0 0 600 220" className="h-full w-full" role="img" aria-label="Quality trend rising to 96 percent">
                <defs><linearGradient id="fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#8b5cf6" stopOpacity=".42"/><stop offset="1" stopColor="#8b5cf6" stopOpacity="0"/></linearGradient></defs>
                <path d="M20 165 C90 90,130 60,190 95 S280 165,340 115 S430 90,480 70 S550 58,580 35 L580 205 L20 205Z" fill="url(#fill)"/>
                <path d="M20 165 C90 90,130 60,190 95 S280 165,340 115 S430 90,480 70 S550 58,580 35" fill="none" stroke="#8b5cf6" strokeWidth="5" strokeLinecap="round"/>
                <circle cx="580" cy="35" r="7" fill="#a855f7"/>
              </svg>
            </div>
          </div>

          <div className="card p-6">
            <div className="flex items-center justify-between"><h2 className="font-semibold">Recent Uploads</h2><button className="text-sm text-[#60a5fa]">View all</button></div>
            <div className="mt-4 divide-y divide-slate-800">
              {uploads.map((upload) => (
                <div key={upload.file} className="flex items-center gap-3 py-4">
                  <div className="icon-tile h-10 w-10 rounded-xl bg-white/5 text-xs text-slate-300">FILE</div>
                  <div className="min-w-0 flex-1"><div className="truncate text-sm font-medium">{upload.file}</div><div className="mt-1 text-xs text-slate-500">{upload.meta}</div></div>
                  <span className="rounded-full px-3 py-1 text-xs" style={{ color: upload.tone, background: `${upload.tone}16` }}>{upload.status}</span>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="mt-5 grid gap-5 lg:grid-cols-3">
          <div className="card p-6"><div className="flex items-center justify-between"><h2 className="font-semibold">Active Jobs</h2><button className="text-sm text-[#60a5fa]">View all</button></div><div className="mt-5 space-y-5">{[["Data cleaning","75%"],["Standardization","45%"]].map(([name, progress]) => <div key={name}><div className="flex justify-between text-sm"><span>{name}</span><span className="text-slate-400">{progress}</span></div><div className="mt-2 h-2 rounded-full bg-slate-800"><div className="h-2 rounded-full bg-gradient-to-r from-[#8b5cf6] to-[#a855f7]" style={{ width: progress }} /></div></div>)}</div></div>
          <div className="card p-6"><div className="flex items-center justify-between"><h2 className="font-semibold">Needs Your Approval</h2><button className="text-sm text-[#60a5fa]">View all</button></div><div className="mt-4 space-y-3">{approvals.map(([title, sub]) => <div key={title} className="rounded-xl border border-slate-800 bg-white/[.015] p-4"><div className="text-sm font-medium">{title}</div><div className="mt-1 text-xs text-slate-500">{sub}</div><button className="mt-3 text-xs text-[#c4b5fd]">Review →</button></div>)}</div></div>
          <div className="card p-6"><h2 className="font-semibold">Quick Actions</h2><div className="mt-4 space-y-3">{["Upload Data","Review Approvals","View Reports"].map((action, index) => <button key={action} className={`w-full rounded-xl border px-4 py-3 text-left text-sm transition hover:-translate-y-0.5 ${index === 0 ? "border-[#8b5cf6] bg-[#8b5cf6]/15" : "border-slate-800 bg-white/[.02] hover:border-[#8b5cf6]/40"}`}>{action}<span className="float-right">→</span></button>)}</div></div>
        </section>
      </div>
    </main>
  );
}
