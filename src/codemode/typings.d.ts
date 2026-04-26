// Typed SDK exposed to model-written TypeScript inside the Bun sandbox.
// The Gemini brain writes code against these globals; we route the calls
// to the Python FastAPI gateway over fetch().

declare global {
  const crm: {
    /**
     * Look up vehicle owner + active policy by license plate (e.g. "B-AB-1234").
     * Returns null if not found.
     */
    lookupByPlate(plate: string): Promise<
      { owner: string; vehicle: string; policy_id: string } | null
    >;
  };

  const fraud: {
    /**
     * Score a candidate claim for fraud risk. score is 0..1; flags is a list
     * of human-readable warnings ("possibly-staged", "duplicate-near-recent", ...).
     */
    check(claim: {
      plate: string;
      description: string;
      location: string;
    }): Promise<{ score: number; flags: string[] }>;
  };

  const claimDb: {
    /**
     * Persist a finalized claim. Returns the assigned claim_id.
     */
    write(claim: object): Promise<{ claim_id: string }>;
  };

  const tavily: {
    /**
     * Real-time German P&C claim domain research via Tavily Research API.
     * Returns a structured summary scoped to the claim taxonomy + fraud
     * red-flags + missing-fact lookup.
     */
    research(query: string): Promise<{
      claim_taxonomy: string;
      fraud_red_flags: string[];
      missing_facts: string[];
    }>;
  };

  const photo: {
    /**
     * Describe a photo of vehicle damage; returns a short German description
     * and a coarse severity bucket.
     */
    describe(url: string): Promise<{
      description: string;
      damage_severity: "low" | "medium" | "high";
    }>;
  };
}

export {};
