"use client";
export default function RecoveryCodes({codes,onContinue}:{codes:string[];onContinue:()=>void}){
  function download(){const blob=new Blob([`AI Data Operations recovery codes\n\n${codes.join("\n")}\n`],{type:"text/plain"});const url=URL.createObjectURL(blob);const link=document.createElement("a");link.href=url;link.download="ai-data-recovery-codes.txt";link.click();URL.revokeObjectURL(url)}
  return <div className="recovery-panel"><div className="login-error recovery-warning">Save these codes now. Each code works once and they will not be shown again.</div><div className="recovery-grid">{codes.map(code=><code key={code}>{code}</code>)}</div><div className="auth-actions"><button className="ops-action" type="button" onClick={download}>Download codes</button><button className="glow-button login-submit" type="button" onClick={onContinue}>I saved my codes</button></div></div>;
}
