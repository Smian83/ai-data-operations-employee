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
  sparkle: <Icon><path d="m12 3 1.1 3.4a5 5 0 0 0 3.2 3.2l3.4 1.1-3.4 1.1a5 5 0 0 0-3.2 3.2L12 18.5 10.9 15a5 5 0 0 0-3.2-3.2l-3.4-1.1 3.4-1.1a5 5 0 0 0 3.2-3.2L12 3Z"/></Icon>,
  sliders: <Icon><path d="M4 6h10M18 6h2M4 12h2M10 12h10M4 18h7M15 18h5"/><circle cx="16" cy="6" r="2"/><circle cx="8" cy="12" r="2"/><circle cx="13" cy="18" r="2"/></Icon>,
  merge: <Icon><path d="M5 4v4c0 2.2 1.8 4 4 4h6m0 0-3-3m3 3-3 3M5 20v-4c0-2.2 1.8-4 4-4"/></Icon>,
  check: <Icon><path d="m5 12 4 4L19 6"/><circle cx="12" cy="12" r="9"/></Icon>,
  review: <Icon><path d="M9 11l2 2 4-4"/><path d="M5 3h14v18H5z"/><path d="M9 17h6"/></Icon>,
  chart: <Icon><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></Icon>,
  clock: <Icon><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></Icon>,
  settings: <Icon><circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1l2-1.5-2-3.4-2.4 1a8 8 0 0 0-1.8-1L14.4 3h-4.8l-.4 3.1a8 8 0 0 0-1.8 1l-2.4-1-2 3.4L5.1 11a7 7 0 0 0 0 2L3 14.5l2 3.4 2.4-1a8 8 0 0 0 1.8 1l.4 3.1h4.8l.4-3.1a8 8 0 0 0 1.8-1l2.4 1 2-3.4-2.1-1.5a7 7 0 0 0 .1-1Z"/></Icon>,
  file: <Icon><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v6h6"/></Icon>,
  success: <Icon><circle cx="12" cy="12" r="9"/><path d="m8 12 2.5 2.5L16 9"/></Icon>,
  error: <Icon><circle cx="12" cy="12" r="9"/><path d="m9 9 6 6m0-6-6 6"/></Icon>,
};

const navItems = [
  ["Dashboard", "dashboard", "/"], ["Upload Data", "upload", "/upload-data"], ["Data Sources", "database", "/data-sources"],
  ["Cleaning", "sparkle", "/cleaning"], ["Standardization", "sliders", "/standardization"], ["Match & Merge", "merge", "/match-merge"],
  ["Validation", "check", "/validation"], ["Approval Queue", "review", "/approval-queue"], ["Reports", "chart", "/reports"],
  ["Audit Log", "clock", "/audit-log"], ["Settings", "settings", "/settings"],
] as const;

const recentUploads = [
  { name: "Customer_Data.csv", size: "5.2 MB", time: "2 min ago", status: "Complete", tone: "#22C55E" },
  { name: "Sales_Records.xlsx", size: "12.8 MB", time: "8 min ago", status: "Processing", tone: "#3B82F6" },
  { name: "Inventory_List.csv", size: "7.1 MB", time: "1 hour ago", status: "Needs Review", tone: "#F59E0B" },
];

function BrandLogo() {
  return <div className="brand-logo" aria-hidden="true"><svg viewBox="0 0 42 42" fill="none"><defs><linearGradient id="uploadLogoMark" x1="8" y1="7" x2="35" y2="35" gradientUnits="userSpaceOnUse"><stop stopColor="#C4B5FD"/><stop offset=".52" stopColor="#A855F7"/><stop offset="1" stopColor="#3B82F6"/></linearGradient></defs><path className="logo-links" stroke="url(#uploadLogoMark)" d="m21 10-9 5.5v11L21 32l9-5.5v-11L21 10Zm0 0v8.5m-9-3 9 5 9-5m-9 5V32"/><path className="logo-core" stroke="url(#uploadLogoMark)" d="m21 16.5 4 2.3v4.5l-4 2.2-4-2.2v-4.5l4-2.3Z"/><circle cx="21" cy="8" r="2.2"/><circle cx="10" cy="14.5" r="2.2"/><circle cx="32" cy="14.5" r="2.2"/><circle cx="10" cy="27.5" r="2.2"/><circle cx="32" cy="27.5" r="2.2"/><circle cx="21" cy="34" r="2.2"/></svg></div>;
}

export default function UploadDataPage() {
  return <main className="dashboard-shell min-h-screen">
    <aside className="desktop-sidebar flex flex-col border-r border-slate-800/70 bg-[#070a16]/90 p-3 backdrop-blur-xl">
      <Link href="/" className="flex items-center gap-3 rounded-xl px-2 py-3 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#A855F7]"><BrandLogo/><div><div className="font-semibold tracking-tight">AI Data</div><div className="text-xs text-slate-400">Operations</div></div></Link>
      <nav className="mt-5 space-y-1" aria-label="Main navigation">{navItems.map(([item, icon, href]) => <Link key={item} href={href} className={`nav-item ${item === "Upload Data" ? "upload-nav-active" : ""}`} aria-current={item === "Upload Data" ? "page" : undefined}><span className="h-[18px] w-[18px]">{icons[icon]}</span><span>{item}</span>{item === "Approval Queue" && <span className="ml-auto rounded-full bg-[#8B5CF6]/20 px-2 py-0.5 text-[11px] text-[#C4B5FD]">2</span>}</Link>)}</nav>
      <div className="mt-auto rounded-xl border border-slate-800/80 bg-white/[.025] p-3"><div className="flex items-center gap-3"><div className="grid h-9 w-9 place-items-center rounded-full bg-[#211A45] text-sm font-medium text-[#C4B5FD]">S</div><div><div className="text-sm font-medium">Salman</div><div className="text-xs text-slate-500">Admin</div></div></div></div>
    </aside>

    <div className="dashboard-content overflow-hidden px-5 py-7 sm:px-7 lg:px-9 lg:py-9">
      <header><h1 className="text-3xl font-semibold tracking-[-0.035em]">Upload Data</h1><p className="mt-2 text-sm text-slate-400 sm:text-base">Add files to start a new data operation.</p></header>

      <section className="upload-layout mt-7 grid gap-5 xl:grid-cols-[minmax(0,1.35fr)_minmax(280px,.65fr)]">
        <div className="card p-5 sm:p-6">
          <div><h2 className="font-semibold">Upload files</h2><p className="mt-1 text-sm text-slate-400">Choose one or more data files to upload.</p></div>
          <label className="upload-dropzone mt-5 flex min-h-72 cursor-pointer flex-col items-center justify-center px-6 py-10 text-center" tabIndex={0}>
            <input className="sr-only" type="file" accept=".csv,.xlsx,.xls" multiple/>
            <span className="upload-drop-icon"><span className="h-7 w-7">{icons.upload}</span></span>
            <span className="mt-5 text-lg font-semibold text-white">Drag &amp; Drop Files Here</span>
            <span className="mt-2 text-sm text-slate-400">or <span className="font-medium text-[#A78BFA]">click to browse</span></span>
            <span className="mt-5 rounded-full border border-slate-700/80 bg-[#080D1D] px-3 py-1.5 text-xs text-slate-500">Supported formats: CSV, Excel (.xlsx, .xls)</span>
          </label>
          <div className="mt-5 flex justify-end"><button type="button" className="glow-button inline-flex items-center gap-2 rounded-xl bg-gradient-to-r from-[#8B5CF6] to-[#A855F7] px-5 py-3 text-sm font-medium text-white focus-visible:outline-none"><span className="h-[18px] w-[18px]">{icons.upload}</span>Upload</button></div>
        </div>

        <div className="space-y-5">
          <div className="card p-5 sm:p-6"><h2 className="font-semibold">Upload Progress</h2><div className="mt-5 rounded-xl border border-slate-800/80 bg-white/[.015] p-4"><div className="flex items-center justify-between gap-3 text-sm"><span className="text-slate-300">No upload in progress</span><span className="text-slate-500">0%</span></div><div className="mt-3 h-1.5 overflow-hidden rounded-full bg-slate-800"><div className="h-full w-0 rounded-full bg-gradient-to-r from-[#8B5CF6] to-[#3B82F6]"/></div><p className="mt-3 text-xs text-slate-500">Progress will appear here after you select a file.</p></div></div>
          <div className="card p-5 sm:p-6"><h2 className="font-semibold">Upload Status</h2><div className="mt-4 space-y-3"><div className="status-placeholder status-success"><span>{icons.success}</span><div><p className="text-sm font-medium">Success message</p><p className="mt-1 text-xs text-slate-500">A completed upload will appear here.</p></div></div><div className="status-placeholder status-error"><span>{icons.error}</span><div><p className="text-sm font-medium">Error message</p><p className="mt-1 text-xs text-slate-500">Upload issues will appear here.</p></div></div></div></div>
        </div>
      </section>

      <section className="card mt-5 p-5 sm:p-6"><div><h2 className="font-semibold">Recent Uploads</h2><p className="mt-1 text-xs text-slate-500">Files uploaded in the last 7 days</p></div><div className="mt-4 divide-y divide-slate-800/80">{recentUploads.map(file => <div key={file.name} className="flex items-center gap-3 py-4"><span className="list-icon text-[#A855F7]">{icons.file}</span><div className="min-w-0 flex-1"><p className="truncate text-sm font-medium">{file.name}</p><p className="mt-1 text-xs text-slate-500">{file.size} · {file.time}</p></div><span className="rounded-full px-2.5 py-1 text-xs" style={{color:file.tone,backgroundColor:`${file.tone}12`}}>{file.status}</span></div>)}</div></section>
    </div>
  </main>;
}
