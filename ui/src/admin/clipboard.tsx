// Clipboard primitives shared by the applications list and the per-app
// Settings tab. Extracted from ApplicationsPanel.tsx unchanged when the
// Settings tab moved into its own module (AppSettings.tsx) — both sides need
// them, and importing back into ApplicationsPanel would make a cycle.
import { useState } from "react";

// Copy-to-clipboard that actually works on this deployment.
//
// `navigator.clipboard` is only defined in a SECURE CONTEXT. The console is
// routinely served over plain http on a NodePort IP (the DGX lab reaches it at
// http://<node>:30808), where the whole API is undefined — so a button wired
// straight to it silently does nothing in exactly the environment operators
// use. Falls back to the execCommand path, and reports honestly when neither
// works rather than showing a "copied" tick that didn't happen.
export function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    return navigator.clipboard.writeText(text).then(() => true, () => execCopy(text));
  }
  return Promise.resolve(execCopy(text));
}

function execCopy(text: string): boolean {
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    // Keep it off-screen and unfocusable-looking, but still selectable.
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

export function CopyButton({ value, what }: { value: string; what: string }) {
  const [state, setState] = useState<"idle" | "ok" | "fail">("idle");
  return (
    <button
      type="button"
      title={`Copy ${what} to clipboard`}
      aria-label={`Copy ${what}`}
      onClick={async () => {
        setState((await copyText(value)) ? "ok" : "fail");
        window.setTimeout(() => setState("idle"), 1500);
      }}
      className="cursor-pointer rounded border border-edge px-1 py-0.5 text-[10px] text-gray-400 transition-colors duration-200 hover:text-gray-100 hover:border-gray-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent">
      {state === "ok" ? "copied" : state === "fail" ? "copy failed" : "copy"}
    </button>
  );
}

// The application id is the value every admin API call, config script and
// support conversation is keyed by (ui-open-items #7: a real operator asked
// "where's the app id?" while trying to set a per-app policy over curl). It
// used to render as an unlabelled grey parenthetical after the name.
export function CopyableId({ value, label = "Application ID" }: { value: string; label?: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="text-[10px] uppercase tracking-wide text-gray-500">{label}</span>
      <code className="font-mono text-xs text-gray-200 select-all">{value}</code>
      <CopyButton value={value} what={label.toLowerCase()} />
    </span>
  );
}

// A token shown exactly once, right after it's minted.
export function TokenReveal({ token }: { token: string }) {
  return (
    <div className="rounded-md border border-accent/60 bg-accent/10 p-3 text-sm">
      <div className="text-accent font-medium mb-1">New token — copy it now, it won't be shown again</div>
      <code className="block break-all font-mono text-gray-100">{token}</code>
      <div className="mt-2"><CopyButton value={token} what="token" /></div>
    </div>
  );
}
