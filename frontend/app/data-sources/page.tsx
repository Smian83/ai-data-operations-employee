import Link from "next/link";
import type { ReactNode, SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;
function Icon({ children, ...props }: IconProps & { children: ReactNode }) {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>{children}</svg>;
}

const icons = {
  dashboard:<Icon><rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/></Icon>,
  upload:<Icon><path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5"/><path d="M5 14v5a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-5"/></Icon>,
  database:<Icon><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v7c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12v7c0 1.7 3.6 3 8 3s8-1.3 8-3v-7"/></Icon>,
  sparkle:<Icon><path d="m12 3 1.1 3.4a5 5 0 0 0 3.2 3.2l3.4 1.1-3.4 1.1a5 5 0 0 0-3.2 3.2L12 18.5 10.9 15a5 5 0 0 0-3.2-3.2l-3.4-1.1 3.4-1.1a5 5 0 0 0 3.2-3.2L12 3Z"/></Icon>,
  sliders:<Icon><path d="M4 6h10M18 6h2M4 12h2M10 12h10M4 18h7M15 18h5"/><circle cx="16" cy="6" r="2"/><circle cx="8" cy="12" r="2"/><circle cx="13" cy="18" r="2"/></Icon>,
  merge:<Icon><path d="M5 4v4c0 2.2 1.8 4 4 4h6m0 0-3-3m3 3-3 3M5 20v-4c0-2.2 1.8-4 4-4"/></Icon>,
  check:<Icon><path d="m5 12 4 4L19 6"/><circle cx="12" cy="12" r="9"/></Icon>,
  review:<Icon><path d="M9 11l2 2 4-4"/><path d="M5 3h14v18H5z"/><path d="M9 17h6"/></Icon>,
  chart:<Icon><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></Icon>,
  clock:<Icon><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></Icon>,
  settings:<Icon><circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1l2-1.5-2-3.4-2.4 1a8 8 0 0 0-1.8-1L14.4 3h-4.8l-.4 3.1a8 8 0 0 0-1.8 1l-2.4-1-2 3.4L5.1 11a7 7 0 0 0 0 2L3 14.5l2 3.4 2.4-1a8 8 0 0 0 1.8 1l.4 3.1h4.8l.4-3.1a8 8 0 0 0 1.8-1l2.4 1 2-3.4-2.1-1.5a7 7 0 0 0 .1-1Z"/></Icon>,
  excel:<Icon><path d="M4 3h16v18H4zM4 9h16M10 3v18"/><path d="m13.5 13 3 4m0-4-3 4"/></Icon>,
  sheets:<Icon><path d="M6 2h9l4 4v16H6zM14 2v5h5M9 11h7M9 15h7M12 9v8"/></Icon>,
  postgres:<Icon><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v7c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12v7c0 1.7 3.6 3 8 3s8-1.3 8-3v-7"/></Icon>,
  mysql:<Icon><path d="M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3Z"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></Icon>,
  sqlserver:<Icon><path d="M5 5.5C5 4.1 8.1 3 12 3s7 1.1 7 2.5S15.9 8 12 8 5 6.9 5 5.5Z"/><path d="M5 5.5v6C5 12.9 8.1 14 12 14s7-1.1 7-2.5v-6M5 11.5v6C5 18.9 8.1 20 12 20s7-1.1 7-2.5v-6"/></Icon>,
  quickbooks:<Icon><circle cx="12" cy="12" r="9"/><path d="M9 8h3a4 4 0 0 1 0 8H9V8Zm6 2v8"/></Icon>,
  hubspot:<Icon><circle cx="7" cy="12" r="2"/><circle cx="17" cy="7" r="2"/><circle cx="17" cy="17" r="2"/><path d="m9 11 6.2-3M9 13l6.2 3M17 5V3"/></Icon>,
  plus:<Icon><path d="M12 5v14M5 12h14"/></Icon>,
  sync:<Icon><path d="M20 7h-5V2M4 17h5v5"/><path d="M5.5 9a7 7 0 0 1 11.8-3L20 7M4 17l2.7 1a7 7 0 0 0 11.8-3"/></Icon>,
};

const navItems = [["Dashboard","dashboard","/"],["Upload Data","upload","/upload-data"],["Data Sources","database","/data-sources"],["Cleaning","sparkle","/cleaning"],["Standardization","sliders","/standardization"],["Match & Merge","merge","/match-merge"],["Validation","check","/validation"],["Approval Queue","review","/approval-queue"],["Reports","chart","/reports"],["Audit Log","clock","/audit-log"],["Settings","settings","/settings"]] as const;

const connectedSources = [
  {name:"Excel",icon:"excel",status:"Connected",sync:"Synced 12 minutes ago",action:"Sync Now",tone:"#22C55E"},
  {name:"Google Sheets",icon:"sheets",status:"Connected",sync:"Synced 24 minutes ago",action:"Sync Now",tone:"#22C55E"},
  {name:"SQL Server",icon:"sqlserver",status:"Connected",sync:"Synced 31 minutes ago",action:"Sync Now",tone:"#22C55E"},
  {name:"PostgreSQL",icon:"postgres",status:"Connected",sync:"Synced 45 minutes ago",action:"Sync Now",tone:"#22C55E"},
  {name:"MySQL",icon:"mysql",status:"Connected",sync:"Synced 1 hour ago",action:"Sync Now",tone:"#22C55E"},
] as const;

const availableSources = [
  {name:"QuickBooks",icon:"quickbooks"},
  {name:"HubSpot CRM",icon:"hubspot"},
] as const;

function BrandLogo(){return <div className="brand-logo" aria-hidden="true"><svg viewBox="0 0 42 42" fill="none"><defs><linearGradient id="sourceLogoMark" x1="8" y1="7" x2="35" y2="35"><stop stopColor="#C4B5FD"/><stop offset=".52" stopColor="#A855F7"/><stop offset="1" stopColor="#3B82F6"/></linearGradient></defs><path className="logo-links" stroke="url(#sourceLogoMark)" d="m21 10-9 5.5v11L21 32l9-5.5v-11L21 10Zm0 0v8.5m-9-3 9 5 9-5m-9 5V32"/><path className="logo-core" stroke="url(#sourceLogoMark)" d="m21 16.5 4 2.3v4.5l-4 2.2-4-2.2v-4.5l4-2.3Z"/><circle cx="21" cy="8" r="2.2"/><circle cx="10" cy="14.5" r="2.2"/><circle cx="32" cy="14.5" r="2.2"/><circle cx="10" cy="27.5" r="2.2"/><circle cx="32" cy="27.5" r="2.2"/><circle cx="21" cy="34" r="2.2"/></svg></div>}

export default function DataSourcesPage(){return <main className="dashboard-shell min-h-screen">
  <aside className="desktop-sidebar flex flex-col border-r border-slate-800/70 bg-[#070a16]/90 p-3 backdrop-blur-xl"><Link href="/" className="flex items-center gap-3 rounded-xl px-2 py-3 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#A855F7]"><BrandLogo/><div><div className="font-semibold tracking-tight">AI Data</div><div className="text-xs text-slate-400">Operations</div></div></Link><nav className="mt-5 space-y-1" aria-label="Main navigation">{navItems.map(([item,icon,href])=><Link key={item} href={href} className={`nav-item ${item==="Data Sources"?"upload-nav-active":""}`} aria-current={item==="Data Sources"?"page":undefined}><span className="h-[18px] w-[18px]">{icons[icon]}</span><span>{item}</span>{item==="Approval Queue"&&<span className="ml-auto rounded-full bg-[#8B5CF6]/20 px-2 py-0.5 text-[11px] text-[#C4B5FD]">2</span>}</Link>)}</nav><div className="mt-auto rounded-xl border border-slate-800/80 bg-white/[.025] p-3"><div className="flex items-center gap-3"><div className="grid h-9 w-9 place-items-center rounded-full bg-[#211A45] text-sm font-medium text-[#C4B5FD]">S</div><div><div className="text-sm font-medium">Salman</div><div className="text-xs text-slate-500">Admin</div></div></div></div></aside>
  <div className="dashboard-content overflow-hidden px-5 py-7 sm:px-7 lg:px-9 lg:py-9">
    <header className="flex items-start justify-between gap-4"><div><h1 className="text-3xl font-semibold tracking-[-0.035em]">Data Sources</h1><p className="mt-2 text-sm text-slate-400 sm:text-base">Connect and manage the places where your data comes from.</p></div><a href="#add-source" className="glow-button inline-flex shrink-0 items-center gap-2 rounded-xl bg-gradient-to-r from-[#8B5CF6] to-[#A855F7] px-4 py-3 text-sm font-medium text-white focus-visible:outline-none"><span className="h-[18px] w-[18px]">{icons.plus}</span><span className="hidden sm:inline">Add Data Source</span></a></header>
    <section className="mt-7"><div><h2 className="font-semibold">Connected Data Sources</h2><p className="mt-1 text-xs text-slate-500">Your active connections and their latest sync status</p></div><div className="mt-4 grid gap-4 md:grid-cols-2 xl:grid-cols-3">{connectedSources.map(source=><article key={source.name} className="source-card card flex min-h-48 flex-col p-5"><div className="flex items-start justify-between gap-3"><span className="source-icon"><span>{icons[source.icon]}</span></span><span className="rounded-full px-2.5 py-1 text-xs font-medium" style={{color:source.tone,backgroundColor:`${source.tone}12`}}>{source.status}</span></div><h3 className="mt-4 font-semibold text-white">{source.name}</h3><p className="mt-1 text-xs text-slate-500">{source.sync}</p><button type="button" className="source-action mt-auto"><span className="h-4 w-4">{icons.sync}</span>{source.action}</button></article>)}</div></section>
    <section id="add-source" className="mt-8 scroll-mt-6"><div><h2 className="font-semibold">Connect a New Data Source</h2><p className="mt-1 text-xs text-slate-500">Choose a connector that is not yet connected</p></div><div className="mt-4 grid gap-4 md:grid-cols-2 xl:grid-cols-3">{availableSources.map(source=><article key={source.name} className="source-card card flex min-h-44 flex-col p-5"><span className="source-icon"><span>{icons[source.icon]}</span></span><h3 className="mt-4 font-semibold text-white">{source.name}</h3><p className="mt-1 text-xs text-slate-500">Not connected</p><button type="button" className="source-action mt-auto"><span className="h-4 w-4">{icons.plus}</span>Connect</button></article>)}</div></section>
  </div>
</main>}
