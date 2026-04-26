// Bun-run sandbox that executes model-written TypeScript inside an async IIFE.
// Exposes typed mock SDKs (crm/fraud/claimDb/tavily/photo) wired to a local
// Python FastAPI gateway on http://127.0.0.1:${HUD_WS_PORT:-8765}.
//
// Invocation:
//   bun run sandbox.ts "<model-written code>"
// stdout (last line):
//   {"ok":true,"result":<value>}  OR  {"ok":false,"error":"...","stack":"..."}

const GATEWAY = process.env.GATEWAY_URL || "http://127.0.0.1:8765";

async function call(path: string, body: unknown): Promise<any> {
  const r = await fetch(`${GATEWAY}/codemode/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const text = await r.text().catch(() => "");
    throw new Error(`gateway ${path} ${r.status}: ${text}`);
  }
  return r.json();
}

(globalThis as any).crm = {
  lookupByPlate: (plate: string) => call("crm/lookup", { plate }),
};
(globalThis as any).fraud = {
  check: (claim: { plate: string; description: string; location: string }) =>
    call("fraud/check", claim),
};
(globalThis as any).claimDb = {
  write: (claim: object) => call("claim_db/write", { data: claim }),
};
(globalThis as any).tavily = {
  research: (query: string) => call("tavily/research", { query }),
};
(globalThis as any).photo = {
  describe: (url: string) => call("photo/describe", { url }),
};

const CODE = process.argv.slice(2).join(" ");
try {
  // Wrap user code in an async IIFE so it can use `await` at the top level
  // and we capture the return value.
  const fn = new Function("return (async () => { " + CODE + " })()");
  const out = await fn();
  console.log(JSON.stringify({ ok: true, result: out ?? null }));
} catch (e: any) {
  console.log(
    JSON.stringify({
      ok: false,
      error: String(e?.message ?? e),
      stack: String(e?.stack ?? ""),
    })
  );
}
